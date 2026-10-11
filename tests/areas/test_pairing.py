"""Lifecycle tests use fake devices, host Wi-Fi and discovery. No sockets."""

import asyncio
import json
import logging
import sys
import types
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from kasa import Credentials, DeviceConfig, Module
from kasa.exceptions import AuthenticationError, DeviceError
from kasa.smart import SmartDevice
from kasa.smartcam import SmartCamDevice

from tolpo import policy, provision as p
from tolpo.areas import pairing
from tolpo.calls import NeedsConfirmation
from tolpo.registry import DeviceUnavailable, Entry, Forbidden, NotFound, RegistryError

from .. import fakes

pytestmark = pytest.mark.anyio


class FakeLifecycle:
    def __init__(self, result=None, failure=None):
        self.sent, self.result, self.failure = [], result, failure

    async def invoke(self, entry, operation, **params):
        self.sent.append((entry.slug, operation, params))
        if self.failure:
            raise self.failure
        if operation == "pair_hub":
            child = fakes.FakeDevice("new-child", "New\nSensor\x00", "T110", fakes.DeviceType.Sensor)
            entry.device.children.append(child)
            return [{"device_id": "new-child", "device_model": "T110", "name": "ENCODED", "mac": "HIDDEN"}]
        if operation == "pair_remove":
            entry.device.children[:] = [d for d in entry.device.children if d.device_id != params["device_id"]]
        if operation == "pair_name":
            entry.device.alias = params["name"]
        if operation == "wifi_scan":
            return [SimpleNamespace(ssid="PRIVATE-NETWORK", bssid="PRIVATE-MAC", password="PRIVATE-PASS", channel=6, signal_level=3), SimpleNamespace(ssid="another")]
        return self.result or {}


@pytest.fixture
def house(make_house, monkeypatch):
    def make(**kwargs):
        reg, caller, devices = make_house(**kwargs)
        monkeypatch.setattr(reg, "discover", AsyncMock(return_value=len(reg.entries())))
        return reg, caller, devices
    return make


