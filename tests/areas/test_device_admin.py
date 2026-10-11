"""Offline tests for settings, firmware, and local room persistence."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from kasa import Module
from kasa.exceptions import DeviceError

from tolpo.areas import device_admin as admin
from tolpo.calls import NeedsConfirmation
from tolpo.registry import DeviceUnavailable, Forbidden, NotFound, RegistryError, Unsupported

pytestmark = pytest.mark.anyio

AUTO = {"enable": False, "time": 180, "random_range": 120}
LATEST = {"type": 1, "need_to_upgrade": True, "fw_ver": "2.0", "release_date": "2026-01-01",
          "release_note": "Fixes\n and\x00 improvements", "fw_size": 200, "oem_id": "PRIVATE"}
LED = {"led_rule": "always", "night_mode_type": "custom", "start_time": 1320, "end_time": 360,
       "sunrise_offset": 0, "sunset_offset": 0}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("TOLPO_STATE_DIR", str(tmp_path))
    return tmp_path / "rooms.json"


@pytest.fixture
def house(make_house):
    def make(tier=4, **config):
        reg, caller, devices = make_house(tier=tier, **config)
        for device in devices.values():
            device.internal_state = {"get_device_info": {"fw_ver": "1.0"}}
            device.modules.update({Module.Time: SimpleNamespace(), Module.Led: SimpleNamespace(data={"led_rule": "always", "led_status": True, "night_mode": {k: v for k, v in LED.items() if k != "led_rule"}}),
                                   Module.Firmware: SimpleNamespace(supported_version=2, firmware_update_info=None)})
        caller.respond("get_device_info", {"fw_ver": "1.0", "hw_ver": "1.1", "model": "P115"})
        caller.respond("get_auto_update_info", dict(AUTO))
        caller.respond("get_latest_fw", dict(LATEST))
        caller.respond("get_device_time", {"timestamp": 1704067200, "time_diff": 60, "region": "Europe/Berlin"})
        caller.respond("component_nego", {"component_list": [{"id": "firmware", "ver_code": 2}]})
        caller.respond("get_fw_download_state", {"status": 2, "download_progress": 30, "upgrade_time": 5,
                                                "reboot_time": 5, "auto_upgrade": False})
        caller.respond("getTimezone", {"system": {"basic": {"timezone": "UTC+01:00", "zone_id": "Europe/Berlin"}}})
        caller.respond("getClockStatus", {"system": {"clock_status": {"seconds_from_1970": 1704067200}}})
        caller.respond("getLedStatus", {"led": {"config": {"enabled": "on"}}})
        caller.respond("getDeviceInfo", {"device_info": {"basic_info": {"device_model": "C225", "sw_version": "1.0", "hw_version": "1.1"}}})
        caller.respond("getAppComponentList", {"app_component": {"app_component_list": [{"name": "led", "version": 1}]}})
        return reg, caller, devices
    return make


async def test_rename_wire_refresh_and_stable_slug(house, monkeypatch):
    reg, caller, devices = house()
    refreshed = []
    original = reg.refresh

    async def refresh(entry, **kwargs):
        refreshed.append((entry.slug, kwargs))
        await original(entry, **kwargs)
    monkeypatch.setattr(reg, "refresh", refresh)
    caller.respond("set_device_info", lambda entry, params: setattr(entry.device, "alias", base64.b64decode(params["nickname"]).decode()) or {})
    result = await admin.rename_device(reg, caller, "desk-lamp", "New\nLamp\x00")
    assert result == {"device": "desk-lamp", "changed": {"name": "New Lamp"}}
    assert caller.sent == [("desk-lamp", "set_device_info", {"nickname": "TmV3IExhbXA="})]
    assert refreshed == [("desk-lamp", {"force": True})]
    assert reg.resolve_one("New Lamp").slug == "desk-lamp"
    assert devices["lamp"].alias == "New Lamp"


async def test_camera_rename_uses_kasa_shape(house):
    reg, caller, _ = house()
    await admin.rename_device(reg, caller, "hallway-cam", "Camera")
    assert caller.sent[-1] == ("hallway-cam", "setDeviceAlias", {"system": {"sys": {"dev_alias": "Camera"}}})


async def test_metadata_icon_and_coordinate_scale_without_coordinate_output(house):
    reg, caller, _ = house()
    result = await admin.set_metadata(reg, caller, "desk-lamp", "plug", 51.25, -1.5)
    assert caller.sent[-1][2] == {"avatar": "plug", "latitude": 512500, "longitude": -15000}
    assert result["changed"] == {"icon": "plug", "location": "updated"}


@pytest.mark.parametrize("kwargs", [{}, {"icon": "http://example.test/x"}, {"latitude": 10}, {"latitude": 91, "longitude": 0},
                                    {"latitude": float("nan"), "longitude": 0}, {"icon": "x\nignore"}])
async def test_metadata_validation_before_wire(house, kwargs):
    reg, caller, _ = house()
    with pytest.raises(RegistryError):
        await admin.set_metadata(reg, caller, "desk-lamp", **kwargs)
    assert caller.sent == []


@pytest.mark.parametrize("name", ["", "\n\x00", "x" * 65])
async def test_name_validation(house, name):
    reg, caller, _ = house()
    with pytest.raises(RegistryError):
        await admin.rename_device(reg, caller, "desk-lamp", name)
    assert caller.sent == []


async def test_clock_smart_read_and_write(house):
    reg, caller, _ = house()
    assert await admin.device_clock(reg, caller, "desk-lamp") == {"device": "desk-lamp", "timestamp": 1704067200, "time_diff": 60, "region": "Europe/Berlin"}
    await admin.device_clock(reg, caller, "desk-lamp", "Europe/Berlin", 1704067200)
    assert caller.sent[-1][1:] == ("set_device_time", {"timestamp": 1704067200, "time_diff": 60, "region": "Europe/Berlin"})


async def test_clock_camera_read_zone_and_reject_clock(house):
    reg, caller, _ = house()
    result = await admin.device_clock(reg, caller, "hallway-cam")
    assert result == {"device": "hallway-cam", "timezone": "UTC+01:00", "zone_id": "Europe/Berlin", "seconds_from_1970": 1704067200}
    await admin.device_clock(reg, caller, "hallway-cam", "Etc/UTC")
    assert caller.sent[-1][1:] == ("setTimezone", {"system": {"basic": {"timezone": "UTC+00:00", "zone_id": "Etc/UTC"}}})
    count = len(caller.sent)
    with pytest.raises(Unsupported):
        await admin.device_clock(reg, caller, "hallway-cam", "Etc/UTC", 1704067200)
    assert len(caller.sent) == count


@pytest.mark.parametrize("zone, stamp", [("not/a/zone", None), ("Etc/UTC", -1), ("Etc/UTC", True), ("Etc/UTC", 99999999999999999999)])
async def test_clock_validation(house, zone, stamp):
    reg, caller, _ = house()
    with pytest.raises(RegistryError):
        await admin.device_clock(reg, caller, "desk-lamp", zone, stamp)
    assert caller.sent == []


@pytest.mark.parametrize("nested", [False, True])
async def test_led_cached_read_and_schedule_preserves_response_layout(house, nested):
    reg, caller, devices = house()
    devices["lamp"].modules[Module.Led].data = {"led_rule": "always", "night_mode": {k: v for k, v in LED.items() if k != "led_rule"}} if nested else dict(LED)
    result = await admin.device_led(reg, caller, "desk-lamp")
    assert result["led_rule"] == "always" and caller.sent == []
    await admin.device_led(reg, caller, "desk-lamp", "night_mode", 1320, 360)
    method, params = caller.sent[-1][1:]
    assert method == "set_led_info" and params["led_rule"] == "night_mode"
    schedule = params["night_mode"] if nested else params
    assert schedule["start_time"] == 1320 and schedule["end_time"] == 360 and schedule["night_mode_type"] == "custom"
    await admin.device_led(reg, caller, "desk-lamp", "never")
    assert caller.sent[-1][2]["led_rule"] == "never"


async def test_camera_led_and_refused_schedule(house):
    reg, caller, _ = house()
    assert await admin.device_led(reg, caller, "hallway-cam") == {"device": "hallway-cam", "enabled": "on"}
    await admin.device_led(reg, caller, "hallway-cam", "never")
    assert caller.sent[-1][1:] == ("setLedStatus", {"led": {"config": {"enabled": "off"}}})
    count = len(caller.sent)
    with pytest.raises(Unsupported):
        await admin.device_led(reg, caller, "hallway-cam", "night_mode", 10, 20)
    assert len(caller.sent) == count


@pytest.mark.parametrize("rule,start,end", [("auto", None, None), ("never", 0, 10), ("night_mode", -1, 10), ("night_mode", 10, 1440),
                                         ("night_mode", 10, None), ("night_mode", 10, 10), ("night_mode", True, 5)])
async def test_led_validation(house, rule, start, end):
    reg, caller, _ = house()
    with pytest.raises(RegistryError):
        await admin.device_led(reg, caller, "desk-lamp", rule, start, end)
    assert caller.sent == []


async def test_firmware_read_cached_notes_check_and_progress(house):
    reg, caller, devices = house(0)
    devices["lamp"].modules[Module.Firmware].firmware_update_info = SimpleNamespace(status=1, needs_upgrade=True, version="2.0", release_notes="fix\nnow", release_date=None)
    result = await admin.firmware_info(reg, caller, "desk-lamp")
    assert result["auto_update"] == AUTO and result["fw_ver"] == "1.0" and result["latest_cached"]["release_note"] == "fix now"
    assert "get_latest_fw" not in caller.methods_sent()
    check = await admin.firmware_info(reg, caller, "desk-lamp", check=True)
    assert check["release_note"] == "Fixes and improvements" and "oem_id" not in check
    progress = await admin.firmware_info(reg, caller, "desk-lamp", progress=True)
    assert progress["download_progress"] == 30 and progress["auto_upgrade"] is False


AUTO_OK = "set_auto_update_info desk-lamp"


async def test_firmware_auto_preserves_omitted_fields(house):
    reg, caller, _ = house()
    await admin.firmware_auto_update(reg, caller, "desk-lamp", True, confirm=AUTO_OK)
    assert caller.sent[-1][1:] == ("set_auto_update_info", {**AUTO, "enable": True})
    await admin.firmware_auto_update(reg, caller, "desk-lamp", False, 0, 0, confirm=AUTO_OK)
    assert caller.sent[-1][2] == {"enable": False, "time": 0, "random_range": 0}


async def test_firmware_auto_update_is_level_four_and_confirmed_before_any_wire_call(house):
    reg, caller, _ = house(3)
    with pytest.raises(Forbidden):
        await admin.firmware_auto_update(reg, caller, "desk-lamp", True, confirm=AUTO_OK)
    assert caller.sent == []
    reg, caller, _ = house()
    for confirm in (None, "set_auto_update_info office-1", "fw_download desk-lamp"):
        with pytest.raises(NeedsConfirmation):
            await admin.firmware_auto_update(reg, caller, "desk-lamp", False, confirm=confirm)
        assert caller.sent == []


@pytest.mark.parametrize("enabled,time,spread", [("true", None, None), (True, -1, None), (True, None, 1440), (True, True, None)])
async def test_firmware_window_validation(house, enabled, time, spread):
    reg, caller, _ = house()
    with pytest.raises(RegistryError):
        await admin.firmware_auto_update(reg, caller, "desk-lamp", enabled, time, spread)
    assert caller.sent == []


async def test_firmware_install_requires_device_confirmation_version_and_available_update(house):
    reg, caller, _ = house()
    for confirm in (None, "fw_download office-1", "yes"):
        with pytest.raises(NeedsConfirmation):
            await admin.firmware_install(reg, caller, "desk-lamp", "2.0", confirm)
        assert caller.sent == []
    with pytest.raises(RegistryError):
        await admin.firmware_install(reg, caller, "desk-lamp", "3.0", "fw_download desk-lamp")
    assert "fw_download" not in caller.methods_sent()
    caller.respond("get_latest_fw", {**LATEST, "type": 0})
    with pytest.raises(RegistryError):
        await admin.firmware_install(reg, caller, "desk-lamp", "2.0", "fw_download desk-lamp")
    caller.respond("get_latest_fw", LATEST)
    result = await admin.firmware_install(reg, caller, "desk-lamp", "2.0", "fw_download desk-lamp")
    assert caller.sent[-1] == ("desk-lamp", "fw_download", {})
    assert result["changed"]["firmware_install"] == "started"
    assert "completion" in result["status"]


WRITE_CASES = [
    (admin.rename_device, "desk-lamp", ("Renamed",), "set_device_info"),
    (admin.rename_device, "hallway-cam", ("Renamed",), "setDeviceAlias"),
    (admin.set_metadata, "desk-lamp", ("plug",), "set_device_info"),
    (admin.device_clock, "desk-lamp", ("Etc/UTC",), "set_device_time"),
    (admin.device_clock, "hallway-cam", ("Etc/UTC",), "setTimezone"),
    (admin.device_led, "desk-lamp", ("never",), "set_led_info"),
    (admin.device_led, "hallway-cam", ("never",), "setLedStatus"),
    (admin.firmware_auto_update, "desk-lamp", (True, None, None, "set_auto_update_info desk-lamp"), "set_auto_update_info"),
    (admin.firmware_install, "desk-lamp", ("2.0", "fw_download desk-lamp"), "fw_download"),
]


@pytest.mark.parametrize("fn,device,args,method", WRITE_CASES)
async def test_every_device_write_refused_below_its_level(house, fn, device, args, method):
    reg, caller, _ = house(1)
    with pytest.raises(Forbidden) as caught:
        await fn(reg, caller, device, *args)
    assert method not in caller.methods_sent()
    if method in ("fw_download", "set_auto_update_info"):
        assert caller.sent == []
    assert not any(s in str(caught.value) for s in ("allow_methods", "TOLPO_", "config"))


READ_CASES = [
    (admin.device_clock, "desk-lamp", (), "get_device_time"),
    (admin.device_clock, "hallway-cam", (), "getTimezone"),
    (admin.device_clock, "hallway-cam", (), "getClockStatus"),
    (admin.device_led, "hallway-cam", (), "getLedStatus"),
    (admin.firmware_info, "desk-lamp", (), "get_device_info"),
    (admin.firmware_info, "desk-lamp", (), "get_auto_update_info"),
    (admin.firmware_info, "desk-lamp", (True,), "get_latest_fw"),
    (admin.firmware_info, "desk-lamp", (False, True), "get_fw_download_state"),
    (admin.diagnostics, "desk-lamp", (), "component_nego"),
    (admin.diagnostics, "hallway-cam", (), "getDeviceInfo"),
    (admin.diagnostics, "hallway-cam", (), "getAppComponentList"),
]


@pytest.mark.parametrize("fn,device,args,method", WRITE_CASES + READ_CASES)
async def test_device_failures_identify_device_and_action_without_raw_values(house, fn, device, args, method):
    reg, caller, _ = house()
    caller.respond(method, DeviceError("SECRET_TOKEN ssid=PRIVATE", error_code=None))
    with pytest.raises(DeviceUnavailable) as caught:
        await fn(reg, caller, device, *args)
    message = str(caught.value)
    assert device in message and method in message and "error" in message
    assert "SECRET_TOKEN" not in message and "PRIVATE" not in message


async def test_diagnostics_allowlist_flattens_text_and_keeps_false_and_zero(house):
    reg, caller, _ = house(0)
    secrets = {"ssid": "SECRET_WIFI", "mac": "SECRET_MAC", "token": "SECRET_TOKEN", "homekit_setup_code": "SECRET_CODE", "latitude": 123, "nickname": "BAD NAME"}
    caller.respond("get_device_info", {**secrets, "fw_ver": "1.0\n test", "rssi": -50, "signal_level": 0, "uptime": 120, "on_time": 30})
    caller.respond("component_nego", {**secrets, "component_list": [{"id": "firmware\n test", "ver_code": 2, **secrets}]})
    result = await admin.diagnostics(reg, caller, "desk-lamp")
    assert result == {"device": "desk-lamp", "fw_ver": "1.0 test", "rssi": -50, "signal_level": 0, "uptime": 120, "on_time": 30,
                      "components": [{"id": "firmware test", "ver_code": 2}]}
    assert "SECRET" not in json.dumps(result)
    caller.respond("get_device_time", {**secrets, "timestamp": 0, "region": ""})
    assert await admin.device_clock(reg, caller, "desk-lamp") == {"device": "desk-lamp", "timestamp": 0}
    caller.respond("get_latest_fw", {**secrets, "type": 0, "need_to_upgrade": False, "release_note": "X\n" * 2000})
    result = await admin.firmware_info(reg, caller, "desk-lamp", True)
    assert result["need_to_upgrade"] is False and len(result["release_note"]) == 1200 and "SECRET" not in json.dumps(result)


async def test_led_cached_data_hides_unknown_fields(house):
    reg, caller, devices = house(0)
    devices["lamp"].modules[Module.Led].data.update(token="SECRET", night_mode={"start_time": 0, "ssid": "SECRET"})
    result = await admin.device_led(reg, caller, "desk-lamp")
    assert "SECRET" not in json.dumps(result) and result["night_mode"] == {"start_time": 0}
    assert caller.sent == []


async def test_unsupported_modules_and_legacy_protocol_do_not_send_guessed_methods(house):
    reg, caller, devices = house()
    devices["lamp"].modules.clear()
    for fn in (admin.firmware_info, admin.device_led, admin.device_clock):
        with pytest.raises(Unsupported):
            await fn(reg, caller, "desk-lamp")
    with pytest.raises(Unsupported):
        await admin.firmware_info(reg, caller, "hallway-cam")
    devices["lamp"].internal_state = {"system": {"get_sysinfo": {}}}
    with pytest.raises(Unsupported):
        await admin.rename_device(reg, caller, "desk-lamp", "New")
    assert caller.sent == []


async def test_firmware_v1_no_auto_methods(house):
    reg, caller, devices = house()
    devices["lamp"].modules[Module.Firmware].supported_version = 1
    result = await admin.firmware_info(reg, caller, "desk-lamp")
    assert "auto_update" not in result and "get_auto_update_info" not in caller.methods_sent()
    with pytest.raises(Unsupported):
        await admin.firmware_auto_update(reg, caller, "desk-lamp", True, confirm=AUTO_OK)


async def test_unavailable_after_successful_write_reported(house, monkeypatch):
    reg, caller, _ = house()
    async def refresh(entry, **kwargs):
        entry.online = False
    monkeypatch.setattr(reg, "refresh", refresh)
    result = await admin.rename_device(reg, caller, "desk-lamp", "New")
    assert "not verified" in result["refresh"]


async def test_local_rooms_and_groups_persist_case_insensitive_and_assign_exact_slugs(house, state):
    reg, caller, _ = house()
    for kind in ("rooms", "groups"):
        assert admin.local_collection(reg, kind, "list") == {kind: []}
        admin.local_collection(reg, kind, "create", "Living\nRoom")
        result = admin.local_collection(reg, kind, "assign", "living room", devices=["Desk Lamp", "desk-lamp", "office-1"])
        assert result["devices"] == ["desk-lamp", "office-1"]
        admin.local_collection(reg, kind, "rename", "LIVING ROOM", "Office")
        assert admin.local_collection(reg, kind, "list") == {kind: [{"name": "Office", "devices": ["desk-lamp", "office-1"]}]}
    assert admin.resolve_room("office") == ["desk-lamp", "office-1"]
    assert state.stat().st_mode & 0o777 == 0o600
    assert state.with_suffix(".lock").stat().st_mode & 0o777 == 0o600
    assert json.loads(state.read_text())["version"] == 1
    assert caller.sent == []
    for kind in ("rooms", "groups"):
        admin.local_collection(reg, kind, "delete", "Office")
    with pytest.raises(NotFound):
        admin.resolve_room("Office")


async def test_rooms_single_membership_groups_multiple(house, state):
    reg, caller, _ = house()
    for kind in ("rooms", "groups"):
        for name in ("A", "B"):
            admin.local_collection(reg, kind, "create", name)
            admin.local_collection(reg, kind, "assign", name, devices=["desk-lamp"])
    data = json.loads(state.read_text())
    assert data["rooms"] == {"A": [], "B": ["desk-lamp"]}
    assert data["groups"] == {"A": ["desk-lamp"], "B": ["desk-lamp"]}


@pytest.mark.parametrize("kind", ["rooms", "groups"])
@pytest.mark.parametrize("action", ["create", "rename", "delete", "assign"])
async def test_local_writes_level_one_denied_without_file_changes(house, state, kind, action):
    reg, caller, _ = house(0)
    with pytest.raises(Forbidden):
        admin.local_collection(reg, kind, action, "A", "B", [])
    assert not state.exists() and caller.sent == []


async def test_local_validation_duplicates_missing_and_no_partial_assignment(house, state):
    reg, caller, _ = house()
    admin.local_collection(reg, "rooms", "create", "A")
    admin.local_collection(reg, "rooms", "create", "B")
    original = state.read_text()
    operations = [("create", "a", None, None), ("rename", "A", "b", None), ("delete", "missing", None, None),
                  ("assign", "A", None, ["desk-lamp", "missing"]), ("assign", "A", None, ["office*"]),
                  ("create", "\n", None, None), ("create", "x" * 65, None, None)]
    for action, name, new, devices in operations:
        with pytest.raises(RegistryError):
            admin.local_collection(reg, "rooms", action, name, new, devices)
        assert state.read_text() == original


async def test_rooms_save_without_posix_locks(house, state, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_fcntl)
    reg, caller, _ = house()
    admin.local_collection(reg, "rooms", "create", "A")
    assert json.loads(state.read_text())["rooms"] == {"A": []}


async def test_non_ascii_names_round_trip_in_utf8(house, state, monkeypatch):
    reg, caller, _ = house()
    admin.local_collection(reg, "rooms", "create", "Küche")
    assert "Küche" in state.read_bytes().decode("utf-8")
    assert admin.local_collection(reg, "rooms", "list") == {"rooms": [{"name": "Küche", "devices": []}]}
    state.write_bytes(b"\xff\xfe{")
    with pytest.raises(RegistryError, match="unreadable"):
        admin.local_collection(reg, "rooms", "create", "A")
    assert state.read_bytes() == b"\xff\xfe{"


@pytest.mark.parametrize("contents", ["{", '{}', '{"version":2,"rooms":{},"groups":{}}',
                                    '{"version":1,"rooms":{"A":["bad/name"]},"groups":{}}',
                                    '{"version":1,"rooms":{"A":[{"token":"SECRET"}]},"groups":{}}',
                                    '{"version":1,"rooms":{},"groups":{},"token":"SECRET"}'])
async def test_corrupt_map_is_not_overwritten_or_returned(house, state, contents):
    reg, caller, _ = house()
    state.write_text(contents)
    for action in ("list", "create"):
        with pytest.raises(RegistryError) as caught:
            admin.local_collection(reg, "rooms", action, "A")
        assert "SECRET" not in str(caught.value)
        assert state.read_text() == contents


async def test_room_power_all_members_preflight_and_partial_failures(house, state, monkeypatch):
    reg, caller, devices = house()
    admin.local_collection(reg, "rooms", "create", "Office")
    admin.local_collection(reg, "rooms", "assign", "Office", devices=["desk-lamp", "hallway-cam"])
    with pytest.raises(Unsupported):
        await admin.room_power(reg, caller, "Office", True)
    assert not devices["lamp"].is_on
    admin.local_collection(reg, "rooms", "assign", "Office", devices=["desk-lamp", "office-1"])
    result = await admin.room_power(reg, caller, "Office", True)
    assert all(item["ok"] for item in result["results"]) and devices["lamp"].is_on
    async def fail():
        raise DeviceError("SECRET", error_code=None)
    monkeypatch.setattr(devices["lamp"], "turn_off", fail)
    result = await admin.room_power(reg, caller, "Office", False)
    assert result["results"][0]["ok"] is False and result["results"][1]["ok"] is True
    assert not devices["office1"].is_on and "SECRET" not in json.dumps(result)


async def test_room_power_level_zero_and_stale_member(house, state):
    reg, caller, _ = house()
    admin.local_collection(reg, "rooms", "create", "Office")
    admin.local_collection(reg, "rooms", "assign", "Office", devices=["desk-lamp"])
    low, lowcaller, devices = house(0)
    with pytest.raises(Forbidden):
        await admin.room_power(low, lowcaller, "Office", True)
    assert not devices["lamp"].is_on
    del reg._entries["p1"]
    with pytest.raises(NotFound):
        await admin.room_power(reg, caller, "Office", True)


async def test_file_save_failure_is_reported_and_atomic(house, state, monkeypatch):
    reg, caller, _ = house()
    admin.local_collection(reg, "rooms", "create", "A")
    original = state.read_text()
    def fail(*args):
        raise OSError("SECRET")
    monkeypatch.setattr(admin.os, "replace", fail)
    with pytest.raises(RegistryError, match="could not be saved"):
        admin.local_collection(reg, "rooms", "create", "B")
    assert state.read_text() == original and not list(state.parent.glob(".rooms-*"))


async def test_compatibility_does_not_guess_account_status():
    assert admin.compatibility_info()["detectable_locally"] is False
    assert admin.compatibility_info()["status"] == "unknown"


async def test_every_tool_mcp_schema_happy_path_and_doc(house, mcp, state):
    reg, caller, _ = house()
    cases = [
        ("device_rename", {"device": "desk-lamp", "name": "New"}),
        ("device_set_metadata", {"device": "desk-lamp", "icon": "plug"}),
        ("device_get_clock", {"device": "desk-lamp"}),
        ("device_set_clock", {"device": "desk-lamp", "timezone": "Etc/UTC"}),
        ("device_get_led", {"device": "desk-lamp"}),
        ("device_set_led", {"device": "desk-lamp", "rule": "night_mode", "start": 0, "end": 10}),
        ("firmware_get", {"device": "desk-lamp"}),
        ("firmware_check", {"device": "desk-lamp"}),
        ("firmware_progress", {"device": "desk-lamp"}),
        ("firmware_set_auto_update", {"device": "desk-lamp", "enabled": True, "time": 0, "random_range": 0, "confirm": AUTO_OK}),
        ("firmware_install_update", {"device": "desk-lamp", "version": "2.0", "confirm": "fw_download desk-lamp"}),
        ("device_diagnostics", {"device": "desk-lamp"}),
        ("device_third_party_compatibility", {}),
        ("room_create", {"name": "A"}), ("room_assign", {"name": "A", "devices": ["desk-lamp"]}),
        ("room_rename", {"name": "A", "new_name": "B"}), ("room_list", {}),
        ("room_set_power", {"name": "B", "on": True}), ("room_delete", {"name": "B"}),
        ("group_create", {"name": "A"}), ("group_assign", {"name": "A", "devices": ["desk-lamp"]}),
        ("group_rename", {"name": "A", "new_name": "B"}), ("group_list", {}), ("group_delete", {"name": "B"}),
    ]
    doc = (Path(__file__).parents[2] / "docs/areas/device_admin.md").read_text()
    async with mcp(reg, caller) as client:
        definitions = {t.name: t for t in (await client.list_tools()).tools}
        names = set(n for n, _ in cases)
        # Compare with what this module registers, so other areas' tools after a merge do not break the check.
        registered = []
        stub = SimpleNamespace(tool=lambda **_: lambda fn: registered.append(fn.__name__) or fn)
        admin.tools(stub)
        assert set(registered) == names and names <= set(definitions)
        for name, args in cases:
            assert f"- `{name}`:" in doc
            assert definitions[name].description
            result = await client.call_tool(name, args)
            assert not result.is_error, (name, result)
            assert isinstance(result.structured_content, dict)
        assert definitions["firmware_install_update"].annotations.destructive_hint
        assert definitions["firmware_set_auto_update"].annotations.destructive_hint
        assert definitions["room_list"].annotations.read_only_hint
        denied = await client.call_tool("firmware_install_update", {"device": "desk-lamp", "version": "2.0"})
        assert denied.is_error and "confirm" in denied.content[0].text
        denied = await client.call_tool("firmware_set_auto_update", {"device": "desk-lamp", "enabled": False})
        assert denied.is_error and AUTO_OK in denied.content[0].text
        invalid = await client.call_tool("device_set_led", {"device": "desk-lamp", "rule": "night_mode", "start": 1440, "end": 10})
        assert invalid.is_error


async def test_mcp_policy_error_reaches_client_without_advice(house, mcp, state):
    reg, caller, _ = house(0)
    async with mcp(reg, caller) as client:
        for name, args in [("device_rename", {"device": "desk-lamp", "name": "New"}), ("room_create", {"name": "A"})]:
            result = await client.call_tool(name, args)
            assert result.is_error and "level" in result.content[0].text and "allow_methods" not in result.content[0].text
    assert caller.sent == [] and not state.exists()


async def test_smartcam_hub_uses_camera_setting_methods_and_cached_diagnostics(house):
    reg, caller, devices = house()
    hub = devices["hub"]
    hub.internal_state = {"getDeviceInfo": {"device_info": {"basic_info": {"device_model": "H200", "sw_version": "1.0", "mac": "SECRET"}}},
                          "getAppComponentList": {"app_component": {"app_component_list": [{"name": "led", "version": 1}]}}}
    for fn, args, method in [(admin.rename_device, ("Hub",), "setDeviceAlias"),
                             (admin.device_clock, ("Etc/UTC",), "setTimezone"),
                             (admin.device_led, ("never",), "setLedStatus")]:
        await fn(reg, caller, "tapo-h200", *args)
        assert caller.sent[-1][1] == method
    result = await admin.diagnostics(reg, caller, "tapo-h200")
    assert result == {"device": "tapo-h200", "device_model": "H200", "sw_version": "1.0", "source": "cached negotiation", "components": [{"name": "led", "version": 1}]}
    assert "SECRET" not in json.dumps(result)
    with pytest.raises(Unsupported):
        await admin.firmware_info(reg, caller, "tapo-h200")


async def test_sensor_diagnostics_use_parent_components_without_unsupported_negotiation(house):
    reg, caller, _ = house()
    entry = reg.resolve_one("main-door")
    entry.device.internal_state = {"get_device_info": {}}
    entry.device._components_raw = {"component_list": [{"id": "firmware", "ver_code": 1}]}
    result = await admin.diagnostics(reg, caller, "main-door")
    assert result["components"] == [{"id": "firmware", "ver_code": 1}]
    assert caller.methods_sent() == ["get_device_info"]


async def test_child_doorbell_diagnostics_use_parent_cache(house):
    from kasa import DeviceType
    reg, caller, devices = house()
    entry = reg.resolve_one("hallway-cam")
    entry.root = reg.resolve_one("tapo-h200")
    entry.device.device_type = DeviceType.Doorbell
    entry.device.sys_info = {"model": "D230", "fw_ver": "1.0", "rssi": -55, "uptime": 120, "mac": "SECRET"}
    entry.device._components_raw = {"app_component_list": [{"name": "firmware", "version": 1}]}
    result = await admin.diagnostics(reg, caller, "hallway-cam")
    assert result["source"] == "cached parent state" and result["rssi"] == -55 and result["uptime"] == 120
    assert caller.sent == [] and "SECRET" not in json.dumps(result)


async def test_disabled_modules_are_refused(house):
    reg, caller, devices = house()
    devices["lamp"].modules[Module.Firmware].disabled = True
    with pytest.raises(Unsupported):
        await admin.firmware_info(reg, caller, "desk-lamp")
    assert caller.sent == []


# Complete response field layouts from sanitized fixtures. Every value below is synthetic.
SMART_WIRE = {'get_device_info': {'auto_off_remain_time': 0,
                     'auto_off_status': 'fake',
                     'avatar': 'fake',
                     'charging_status': 'fake',
                     'default_states': {'type': 'fake'},
                     'device_id': 'fake',
                     'device_on': False,
                     'fw_id': 'fake',
                     'fw_ver': 'fake',
                     'has_set_location_info': False,
                     'hw_id': 'fake',
                     'hw_ver': 'fake',
                     'ip': 'fake',
                     'lang': 'fake',
                     'latitude': 'fake',
                     'longitude': 'fake',
                     'mac': 'fake',
                     'model': 'fake',
                     'nickname': 'fake',
                     'oem_id': 'fake',
                     'on_time': 0,
                     'overcurrent_status': 'fake',
                     'power_protection_status': 'fake',
                     'region': 'fake',
                     'rssi': 0,
                     'signal_level': 0,
                     'specs': 'fake',
                     'ssid': 'fake',
                     'time_diff': 'fake',
                     'type': 'fake'},
 'component_nego': {'component_list': [{'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0},
                                       {'id': 'fake', 'ver_code': 0}]},
 'get_auto_update_info': {'enable': False, 'random_range': 0, 'time': 0},
 'get_device_time': {'region': 'fake', 'time_diff': 'fake', 'timestamp': 0},
 'get_led_info': {'led_rule': 'fake',
                  'led_status': False,
                  'night_mode': {'end_time': 0,
                                 'night_mode_type': 'fake',
                                 'start_time': 0,
                                 'sunrise_offset': 0,
                                 'sunset_offset': 0}}}
CAMERA_WIRE = {'getDeviceInfo': {'device_info': {'basic_info': {'avatar': 'fake',
                                                  'barcode': 'fake',
                                                  'dev_id': 'fake',
                                                  'device_alias': 'fake',
                                                  'device_info': 'fake',
                                                  'device_model': 'fake',
                                                  'device_name': 'fake',
                                                  'device_type': 'fake',
                                                  'features': 0,
                                                  'ffs': False,
                                                  'has_set_location_info': 'fake',
                                                  'hw_desc': 'fake',
                                                  'hw_id': 'fake',
                                                  'hw_version': 'fake',
                                                  'is_cal': False,
                                                  'latitude': 'fake',
                                                  'longitude': 'fake',
                                                  'mac': 'fake',
                                                  'manufacturer_name': 'fake',
                                                  'mobile_access': 'fake',
                                                  'no_rtsp_constrain': 0,
                                                  'oem_id': 'fake',
                                                  'region': 'fake',
                                                  'sw_version': 'fake'},
                                   'info': {'cover_reg_num': 0,
                                            'device_model': 'fake',
                                            'fss_banned_reg_num': 0,
                                            'fss_reg_num': 0,
                                            'hw_id': 'fake',
                                            'hw_version': 'fake',
                                            'isp_sw_version': 'fake',
                                            'isp_version': 0,
                                            'manufacturer_name': 'fake',
                                            'md_reg_num': 0,
                                            'model_description': 'fake',
                                            'roi_reg_num': 0,
                                            'sensor': 'fake',
                                            'sw_version': 'fake'}}},
 'getAppComponentList': {'app_component': {'app_component_list': [{'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0}]}},
 'getTimezone': {'system': {'basic': {'timezone': 'fake', 'timing_mode': 'fake', 'zone_id': 'fake'}}},
 'getClockStatus': {'system': {'clock_status': {'local_time': 'fake', 'seconds_from_1970': 0}}},
 'getLedStatus': {'led': {'config': {'enabled': 'fake'}}}}
HUB_WIRE = {'getDeviceInfo': {'device_info': {'basic_info': {'avatar': 'fake',
                                                  'bind_status': False,
                                                  'child_num': 0,
                                                  'dev_id': 'fake',
                                                  'device_alias': 'fake',
                                                  'device_info': 'fake',
                                                  'device_model': 'fake',
                                                  'device_name': 'fake',
                                                  'device_type': 'fake',
                                                  'has_set_location_info': 'fake',
                                                  'hw_id': 'fake',
                                                  'hw_version': 'fake',
                                                  'latitude': 'fake',
                                                  'local_ip': 'fake',
                                                  'longitude': 'fake',
                                                  'mac': 'fake',
                                                  'need_sync_sha1_password': 'fake',
                                                  'oem_id': 'fake',
                                                  'product_name': 'fake',
                                                  'region': 'fake',
                                                  'status': 'fake',
                                                  'subg_phyid': 'fake',
                                                  'sw_version': 'fake'},
                                   'info': {'avatar': 'fake',
                                            'bind_status': False,
                                            'child_num': 0,
                                            'dev_id': 'fake',
                                            'device_alias': 'fake',
                                            'device_info': 'fake',
                                            'device_model': 'fake',
                                            'device_name': 'fake',
                                            'device_type': 'fake',
                                            'has_set_location_info': 'fake',
                                            'hw_id': 'fake',
                                            'hw_version': 'fake',
                                            'latitude': 'fake',
                                            'local_ip': 'fake',
                                            'longitude': 'fake',
                                            'mac': 'fake',
                                            'need_sync_sha1_password': 'fake',
                                            'oem_id': 'fake',
                                            'product_name': 'fake',
                                            'region': 'fake',
                                            'status': 'fake',
                                            'subg_phyid': 'fake',
                                            'sw_version': 'fake'}}},
 'getAppComponentList': {'app_component': {'app_component_list': [{'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0},
                                                                  {'name': 'fake', 'version': 0}]}},
 'getTimezone': {'system': {'basic': {'timezone': 'fake', 'zone_id': 'fake'}}},
 'getClockStatus': {'system': {'clock_status': {'local_time': 'fake', 'seconds_from_1970': 0}}},
 'getLedStatus': {'led': {'config': {'.name': 'fake', '.type': 'fake', 'enabled': 'fake'}}}}


@pytest.mark.parametrize("device,wire", [("desk-lamp", SMART_WIRE), ("hallway-cam", CAMERA_WIRE), ("tapo-h200", HUB_WIRE)])
async def test_complete_fixture_response_shapes_with_synthetic_values(house, device, wire):
    reg, caller, _ = house(0)
    entry = reg.resolve_one(device)
    entry.device.internal_state = wire
    if "get_led_info" in wire:
        entry.device.modules[Module.Led].data = wire["get_led_info"]
    for method, response in wire.items():
        caller.respond(method, response)
    diagnostic = await admin.diagnostics(reg, caller, device)
    assert "components" in diagnostic
    assert "mac" not in diagnostic and "ssid" not in diagnostic
    clock = await admin.device_clock(reg, caller, device)
    assert "timestamp" in clock or "seconds_from_1970" in clock
    led = await admin.device_led(reg, caller, device)
    assert "led_rule" in led or "enabled" in led
    if device == "desk-lamp":
        result = await admin.firmware_info(reg, caller, device)
        assert result["auto_update"] == {"enable": False, "time": 0, "random_range": 0}


async def test_room_power_refuses_camera_even_when_camera_state_is_explicitly_writable(make_house, state):
    reg, caller, devices = make_house(tier=1, allow_write=frozenset({"camera.state"}))
    admin.local_collection(reg, "rooms", "create", "Mixed")
    admin.local_collection(reg, "rooms", "assign", "Mixed", devices=["desk-lamp", "hallway-cam"])
    with pytest.raises(Unsupported):
        await admin.room_power(reg, caller, "Mixed", False)
    assert devices["cam"].is_on is True
    assert devices["lamp"].is_on is False


async def test_explicit_method_permission_keeps_firmware_confirmation(house):
    reg, caller, _ = house(0, allow_methods=frozenset({"fw_download", "set_device_info"}))
    await admin.rename_device(reg, caller, "desk-lamp", "New")
    assert caller.sent[-1][1] == "set_device_info"
    caller.sent.clear()
    with pytest.raises(NeedsConfirmation):
        await admin.firmware_install(reg, caller, "desk-lamp", "2.0")
    assert caller.sent == []
    await admin.firmware_install(reg, caller, "desk-lamp", "2.0", "fw_download desk-lamp")
    assert caller.sent[-1][1] == "fw_download"


async def test_native_smart_child_without_info_in_last_update_can_use_firmware(house):
    # Build the library object without running initialization or touching its transport.
    from kasa.smart.smartchilddevice import SmartChildDevice
    from kasa import DeviceType
    reg, caller, _ = house(0)
    child = object.__new__(SmartChildDevice)
    child._device_type = DeviceType.Sensor
    child._modules = {Module.Firmware: SimpleNamespace(supported_version=1, firmware_update_info=None)}
    child._last_update = {"get_connect_cloud_state": {"status": 0}}
    child._info = {"device_id": "child", "model": "T110", "fw_ver": "1.0"}
    entry = reg.resolve_one("main-door")
    entry.device = child
    result = await admin.firmware_info(reg, caller, "main-door")
    assert result["fw_ver"] == "1.0" and caller.methods_sent() == ["get_device_info"]


async def test_generic_smart_vacuum_firmware_only_when_module_is_present(make_house):
    from kasa import DeviceType
    from ..fakes import FakeDevice
    robot = FakeDevice("robot", "Cleaning Robot", "RV30", DeviceType.Vacuum)
    robot.internal_state = {"get_device_info": {"fw_ver": "1.0"}}
    robot.modules[Module.Firmware] = SimpleNamespace(supported_version=1, firmware_update_info=None)
    reg, caller, _ = make_house(tier=4, extra=[robot], responses={"get_device_info": {"fw_ver": "1.0"}, "get_latest_fw": LATEST})
    result = await admin.firmware_info(reg, caller, "cleaning-robot")
    assert result == {"device": "cleaning-robot", "fw_ver": "1.0"}
    await admin.firmware_install(reg, caller, "cleaning-robot", "2.0", "fw_download cleaning-robot")
    assert caller.sent[-1] == ("cleaning-robot", "fw_download", {})
    robot.modules.clear()
    with pytest.raises(Unsupported):
        await admin.firmware_info(reg, caller, "cleaning-robot")