@pytest.fixture
def env(monkeypatch):
    values = {"TOLPO_PROVISION_AP": "Tapo_Setup_TEST", "TOLPO_PROVISION_SSID": "PRIVATE-NETWORK", "TOLPO_PROVISION_WIFI_PASSWORD": "PRIVATE-WIFI-PASS", "TOLPO_PROVISION_ACCOUNT": "PRIVATE-ACCOUNT", "TOLPO_PROVISION_ACCOUNT_PASSWORD": "PRIVATE-ACCOUNT-PASS"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def plan(**kwargs):
    return p.SoftAPPlan("new-plug", "expected-id", "192.0.2.1", "TOLPO_PROVISION_AP", "TOLPO_PROVISION_SSID", "TOLPO_PROVISION_WIFI_PASSWORD", "TOLPO_PROVISION_ACCOUNT", "TOLPO_PROVISION_ACCOUNT_PASSWORD", "wlan0", **kwargs)


def expected_phrase(operation, entry):
    # pair_remove names the child that is removed, not the hub.
    subject = "main-door" if operation == "pair_remove" else entry.slug
    return policy.confirmation(policy.rule_for(entry.kind, operation), subject)


async def action(reg, adapter, operation, confirm=None, **changes):
    entry = reg.resolve_one("tapo-h200" if operation in {"pair_hub", "pair_remove"} else "desk-lamp")
    if operation == "pair_hub":
        return await p.pair_hub(reg, adapter, entry, confirm=confirm, **changes)
    if operation == "pair_remove":
        return await p.pair_remove(reg, adapter, entry, "main-door", confirm, **changes)
    if operation == "pair_name":
        return await p.pair_name(reg, adapter, entry, changes.pop("name", "Paired Plug"), confirm)
    if operation == "wifi_join":
        return await p.wifi_join(reg, adapter, entry, "TOLPO_PROVISION_SSID", "TOLPO_PROVISION_WIFI_PASSWORD", "TOLPO_PROVISION_ACCOUNT", "TOLPO_PROVISION_ACCOUNT_PASSWORD", confirm=confirm, **changes)
    if operation == "reset_factory":
        return await p.reset(reg, adapter, entry, factory=True, confirm=confirm, **changes)
    if operation == "reset_reboot":
        return await p.reset(reg, adapter, entry, **changes)
    return await p.wifi_scan(reg, adapter, entry)


LIFECYCLE = ["pair_hub", "pair_remove", "pair_name", "wifi_join", "reset_factory"]


@pytest.mark.parametrize("operation", LIFECYCLE + ["reset_reboot"])
async def test_levels_stop_before_adapter_or_refresh(house, operation):
    reg, _, _ = house(tier=1)
    adapter = FakeLifecycle()
    with pytest.raises(Forbidden) as exc:
        await action(reg, adapter, operation, confirm=f"{operation} desk-lamp")
    assert "policy blocks" in str(exc.value)
    assert "allow_methods" not in str(exc.value)
    assert adapter.sent == []
    reg.discover.assert_not_awaited()


@pytest.mark.parametrize("operation", LIFECYCLE)
@pytest.mark.parametrize("confirmation", [None, "wrong action", "{operation} office-1", "{operation} tapo-h200 main-door"])
async def test_every_lifecycle_call_requires_its_exact_phrase(house, operation, confirmation):
    reg, _, _ = house(tier=4)
    adapter = FakeLifecycle()
    with pytest.raises(NeedsConfirmation):
        await action(reg, adapter, operation, confirm=confirmation and confirmation.format(operation=operation))
    assert adapter.sent == []
    reg.discover.assert_not_awaited()


@pytest.mark.parametrize("operation", LIFECYCLE + ["reset_reboot", "wifi_scan"])
async def test_happy_paths_are_compact_and_refresh_writes(house, env, monkeypatch, operation):
    reg, _, _ = house(tier=4)
    entry = reg.resolve_one("tapo-h200" if operation in {"pair_hub", "pair_remove"} else "desk-lamp")
    spy = AsyncMock(wraps=reg.refresh)
    monkeypatch.setattr(reg, "refresh", spy)
    adapter = FakeLifecycle()
    out = await action(reg, adapter, operation, expected_phrase(operation, entry))
    assert len(adapter.sent) == 1 and adapter.sent[0][1] == operation
    assert not any(value in json.dumps(out) for value in [*env.values(), "PRIVATE-MAC", "ENCODED"])
    assert all(value not in ({}, [], None, "") for value in out.values())
    if operation != "wifi_scan":
        spy.assert_awaited_once_with(entry, force=True)
    else:
        spy.assert_not_awaited()
        assert out == {"device": "desk-lamp", "network_count": 2, "radio": [{"channel": 6, "signal_level": 3}]}
    if operation == "pair_hub":
        reg.discover.assert_awaited_once()
        assert out["paired_count"] == 1
        assert out["paired"][0]["name"] == "New Sensor"
        assert out["new_devices"] == out["paired"]
    if operation == "pair_remove":
        assert out["removed"] is True
        with pytest.raises(NotFound):  # the hub no longer lists it, so the registry forgets it too
            reg.resolve_one("main-door")


@pytest.mark.parametrize("operation", LIFECYCLE + ["reset_reboot", "wifi_scan"])
@pytest.mark.parametrize("failure", [DeviceError("PRIVATE-NETWORK PRIVATE-PASS"), ValueError("PRIVATE-NETWORK PRIVATE-PASS"), AuthenticationError("PRIVATE-PASS"), TimeoutError()])
async def test_device_errors_name_target_without_leaking(house, env, operation, failure):
    reg, _, _ = house(tier=4)
    entry = reg.resolve_one("tapo-h200" if operation in {"pair_hub", "pair_remove"} else "desk-lamp")
    with pytest.raises(DeviceUnavailable) as exc:
        await action(reg, FakeLifecycle(failure=failure), operation, expected_phrase(operation, entry))
    assert entry.slug in str(exc.value)
    assert "PRIVATE" not in str(exc.value)


@pytest.mark.parametrize("operation,changes", [("pair_hub", {"timeout": 0}), ("pair_hub", {"timeout": 11}), ("reset_reboot", {"delay": 0}), ("reset_reboot", {"delay": 31}), ("pair_name", {"name": "bad\nname"}), ("pair_name", {"name": "é" * 33}), ("wifi_join", {"keytype": "evil"})])
async def test_inputs_are_validated_before_write(house, env, operation, changes):
    reg, _, _ = house(tier=4)
    entry = reg.resolve_one("tapo-h200" if operation == "pair_hub" else "desk-lamp")
    adapter = FakeLifecycle()
    with pytest.raises(RegistryError):
        await action(reg, adapter, operation, expected_phrase(operation, entry), **changes)
    assert not adapter.sent


async def test_wrong_hub_child_and_kind_are_refused(house):
    reg, _, _ = house(tier=4)
    adapter = FakeLifecycle()
    with pytest.raises(RegistryError, match="not a child"):
        await p.pair_remove(reg, adapter, reg.resolve_one("tapo-h200"), "desk-lamp", "pair_remove tapo-h200")
    with pytest.raises(Forbidden):
        await p.pair_hub(reg, adapter, reg.resolve_one("desk-lamp"), confirm="pair_hub desk-lamp")
    with pytest.raises(Forbidden):
        await p.reset(reg, adapter, reg.resolve_one("hallway-cam"), factory=True, confirm="reset_factory hallway-cam")
    assert not adapter.sent


async def test_explicit_operation_opt_in_and_read_at_level_zero(house, env):
    reg, _, _ = house(tier=0, allow_methods=["wifi_join"])
    adapter = FakeLifecycle()
    await action(reg, adapter, "wifi_scan")
    await action(reg, adapter, "wifi_join", "wifi_join desk-lamp")
    with pytest.raises(Forbidden):
        await action(reg, adapter, "pair_name", "pair_name desk-lamp")
    assert [s[1] for s in adapter.sent] == ["wifi_scan", "wifi_join"]


async def test_env_values_only_after_policy_and_never_in_errors(house, monkeypatch):
    reg, _, _ = house(tier=4)
    adapter = FakeLifecycle()
    for name in ["TOLPO_PROVISION_SSID", "TOLPO_PROVISION_WIFI_PASSWORD", "TOLPO_PROVISION_ACCOUNT", "TOLPO_PROVISION_ACCOUNT_PASSWORD"]:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(NeedsConfirmation):
        await action(reg, adapter, "wifi_join")
    with pytest.raises(RegistryError, match="environment variable"):
        await action(reg, adapter, "wifi_join", "wifi_join desk-lamp")
    assert not adapter.sent
    for invalid in ["actual password!", "x; echo secret", "$(secret)"]:
        monkeypatch.setenv(invalid, "PRIVATE-PASSWORD")
        with pytest.raises(RegistryError):
            p.env_value(invalid)


async def test_wifi_network_length_before_write(house, env, monkeypatch):
    reg, _, _ = house(tier=4)
    monkeypatch.setenv("TOLPO_PROVISION_SSID", "x" * 33)
    adapter = FakeLifecycle()
    with pytest.raises(RegistryError, match="32 bytes"):
        await action(reg, adapter, "wifi_join", "wifi_join desk-lamp")
    assert not adapter.sent


async def test_refresh_offline_is_reported(house):
    reg, _, devices = house(tier=4)
    devices["lamp"].update = AsyncMock(side_effect=TimeoutError())
    out = await action(reg, FakeLifecycle(), "reset_factory", "reset_factory desk-lamp")
    assert out["state"] == "offline" and out["changed"] == "factory_reset_requested"


@pytest.mark.parametrize("camera_hub", [False, True])
async def test_kasa_adapter_uses_public_child_setup_for_both_hubs(house, camera_hub):
    reg, _, devices = house()
    setup = SimpleNamespace(pair=AsyncMock(return_value=[]), unpair=AsyncMock(return_value={}))
    devices["hub"].modules[Module.ChildSetup] = setup
    # The common module key selects either dialect. No raw RPC in our adapter.
    if camera_hub:
        devices["hub"].protocol = SimpleNamespace(query=AsyncMock(side_effect=AssertionError("raw query forbidden")))
    entry = reg.resolve_one("tapo-h200")
    adapter = p.KasaLifecycle()
    assert await adapter.invoke(entry, "pair_hub", timeout=2) == []
    await adapter.invoke(entry, "pair_remove", device_id="child-door")
    setup.pair.assert_awaited_once_with(timeout=2)
    setup.unpair.assert_awaited_once_with("child-door")


async def test_adapter_name_scan_join_and_owner_account_checks(house):
    reg, _, devices = house()
    dev = devices["lamp"]
    dev.set_alias, dev.wifi_scan, dev.wifi_join = AsyncMock(), AsyncMock(return_value=[]), AsyncMock()
    dev.credentials = Credentials("owner", "owner-password")
    entry, adapter = reg.resolve_one("desk-lamp"), p.KasaLifecycle()
    await adapter.invoke(entry, "pair_name", name="New")
    await adapter.invoke(entry, "wifi_scan")
    await adapter.invoke(entry, "wifi_join", ssid="home", password="wifi-pass", keytype="wpa2_psk", username="owner", account_password="owner-password")
    dev.set_alias.assert_awaited_once_with("New")
    dev.wifi_scan.assert_awaited_once_with()
    dev.wifi_join.assert_awaited_once_with("home", "wifi-pass", "wpa2_psk")
    with pytest.raises(RegistryError, match="same owner's"):
        await adapter.invoke(entry, "wifi_join", username="different", account_password="wrong")
    with pytest.raises(NotFound):
        await adapter.invoke(reg.resolve_one("main-door"), "wifi_scan")


async def test_adapter_smart_only_reset_and_reboot():
    smart = SmartDevice("192.0.2.20")
    smart.factory_reset, smart.reboot = AsyncMock(), AsyncMock()
    adapter = p.KasaLifecycle()
    entry = Entry(smart, "smart")
    await adapter.invoke(entry, "reset_factory")
    await adapter.invoke(entry, "reset_reboot", delay=2)
    smart.factory_reset.assert_awaited_once_with()
    smart.reboot.assert_awaited_once_with(delay=2)
    camera = SmartCamDevice("192.0.2.21")
    camera.factory_reset, camera.reboot = AsyncMock(), AsyncMock()
    for operation in ("reset_factory", "reset_reboot"):
        with pytest.raises(NotFound, match="SMART"):
            await adapter.invoke(Entry(camera, "camera-hub"), operation, delay=1)
        with pytest.raises(NotFound):
            await adapter.invoke(Entry(smart, "child", root=entry), operation, delay=1)
    with pytest.raises(Forbidden):
        await adapter.invoke(entry, "unknown")


class FakeHost:
    def __init__(self, failure=None, restore_failure=False):
        self.events, self.failure, self.restore_failure = [], failure, restore_failure

    async def current(self):
        self.events.append("current")
        return "previous-private-network"

    async def join(self, network):
        self.events.append("join")
        if self.failure:
            raise self.failure

    async def restore(self, previous):
        assert previous == "previous-private-network"
        self.events.append("restore")
        if self.restore_failure:
            raise RegistryError("Host Wi-Fi command failed")


def setup_device():
    config = DeviceConfig("192.0.2.1", credentials=Credentials("PRIVATE-ACCOUNT", "PRIVATE-ACCOUNT-PASS"))
    dev = SmartDevice("192.0.2.1", config=config)
    dev._device_type = fakes.DeviceType.Plug
    dev._info = {"device_id": "expected-id", "model": "P115", "type": "SMART.TAPOPLUG"}
    dev.disconnect = AsyncMock()
    dev.update = AsyncMock()
    return dev


@pytest.mark.parametrize("tier,confirm,error", [(1, "pair_softap expected-id", Forbidden), (4, None, NeedsConfirmation), (4, "pair_softap wrong-plug", NeedsConfirmation)])
async def test_softap_gate_before_host_or_credentials(house, monkeypatch, tier, confirm, error):
    reg, _, _ = house(tier=tier)
    host = FakeHost()
    connector = SimpleNamespace(connect=AsyncMock())
    monkeypatch.setattr(p, "env_value", lambda *a, **k: pytest.fail("credentials read before authorization"))
    with pytest.raises(error):
        await p.softap_provision(reg, FakeLifecycle(), plan(), host, connector, confirm)
    assert host.events == []
    connector.connect.assert_not_awaited()


async def test_softap_plan_has_steps_without_contacts_or_private_values(house):
    reg, _, _ = house(tier=4)
    out = p.softap_plan(reg, plan(), "pair_softap expected-id")
    assert out["target"] == "new-plug" and len(out["steps"]) == 6
    assert "192.0.2.1" not in json.dumps(out) and "TOLPO_PROVISION_" not in json.dumps(out)
    reg.discover.assert_not_awaited()


@pytest.mark.parametrize("changes", [{"target": "bad target"}, {"ap_host": "8.8.8.8"}, {"ap_host": "127.0.0.1"}, {"ap_host": "$(host)"}, {"interface": "-evil"}, {"ssid_env": "password value!"}, {"device_id": "bad\nname"}, {"wait_seconds": 0}])
async def test_softap_input_validation_before_host(house, env, changes):
    reg, _, _ = house(tier=4)
    host = FakeHost()
    with pytest.raises(RegistryError):
        await p.softap_provision(reg, FakeLifecycle(), replace(plan(), **changes), host, confirm="pair_softap expected-id")
    assert not host.events


async def test_softap_end_to_end_restores_then_rescans_and_verifies(house, env, monkeypatch):
    reg, _, _ = house(tier=4)
    dev, host, adapter = setup_device(), FakeHost(), FakeLifecycle()
    connector = SimpleNamespace(connect=AsyncMock(return_value=dev))
    new = fakes.plug("expected-id", "New\nPlug")
    new.features["cloud_connection"] = fakes.FakeFeature("cloud_connection", fakes.T.BinarySensor, value=True)

    async def discover():
        assert host.events[-1] == "restore"
        reg._adopt(new)

    monkeypatch.setattr(reg, "discover", AsyncMock(side_effect=discover))
    out = await p.softap_provision(reg, adapter, plan(), host, connector, "pair_softap expected-id")
    assert host.events == ["current", "join", "restore"]
    connector.connect.assert_awaited_once_with("192.0.2.1", env["TOLPO_PROVISION_ACCOUNT"], env["TOLPO_PROVISION_ACCOUNT_PASSWORD"])
    dev.disconnect.assert_awaited_once()
    assert out["verified"] == "lan" and out["cloud_connected"] is True
    assert out["device"]["name"] == "New Plug" and out["new_devices"] == [out["device"]]
    assert not any(secret in json.dumps(out) for secret in [*env.values(), "previous-private-network"])
    assert adapter.sent[0][1] == "wifi_join"
    assert adapter.sent[0][2]["password"] == env["TOLPO_PROVISION_WIFI_PASSWORD"]


@pytest.mark.parametrize("where", ["join", "connect", "id", "camera", "send", "refresh", "disconnect", "cancel"])
async def test_softap_restores_after_every_failure(house, env, monkeypatch, where):
    reg, _, _ = house(tier=4)
    dev = setup_device()
    failure = ValueError("PRIVATE-ACCOUNT-PASS PRIVATE-NETWORK")
    host = FakeHost(failure=failure if where == "join" else None)
    if where == "id":
        dev._info["device_id"] = "not-expected"
    if where == "camera":
        dev = SmartCamDevice("192.0.2.1")
        dev.disconnect = AsyncMock()
    if where == "disconnect":
        dev.disconnect.side_effect = failure
    connector = SimpleNamespace(connect=AsyncMock(return_value=dev, side_effect=failure if where == "connect" else None))
    # A broken preflight check must fail promptly rather than spend the LAN
    # verification deadline waiting for a device this test never adopts.
    monkeypatch.setattr(reg, "discover", AsyncMock(side_effect=TimeoutError()))
    adapter = FakeLifecycle(failure=failure if where == "send" else asyncio.CancelledError() if where == "cancel" else None)
    if where == "refresh":
        monkeypatch.setattr(reg, "refresh", AsyncMock(side_effect=failure))
    expected = asyncio.CancelledError if where == "cancel" else (RegistryError, ValueError)
    with pytest.raises(expected) as exc:
        await p.softap_provision(reg, adapter, plan(), host, connector, "pair_softap expected-id")
    assert host.events[-1] == "restore"
    assert "PRIVATE" not in str(exc.value)
    if where not in {"connect", "join"}:
        dev.disconnect.assert_awaited_once()
    reg.discover.assert_not_awaited()


async def test_softap_reports_restore_failure_and_lan_timeout(house, env, monkeypatch):
    reg, _, _ = house(tier=4)
    connector = SimpleNamespace(connect=AsyncMock(return_value=setup_device()))
    with pytest.raises(RegistryError, match="Host Wi-Fi restore failed"):
        await p.softap_provision(reg, FakeLifecycle(), plan(), FakeHost(restore_failure=True), connector, "pair_softap expected-id")
    reg.discover.assert_not_awaited()
    monkeypatch.setattr(reg, "discover", AsyncMock(side_effect=TimeoutError()))
    host = FakeHost()
    with pytest.raises(DeviceUnavailable, match="LAN verification timed out"):
        await p.softap_provision(reg, FakeLifecycle(), plan(wait_seconds=1), host, connector, "pair_softap expected-id")
    assert host.events[-1] == "restore"


async def test_repeated_cancellation_cannot_interrupt_host_restore():
    started, finish = asyncio.Event(), asyncio.Event()
    completed = []

    async def restore(previous):
        started.set()
        await finish.wait()
        completed.append(previous)

    task = asyncio.create_task(p._restore(SimpleNamespace(restore=restore), "previous"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed == ["previous"]


async def test_pair_rescan_failure_reports_completed_pairing(house, monkeypatch):
    reg, _, _ = house(tier=4)
    monkeypatch.setattr(reg, "discover", AsyncMock(side_effect=TimeoutError()))
    with pytest.raises(DeviceUnavailable, match="tapo-h200 pairing completed.*rescan timed out"):
        await action(reg, FakeLifecycle(), "pair_hub", "pair_hub tapo-h200")


async def test_connector_uses_owner_credentials_and_no_retry(monkeypatch):
    discover = AsyncMock(side_effect=AuthenticationError("rejected"))
    monkeypatch.setattr(p.Discover, "discover_single", discover)
    with pytest.raises(AuthenticationError):
        await p.KasaConnector().connect("192.0.2.1", "owner", "owner-password")
    discover.assert_awaited_once_with("192.0.2.1", credentials=Credentials("owner", "owner-password"), discovery_timeout=5)


async def test_connector_authenticates_once_and_disconnects_on_failure(monkeypatch):
    dev = setup_device()
    discover = AsyncMock(return_value=dev)
    monkeypatch.setattr(p.Discover, "discover_single", discover)
    assert await p.KasaConnector().connect("192.0.2.1", "owner", "owner-password") is dev
    dev.update.assert_awaited_once_with()
    dev.disconnect.assert_not_awaited()
    dev.update.side_effect = AuthenticationError("rejected")
    with pytest.raises(AuthenticationError):
        await p.KasaConnector().connect("192.0.2.1", "owner", "owner-password")
    assert dev.update.await_count == 2
    dev.disconnect.assert_awaited_once_with()


async def test_connector_refuses_camera_before_login(monkeypatch):
    dev = SmartCamDevice("192.0.2.1")
    dev.update, dev.disconnect = AsyncMock(), AsyncMock()
    monkeypatch.setattr(p.Discover, "discover_single", AsyncMock(return_value=dev))
    with pytest.raises(RegistryError, match="SMART"):
        await p.KasaConnector().connect("192.0.2.1", "owner", "owner-password")
    dev.update.assert_not_awaited()
    dev.disconnect.assert_awaited_once_with()


async def test_softap_refuses_robot_family_before_write(house, env):
    reg, _, _ = house(tier=4)
    dev = setup_device()
    dev._device_type = fakes.DeviceType.Vacuum
    adapter, host = FakeLifecycle(), FakeHost()
    reg.discover.side_effect = TimeoutError()
    with pytest.raises(RegistryError, match="device family"):
        await p.softap_provision(reg, adapter, plan(), host, SimpleNamespace(connect=AsyncMock(return_value=dev)), "pair_softap expected-id")
    assert not adapter.sent and host.events[-1] == "restore"


@pytest.mark.parametrize("host_type", [p.MacOSWifi, p.LinuxWifi])
async def test_real_host_adapters_argv_and_restoration(host_type):
    calls = []
    previous = "Private Previous" if host_type is p.MacOSWifi else "12345678-1234-1234-1234-123456789abc"

    async def run(*args):
        calls.append(args)
        if "-getairportnetwork" in args:
            return f"Current Wi-Fi Network: {previous}"
        if "GENERAL.CON-UUID" in args:
            return previous
        return ""

    host = host_type("wlan0", run)
    saved = await host.current()
    await host.join("Tapo_Setup_TEST")
    await host.restore(saved)
    if host_type is p.MacOSWifi:
        assert calls[1] == ("/usr/sbin/networksetup", "-setairportnetwork", "wlan0", "Tapo_Setup_TEST")
        assert calls[2] == ("/usr/sbin/networksetup", "-setairportnetwork", "wlan0", previous)
        # The open setup network is not left in the host's auto-join list.
        assert calls[4] == ("/usr/sbin/networksetup", "-removepreferredwirelessnetwork", "wlan0", "Tapo_Setup_TEST")
        assert len(calls) == 5
    else:
        assert calls[2] == ("nmcli", "connection", "add", "type", "wifi", "ifname", "wlan0", "con-name", "tolpo-softap", "autoconnect", "no", "ssid", "Tapo_Setup_TEST")
        assert calls[3] == ("nmcli", "connection", "up", "id", "tolpo-softap", "ifname", "wlan0")
        assert calls[4] == ("nmcli", "connection", "up", "uuid", previous, "ifname", "wlan0")
        assert calls[-1] == ("nmcli", "connection", "delete", "id", "tolpo-softap")
        assert len(calls) == 7


@pytest.mark.parametrize("host_type", [p.MacOSWifi, p.LinuxWifi])
async def test_host_refuses_unrestorable_network_and_failed_restore(host_type):
    host = host_type("wlan0", AsyncMock(return_value="not connected"))
    with pytest.raises(RegistryError):
        await host.current()
    with pytest.raises(RegistryError):
        await host.restore("previous")
    with pytest.raises(RegistryError):
        host_type("bad; command")


async def test_system_tool_does_not_use_shell_or_expose_output(monkeypatch):
    proc = SimpleNamespace(communicate=AsyncMock(return_value=(b"PRIVATE-SSID", b"PRIVATE-PASSWORD")), returncode=1, kill=lambda: None)
    create = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(RegistryError) as exc:
        await p.system_command("tool", "argument with spaces")
    assert "PRIVATE" not in str(exc.value)
    assert create.call_args.args == ("tool", "argument with spaces")


async def test_sensitive_kasa_logs_are_suppressed_only_for_operation(house, env, caplog):
    reg, _, _ = house(tier=4)
    logger = logging.getLogger("kasa.tests.pairing")

    class LoggingAdapter(FakeLifecycle):
        async def invoke(self, entry, operation, **params):
            logger.warning("PRIVATE-WIFI-PASS PRIVATE-ACCOUNT-PASS")
            # This logger did not exist when private_logs installed filters.
            logging.getLogger("kasa.tests.lazy_pairing").warning("PRIVATE-NEW-LOGGER-PASS")
            return await super().invoke(entry, operation, **params)

    await action(reg, LoggingAdapter(), "wifi_join", "wifi_join desk-lamp")
    assert "PRIVATE" not in caplog.text
    logger.warning("normal log")
    assert "normal log" in caplog.text


@pytest.mark.parametrize("camera_hub", [False, True])
@pytest.mark.parametrize("detected", [False, True])
async def test_real_kasa_pair_flow_shapes_with_fake_module_wire(camera_hub, detected, monkeypatch):
    from kasa.smart.modules.childsetup import ChildSetup as SmartSetup
    from kasa.smartcam.modules.childsetup import ChildSetup as CameraSetup

    dev = SimpleNamespace(host="192.0.2.20", _children={"new-child": object()} if detected else {}, update=AsyncMock())
    category = [{"category": "subg.trigger"}]
    if camera_hub:
        # The owner's H200 answers without a childControl wrapper (fixtures/hub/H200).
        dev._last_update = {"getSupportChildDeviceCategory": {"device_category_list": category}}
        setup = CameraSetup(dev, "childQuickSetup")
    else:
        dev._last_update = {"get_support_child_device_category": {"device_category_list": category}}
        dev._parent = None
        setup = SmartSetup(dev, "child_quick_setup")
    setup._categories = ["subg.trigger"]
    rows = [{"device_id": "new-child", "device_model": "T110", "category": "subg.trigger"}] if detected else []
    method = "getScanChildDeviceList" if camera_hub else "get_scan_child_device_list"
    response = {"child_device_list": rows, "scan_status": "idle"}
    setup.call = AsyncMock(side_effect=lambda name, params=None: {method: response} if name == method else {})
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    assert await setup.pair(timeout=1) == rows
    expected = ["startScanChildDevice", method, "addScanChildDeviceList"] if camera_hub else ["begin_scanning_child_device", method, "add_child_device_list"]
    assert [call.args[0] for call in setup.call.await_args_list] == (expected if detected else expected[:2])
    if camera_hub:
        assert setup.call.await_args_list[0].args[1] == {"childControl": {"category": ["subg.trigger"]}}
        if detected:
            assert setup.call.await_args_list[2].args[1] == {"childControl": {"child_device_list": rows}}
    elif detected:
        assert setup.call.await_args_list[2].args[1] == response
    setup.call.reset_mock()
    await setup.unpair("new-child")
    name, payload = setup.call.await_args.args
    assert name == ("removeChildDeviceList" if camera_hub else "remove_child_device_list")
    assert payload == ({"childControl": {"child_device_list": [{"device_id": "new-child"}]}} if camera_hub else {"child_device_list": [{"device_id": "new-child"}]})


@pytest.mark.parametrize("camera", [False, True])
async def test_real_kasa_wifi_scan_and_join_with_fake_protocol(camera):
    import base64

    encode = lambda value: base64.b64encode(value.encode()).decode()
    cls = SmartCamDevice if camera else SmartDevice
    dev = cls("192.0.2.20", config=DeviceConfig("192.0.2.20", credentials=Credentials("owner", "account-password")))
    if camera:
        net = {"ssid": "fake-wifi", "auth": 1, "encryption": 3, "rssi": -40, "bssid": "fake-hardware-address"}
        query = AsyncMock(side_effect=[{"scanApList": {"onboarding": {"scan": {"ap_list": [net]}}}}, {}])
        # Test the request shape without exercising key handling.
        dev._encrypt_password = lambda password: "fake-encrypted-password"
    else:
        net = {"ssid": encode("fake-wifi"), "cipher_type": 1, "key_type": "wpa2_psk", "channel": 6, "signal_level": 3, "bssid": "fake-hardware-address"}
        query = AsyncMock(side_effect=[{"get_wireless_scan_info": {"ap_list": [net]}}, {}])
        dev._last_update = {"get_device_time": {"timestamp": 1, "time_diff": 0, "region": "Etc/UTC"}}
    dev.protocol.query = query
    adapter = p.KasaLifecycle()
    entry = Entry(dev, "test-device")
    networks = await adapter.invoke(entry, "wifi_scan")
    assert len(networks) == 1 and networks[0].ssid == "fake-wifi"
    await adapter.invoke(entry, "wifi_join", ssid="fake-wifi", password="wifi-password", keytype="wpa2_psk", username="owner", account_password="account-password")
    if camera:
        assert query.await_args_list[0].args[0] == {"scanApList": {"onboarding": {"scan": {}}}}
        assert query.await_args_list[1].args[0] == {"connectAp": {"onboarding": {"connect": {**net, "password": "fake-encrypted-password"}}}}
    else:
        assert query.await_args_list[0].args[0] == {"get_wireless_scan_info": {"start_index": 0}}
        assert query.await_args_list[1].args[0] == {"set_qs_info": {
            "account": {"username": encode("owner"), "password": encode("account-password")},
            "wireless": {"ssid": encode("fake-wifi"), "password": encode("wifi-password"), "key_type": "wpa2_psk"},
            "time": dev._last_update["get_device_time"],
        }}
    assert query.await_args_list[1].kwargs["retry_count"] == 0


async def test_real_kasa_smart_reset_reboot_with_fake_protocol():
    dev = SmartDevice("192.0.2.20")
    dev.protocol.query = AsyncMock()
    adapter, entry = p.KasaLifecycle(), Entry(dev, "smart")
    await adapter.invoke(entry, "reset_reboot", delay=2)
    await adapter.invoke(entry, "reset_factory")
    assert [call.args[0] for call in dev.protocol.query.await_args_list] == [{"device_reboot": {"delay": 2}}, "device_reset"]


async def test_fallback_mapping_blocks_defaults_and_blank_path_without_reading():
    original = {"fake": "fake-value"}
    guarded = p._NoFallbackMapping(original)
    assert guarded["fake"] == "fake-value"
    with p.private_logs():
        for access in (lambda: guarded["fake"], lambda: list(guarded.items()), lambda: len(guarded)):
            with pytest.raises(AuthenticationError, match="fallback is disabled"):
                access()
        from kasa.transports import aestransport, klaptransport
        with pytest.raises(AuthenticationError):
            aestransport.DEFAULT_CREDENTIALS["unused"]
        with pytest.raises(AuthenticationError):
            list(klaptransport.DEFAULT_CREDENTIALS.items())


@pytest.mark.parametrize("cached", [False, True])
async def test_aes_owner_login_rejection_never_attempts_another_login(cached):
    from kasa.exceptions import SmartErrorCode
    from kasa.transports.aestransport import AesTransport

    transport = AesTransport(config=DeviceConfig("192.0.2.20", credentials=Credentials("owner", "owner-password")))
    # All fallback values in this test are invented. Never read the library's.
    if cached:
        transport._default_credentials = Credentials("unused-fake", "unused-fake-password")
    transport.try_login = AsyncMock(side_effect=AuthenticationError("owner rejected", error_code=SmartErrorCode.LOGIN_ERROR))
    transport.perform_handshake = AsyncMock()
    entry = Entry(SimpleNamespace(protocol=SimpleNamespace(_transport=transport)), "test")
    with p.private_logs():
        p._guard_cached_credentials(entry)
        with pytest.raises(AuthenticationError, match="fallback is disabled"):
            await transport.perform_login()
    assert transport.try_login.await_count == 1
    if not cached:
        transport.perform_handshake.assert_not_awaited()


async def test_camera_constructor_and_cached_fallback_cannot_use_defaults():
    from kasa.transports.sslaestransport import SslAesTransport

    with p.private_logs():
        transport = SslAesTransport(config=DeviceConfig("192.0.2.20", credentials=Credentials("owner", "owner-password")))
        credentials = transport._default_credentials
        assert isinstance(credentials, p._NoFallbackCredentials) and credentials.original is None
        with pytest.raises(AuthenticationError):
            _ = credentials.password
        with pytest.raises(AuthenticationError):
            _ = credentials.username
        transport._default_credentials = Credentials("unused-fake", "unused-fake-password")
        p._guard_cached_credentials(Entry(SimpleNamespace(protocol=SimpleNamespace(_transport=transport)), "camera"))
        with pytest.raises(AuthenticationError):
            _ = transport._default_credentials.password


async def test_klap_rejection_cannot_accept_default_or_blank_identity():
    from kasa.transports.klaptransport import KlapTransport

    transport = KlapTransport(config=DeviceConfig("192.0.2.20", credentials=Credentials("owner", "owner-password")))
    transport._http_client.post = AsyncMock(return_value=(200, b"\x00" * 48))
    with p.private_logs():
        with pytest.raises(AuthenticationError, match="fallback is disabled"):
            await transport.perform_handshake1()
    assert transport._http_client.post.await_count == 1


async def test_mcp_all_tools_have_docs_types_levels_and_execute(house, env, mcp, monkeypatch):
    reg, caller, _ = house(tier=4)
    adapter = FakeLifecycle()
    monkeypatch.setattr(p, "KasaLifecycle", lambda: adapter)
    host = FakeHost()
    monkeypatch.setattr(p, "host_wifi", lambda interface: host)
    connector = SimpleNamespace(connect=AsyncMock(return_value=setup_device()))
    monkeypatch.setattr(p, "KasaConnector", lambda: connector)
    original_discover = reg.discover

    async def discover():
        if host.events:
            reg._adopt(fakes.plug("expected-id", "New Plug"))
        return await original_discover()

    monkeypatch.setattr(reg, "discover", AsyncMock(side_effect=discover))
    args = {
        "pair_hub": {"device": "tapo-h200", "timeout": 1, "confirm": "pair_hub tapo-h200"},
        "pair_remove": {"device": "tapo-h200", "child": "main-door", "confirm": "pair_remove main-door"},
        "pair_name": {"device": "desk-lamp", "name": "Renamed Plug", "confirm": "pair_name desk-lamp"},
        "wifi_scan": {"device": "desk-lamp"},
        "wifi_join": {"device": "desk-lamp", "ssid_env": "TOLPO_PROVISION_SSID", "password_env": "TOLPO_PROVISION_WIFI_PASSWORD", "account_env": "TOLPO_PROVISION_ACCOUNT", "account_password_env": "TOLPO_PROVISION_ACCOUNT_PASSWORD", "confirm": "wifi_join desk-lamp"},
        "reset_factory": {"device": "desk-lamp", "confirm": "reset_factory desk-lamp"},
        "reset_reboot": {"device": "desk-lamp"},
        "pair_softap_plan": {"plan": vars(plan()), "confirm": "pair_softap expected-id"},
        "pair_softap_provision": {"plan": vars(plan()), "confirm": "pair_softap expected-id"},
    }
    doc = (Path(__file__).parents[2] / "docs/areas/pairing.md").read_text()
    async with mcp(reg, caller) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name, arguments in args.items():
            tool = tools[name]
            assert tool.description and tool.output_schema
            assert f"- `{name}`:" in doc
            assert tool.annotations.read_only_hint == (name == "wifi_scan")
            result = await client.call_tool(name, arguments)
            assert not result.is_error, result.content
            assert not any(secret in json.dumps(result.structured_content) for secret in env.values())


async def test_mcp_refusals_and_validation_do_not_call_devices(house, mcp, monkeypatch):
    reg, caller, _ = house(tier=1)
    adapter = FakeLifecycle()
    monkeypatch.setattr(p, "KasaLifecycle", lambda: adapter)
    async with mcp(reg, caller) as client:
        for name, args in [("pair_hub", {"device": "tapo-h200"}), ("reset_reboot", {"device": "desk-lamp"}), ("pair_softap_provision", {"plan": vars(plan())}), ("pair_hub", {"device": "tapo-h200", "timeout": 100})]:
            result = await client.call_tool(name, args)
            assert result.is_error
    assert not adapter.sent


async def test_pair_remove_phrase_names_the_child_and_spares_siblings(house):
    reg, _, _ = house(tier=4)
    hub = reg.resolve_one("tapo-h200")
    adapter = FakeLifecycle()
    # A phrase for the hub, or for a sibling, never removes this child.
    for wrong in ("pair_remove tapo-h200", "pair_remove temp-sensor"):
        with pytest.raises(NeedsConfirmation):
            await p.pair_remove(reg, adapter, hub, "main-door", wrong)
    assert adapter.sent == []
    out = await p.pair_remove(reg, adapter, hub, "main-door", "pair_remove main-door")
    assert out["removed"] is True
    assert adapter.sent == [("tapo-h200", "pair_remove", {"device_id": "child-door"})]
    sibling = reg.resolve_one("temp-sensor")
    assert sibling.online and sibling.root is hub and sibling.device in hub.device.children


async def test_hub_reset_targets_only_the_hub(house):
    reg, _, _ = house(tier=4)
    hub = reg.resolve_one("tapo-h200")
    children = {e.slug for e in reg.entries() if e.root is hub}
    adapter = FakeLifecycle()
    await p.reset(reg, adapter, hub, factory=True, confirm="reset_factory tapo-h200")
    assert adapter.sent == [("tapo-h200", "reset_factory", {"delay": 1})]
    assert {e.slug for e in reg.entries() if e.root is hub} == children


@pytest.mark.parametrize("ref", ["Desk Lamp", "desk lamp", "desk*", "*", "main door"])
async def test_lifecycle_targets_need_exact_ids_not_device_text(house, mcp, monkeypatch, ref):
    reg, caller, _ = house(tier=4)
    with pytest.raises(NotFound, match="exact device id"):
        p.exact_entry(reg, ref)
    assert p.exact_entry(reg, "p1") is p.exact_entry(reg, "desk-lamp")
    adapter = FakeLifecycle()
    monkeypatch.setattr(p, "KasaLifecycle", lambda: adapter)
    async with mcp(reg, caller) as client:
        for name, args in [
            ("reset_factory", {"device": ref, "confirm": f"reset_factory {ref}"}),
            ("reset_factory", {"device": ref, "confirm": "reset_factory desk-lamp"}),
            ("pair_remove", {"device": "tapo-h200", "child": ref, "confirm": "pair_remove main-door"}),
            ("pair_name", {"device": ref, "name": "x", "confirm": "pair_name desk-lamp"}),
        ]:
            assert (await client.call_tool(name, args)).is_error
    assert adapter.sent == []


@pytest.mark.parametrize("name", ["TAPO_PASSWORD", "AWS_SECRET_ACCESS_KEY", "TOLPO_PROVISION_", "tolpo_provision_x", "TOLPO_PROVISION_A B"])
async def test_only_owner_provisioning_variables_can_be_read(house, env, monkeypatch, name):
    reg, _, _ = house(tier=4)
    monkeypatch.setenv("TAPO_PASSWORD", "PRIVATE-SERVER-PASSWORD")
    adapter, host = FakeLifecycle(), FakeHost()
    entry = reg.resolve_one("desk-lamp")
    for args in [
        (name, "TOLPO_PROVISION_WIFI_PASSWORD", "TOLPO_PROVISION_ACCOUNT", "TOLPO_PROVISION_ACCOUNT_PASSWORD"),
        ("TOLPO_PROVISION_SSID", "TOLPO_PROVISION_WIFI_PASSWORD", "TOLPO_PROVISION_ACCOUNT", name),
    ]:
        with pytest.raises(RegistryError, match="TOLPO_PROVISION_") as exc:
            await p.wifi_join(reg, adapter, entry, *args, confirm="wifi_join desk-lamp")
        assert "PRIVATE" not in str(exc.value)
    for field in ("ap_env", "ssid_env", "wifi_password_env", "account_env", "account_password_env"):
        with pytest.raises(RegistryError, match="TOLPO_PROVISION_"):
            await p.softap_provision(reg, adapter, replace(plan(), **{field: name}), host, confirm="pair_softap expected-id")
    assert adapter.sent == [] and host.events == []


async def test_softap_phrase_names_the_expected_device_not_the_label(house, env):
    reg, _, _ = house(tier=4)
    host, connector = FakeHost(), SimpleNamespace(connect=AsyncMock())
    for wrong in ("pair_softap new-plug", "pair_softap other-id"):
        with pytest.raises(NeedsConfirmation, match="pair_softap expected-id"):
            await p.softap_provision(reg, FakeLifecycle(), plan(), host, connector, wrong)
    assert host.events == []
    connector.connect.assert_not_awaited()


def _broken_module(monkeypatch):
    module = types.ModuleType("kasa.transports.futuretransport")
    module.get_default_credentials = lambda value: value
    monkeypatch.setitem(sys.modules, module.__name__, module)


def _missing_mapping(monkeypatch):
    from kasa.transports import aestransport

    monkeypatch.delattr(aestransport, "DEFAULT_CREDENTIALS")


def _missing_decoder(monkeypatch):
    from kasa.transports import sslaestransport

    monkeypatch.delattr(sslaestransport, "get_default_credentials")


@pytest.mark.parametrize("change", [_broken_module, _missing_mapping, _missing_decoder])
async def test_owner_only_guard_fails_closed_when_kasa_internals_change(house, env, monkeypatch, change):
    reg, _, _ = house(tier=4)
    change(monkeypatch)
    adapter, host = FakeLifecycle(), FakeHost()
    connector = SimpleNamespace(connect=AsyncMock())
    with pytest.raises(p.GuardUnavailable, match="Owner-only sign-in cannot be enforced"):
        await action(reg, adapter, "reset_factory", "reset_factory desk-lamp")
    with pytest.raises(p.GuardUnavailable):
        await action(reg, adapter, "wifi_scan")
    with pytest.raises(p.GuardUnavailable):
        await p.softap_provision(reg, adapter, plan(), host, connector, "pair_softap expected-id")
    with pytest.raises(p.GuardUnavailable):
        with p.private_logs():
            pass
    assert adapter.sent == [] and host.events == []
    connector.connect.assert_not_awaited()


async def test_cached_fallback_guard_fails_closed_when_attributes_move():
    from kasa.protocols import SmartProtocol
    from kasa.transports.aestransport import AesTransport

    transport = AesTransport(config=DeviceConfig("192.0.2.20", credentials=Credentials("owner", "owner-password")))
    del transport._default_credentials
    with pytest.raises(p.GuardUnavailable):
        p._guard_cached_credentials(Entry(SimpleNamespace(protocol=SimpleNamespace(_transport=transport)), "renamed-cache"))
    protocol = SmartProtocol(transport=AesTransport(config=DeviceConfig("192.0.2.20")))
    del protocol._transport
    with pytest.raises(p.GuardUnavailable):
        p._guard_cached_credentials(Entry(SimpleNamespace(protocol=protocol), "moved-transport"))


@pytest.mark.parametrize("host_type", [p.MacOSWifi, p.LinuxWifi])
async def test_host_forgets_setup_network_even_when_restore_fails(host_type):
    calls = []
    previous = "Private Previous" if host_type is p.MacOSWifi else "12345678-1234-1234-1234-123456789abc"

    async def run(*args):
        calls.append(args)
        if "-getairportnetwork" in args or "GENERAL.CON-UUID" in args:
            return "not connected"
        return ""

    host = host_type("wlan0", run)
    await host.join("Tapo_Setup_TEST")
    with pytest.raises(RegistryError):
        await host.restore(previous)
    assert calls[-1][1] == "-removepreferredwirelessnetwork" or calls[-1][:3] == ("nmcli", "connection", "delete")

    async def forget_fails(*args):
        if "-removepreferredwirelessnetwork" in args or args[:3] == ("nmcli", "connection", "delete"):
            raise RegistryError("Host Wi-Fi command failed")
        if "-getairportnetwork" in args:
            return f"Current Wi-Fi Network: {previous}"
        return previous if "GENERAL.CON-UUID" in args else ""

    host = host_type("wlan0", forget_fails)
    await host.join("Tapo_Setup_TEST")
    with pytest.raises(RegistryError, match="restored, but"):
        await host.restore(previous)
