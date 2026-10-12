"""Owner-authorized lifecycle operations through python-kasa and host Wi-Fi tools."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hmac
import ipaddress
import logging
import os
import re
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from kasa import Credentials, Discover, Module
from kasa.exceptions import AuthenticationError
from kasa.smart import SmartDevice
from kasa.smartcam import SmartCamDevice

from . import policy
from .calls import NeedsConfirmation
from .registry import DeviceUnavailable, Entry, Forbidden, NotFound, RegistryError, model_of
from .views import clean


def authorize(registry, entry, method, confirm=None, subject=None):
    """The operation names are policy declarations, not raw protocol methods.

    `subject` is the exact device the action lands on when that is not `entry`,
    for example the child that a hub unpairs. The phrase always names it.
    """
    rule = policy.rule_for(entry.kind, method)
    if rule is None:
        raise Forbidden(f"{method!r} is not supported on {entry.kind} devices")
    if not policy.permitted(rule, registry.cfg.tier, registry.cfg.allow_methods):
        raise Forbidden(f"{method!r} is a level {int(rule.tier)} action ({rule.summary}) and the owner's policy blocks it")
    phrase = policy.confirmation(rule, subject or entry.slug)
    if rule.needs_confirmation and confirm != phrase:
        raise NeedsConfirmation(f"{method!r}: {rule.summary} Call again with confirm={phrase!r}")
    # Every authorized action authenticates. Refuse it when owner-only sign-in
    # cannot be enforced, before any device or host is touched.
    _install_fallback_guard()
    return rule


def exact_entry(registry, ref):
    """A lifecycle target is named by its registry id or device id only.

    Names and globs match device-supplied text, so they never pick a device to
    pair, unpair, rename, move to another Wi-Fi or erase.
    """
    found = [e for e in registry.resolve(ref) if ref in (e.slug, e.device.device_id)]
    if len(found) != 1:
        raise NotFound(f"no device has the id {clean(ref, 80)!r}; lifecycle actions need the exact device id")
    return found[0]


_private = contextvars.ContextVar("provision_private", default=False)
_factory_installed = False


class _NoFallbackMapping(Mapping):
    """Stop kasa's default/blank fallback path before any default is read."""

    def __init__(self, original):
        self.original = original

    def _check(self):
        if _private.get():
            raise AuthenticationError("Owner credentials were rejected; fallback is disabled")

    def __getitem__(self, key):
        self._check()
        return self.original[key]

    def __iter__(self):
        self._check()
        return iter(self.original)

    def __len__(self):
        self._check()
        return len(self.original)


class _NoFallbackCredentials:
    def __init__(self, original=None):
        self.original = original

    def _value(self, field):
        if _private.get() or self.original is None:
            raise AuthenticationError("Owner credentials were rejected; fallback is disabled")
        return getattr(self.original, field)

    @property
    def username(self):
        return self._value("username")

    @property
    def password(self):
        return self._value("password")


class GuardUnavailable(Forbidden):
    """python-kasa changed in a way the owner-only sign-in guard does not know."""


def _refuse_guard():
    raise GuardUnavailable("Owner-only sign-in cannot be enforced with the installed python-kasa, so this action is refused")


# Modules that can reach python-kasa's fallback sign-in values, and
# whether their fallback mapping is read only at login time. The SMARTCAM
# transport reads it while it is built, so only its decoder is wrapped.
_FALLBACK_MODULES = {
    "kasa.transports.aestransport": True,
    "kasa.transports.klaptransport": True,
    "kasa.transports.ssltransport": True,
    "kasa.transports.linkietransport": True,
    "kasa.transports.sslaestransport": False,
    # Camera admin password change tries fallback passwords. Not used here.
    "kasa.smartcam.smartcamdevice": True,
}
_FALLBACK_NAMES = ("DEFAULT_CREDENTIALS", "get_default_credentials")
# Transport classes that cache a fallback identity on the instance.
_CACHING_TRANSPORTS = ("AesTransport", "SslTransport", "SslAesTransport")


def _guard_function(original):
    def guarded(value):
        # Inside a protected call never even decode the fallback value.
        return _NoFallbackCredentials(None if _private.get() else original(value))

    guarded._tolpo_guarded = True
    return guarded


def _install_fallback_guard():
    """Kasa 0.11 has no public owner-only authentication option.

    These context-aware guards deny fallback values, not owner authentication.
    No built-in credential values are read or copied by this area. Other tasks
    retain normal library behavior outside the protected context.

    The guards depend on python-kasa internals, so they fail closed: when a
    known module or name is missing, or any other kasa module can reach the
    fallback values, every lifecycle action is refused. This runs on every call.
    """
    import importlib
    import pkgutil

    try:
        from kasa import credentials as kasa_credentials, transports

        for info in pkgutil.iter_modules(transports.__path__):
            importlib.import_module(f"{transports.__name__}.{info.name}")
        originals = [getattr(kasa_credentials, name) for name in _FALLBACK_NAMES]
    except Exception:
        _refuse_guard()
    for name, wrap_mapping in _FALLBACK_MODULES.items():
        module = sys.modules.get(name)
        mapping = getattr(module, "DEFAULT_CREDENTIALS", None)
        decode = getattr(module, "get_default_credentials", None)
        if not isinstance(mapping, Mapping) or not callable(decode):
            _refuse_guard()
        if wrap_mapping and not isinstance(mapping, _NoFallbackMapping):
            module.DEFAULT_CREDENTIALS = _NoFallbackMapping(mapping)
        if not getattr(decode, "_tolpo_guarded", False):
            module.get_default_credentials = _guard_function(decode)
    for name, module in list(sys.modules.items()):
        if not name.startswith("kasa.") or name in _FALLBACK_MODULES or name == kasa_credentials.__name__:
            continue
        values = vars(module).values() if hasattr(module, "__dict__") else ()
        if any(hasattr(module, n) for n in _FALLBACK_NAMES) or any(v is o for v in values for o in originals):
            _refuse_guard()  # a new path to the fallback values this guard does not cover


def _guard_cached_credentials(entry):
    protocol = getattr(entry.top.device, "protocol", None)
    transport = getattr(protocol, "_transport", None)
    if transport is None:
        from kasa.protocols import BaseProtocol

        if isinstance(protocol, BaseProtocol):
            _refuse_guard()  # the transport moved; its cache cannot be checked
        return
    if type(transport).__name__ in _CACHING_TRANSPORTS and not hasattr(transport, "_default_credentials"):
        _refuse_guard()  # the fallback cache was renamed
    cached = getattr(transport, "_default_credentials", None)
    if cached is not None and not isinstance(cached, _NoFallbackCredentials):
        transport._default_credentials = _NoFallbackCredentials(cached)


class _PrivateFilter(logging.Filter):
    def filter(self, record):
        return not _private.get()


@contextmanager
def private_logs():
    """Kasa debug logs can contain the Wi-Fi request. Suppress them in this task."""
    global _factory_installed
    _install_fallback_guard()
    # New kasa loggers can be created by a lazy import during discovery. A
    # context-aware record factory protects those before any handler sees them.
    if not _factory_installed:
        previous_factory = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = previous_factory(*args, **kwargs)
            if _private.get():
                record.msg, record.args = "Provisioning log suppressed", ()
                record.exc_info = record.exc_text = record.stack_info = None
            return record

        logging.setLogRecordFactory(factory)
        _factory_installed = True
    # Logger filters apply at the origin, before propagation to any handler.
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(logger, logging.Logger) and (name.startswith("kasa") or name == "tolpo.registry"):
            if not any(isinstance(f, _PrivateFilter) for f in logger.filters):
                logger.addFilter(_PrivateFilter())
    token = _private.set(True)
    try:
        yield
    finally:
        _private.reset(token)


# Only variables the owner created for provisioning can be read. Any other
# name, such as the account the server signs in with or an unrelated secret,
# is refused, so an agent cannot send it to a device or a host Wi-Fi tool.
ENV_PREFIX = "TOLPO_PROVISION_"
ENV_NAME = re.compile(r"TOLPO_PROVISION_[A-Z0-9_]{1,64}")


def env_name(name) -> str:
    if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
        raise RegistryError(f"Use the name of an environment variable that starts with {ENV_PREFIX}, not a value")
    return name


def env_value(name: str, *, empty=False) -> str:
    env_name(name)
    value = os.environ.get(name)
    if value is None or (not empty and not value) or "\x00" in value:
        raise RegistryError("A required provisioning environment variable is missing or invalid")
    return value


class Lifecycle(Protocol):
    async def invoke(self, entry: Entry, operation: str, **params: Any) -> Any: ...


def _same(a, b):
    return isinstance(a, str) and isinstance(b, str) and hmac.compare_digest(a.encode(), b.encode())


class KasaLifecycle:
    """The small fakeable interface uses public kasa device and module APIs only."""

    async def invoke(self, entry, operation, **params):
        dev = entry.device
        if operation in {"pair_hub", "pair_remove"}:
            setup = dev.modules.get(Module.ChildSetup)
            if setup is None or entry.root is not None:
                raise NotFound(f"{entry.slug} has no hub child setup support")
            if operation == "pair_hub":
                return await setup.pair(timeout=params["timeout"])
            return await setup.unpair(params["device_id"])
        if operation == "pair_name":
            return await dev.set_alias(params["name"])
        if operation == "wifi_scan":
            if entry.root is not None:
                raise NotFound(f"{entry.slug} has no own Wi-Fi radio")
            return await dev.wifi_scan()
        if operation == "wifi_join":
            if entry.root is not None:
                raise NotFound(f"{entry.slug} has no own Wi-Fi radio")
            credentials = dev.credentials
            if credentials is None or not (
                _same(credentials.username, params["username"]) & _same(credentials.password, params["account_password"])
            ):
                raise RegistryError("The device session must use the same owner's environment-supplied account")
            return await dev.wifi_join(params["ssid"], params["password"], params["keytype"])
        if operation in {"reset_factory", "reset_reboot"}:
            # SmartCamDevice inherits SmartDevice but does not implement these
            # operations in its dialect. Never send the SMART method to it.
            if not isinstance(dev, SmartDevice) or isinstance(dev, SmartCamDevice) or entry.root is not None:
                raise NotFound(f"{entry.slug} has no supported SMART reset or reboot")
            if operation == "reset_factory":
                return await dev.factory_reset()
            return await dev.reboot(delay=params["delay"])
        raise Forbidden("Unknown provisioning operation")


async def invoke(registry, adapter, entry, operation, **params):
    async def send():
        with private_logs():
            _guard_cached_credentials(entry)
            try:
                return await adapter.invoke(entry, operation, **params)
            except RegistryError:
                raise
            except AuthenticationError:
                raise DeviceUnavailable(f"{entry.slug} rejected the account credentials") from None
            except (TimeoutError, asyncio.CancelledError):
                raise
            except Exception:
                # Kasa may include the SSID or the entire request in its error.
                raise DeviceUnavailable(f"{entry.slug} failed during {operation}") from None

    return await registry.guarded(entry, send)


def device_view(entry):
    out = {"id": clean(entry.slug), "kind": entry.kind, "model": clean(str(model_of(entry)))}
    if entry.device.alias:
        out["name"] = clean(str(entry.device.alias))
    return out


async def refresh(registry, entry):
    with private_logs():
        _guard_cached_credentials(entry)
        await registry.refresh(entry, force=True)
    return {"state": "refreshed" if entry.online else "offline"}


async def rescan_new(registry, before):
    # Same entry point as the existing rescan tool. Do not implement discovery.
    with private_logs():
        for entry in registry.entries():
            _guard_cached_credentials(entry)
        try:
            await registry.discover()
        except (TimeoutError, asyncio.CancelledError):
            raise
        except Exception:
            raise DeviceUnavailable("Network rescan failed after the lifecycle action") from None
    return [device_view(e) for e in registry.entries() if e.device.device_id not in before and e.online]


async def pair_hub(registry, adapter, entry, timeout=10, confirm=None):
    authorize(registry, entry, "pair_hub", confirm)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 10:
        raise RegistryError("Pairing wait must be from 1 to 10 seconds")
    before = {e.device.device_id for e in registry.entries()}
    found = await invoke(registry, adapter, entry, "pair_hub", timeout=timeout)
    state = await refresh(registry, entry)
    try:
        new = await rescan_new(registry, before)
    except TimeoutError:
        raise DeviceUnavailable(f"{entry.slug} pairing completed but the network rescan timed out") from None
    # Kasa returns only successes, not a list of all candidates. Never forward
    # its records, which can include encoded names and hardware identifiers.
    ids = {item["device_id"] for item in found if isinstance(item, dict) and isinstance(item.get("device_id"), str)}
    paired = [device_view(e) for e in registry.entries() if e.root is entry and e.device.device_id in ids]
    out = {"device": clean(entry.slug), "paired_count": len(ids), **state}
    if paired:
        out["paired"] = paired
    if new:
        out["new_devices"] = new
    return out


async def pair_remove(registry, adapter, entry, child, confirm=None):
    # The phrase names the child that is removed, not only its hub, so one
    # confirmation can never remove a different child of the same hub.
    target = exact_entry(registry, child)
    if target.root is not entry:
        raise RegistryError("The selected device is not a child of this hub")
    authorize(registry, entry, "pair_remove", confirm, subject=target.slug)
    await invoke(registry, adapter, entry, "pair_remove", device_id=target.device.device_id)
    state = await refresh(registry, entry)
    removed = target.device.device_id not in {d.device_id for d in entry.device.children}
    if removed:
        registry.forget(target)  # the hub confirmed it, so it need not wait for a second update to leave the list
    return {"device": clean(entry.slug), "child": clean(target.slug), "removed": removed, **state}


async def pair_name(registry, adapter, entry, name, confirm=None):
    authorize(registry, entry, "pair_name", confirm)
    if not isinstance(name, str) or not name.strip() or len(name.encode()) > 64 or clean(name, 128) != name:
        raise RegistryError("Name must be flat text of 1 to 64 bytes with no surrounding spaces")
    await invoke(registry, adapter, entry, "pair_name", name=name)
    state = await refresh(registry, entry)
    return {"device": clean(entry.slug), "name": clean(name), "changed": "name", **state}


async def wifi_scan(registry, adapter, entry):
    authorize(registry, entry, "wifi_scan")
    networks = await invoke(registry, adapter, entry, "wifi_scan")
    # Only radio measurements. No SSIDs, BSSIDs or opaque per-network handles.
    rows = []
    for net in networks[:64]:
        row = {}
        for key in ("channel", "signal_level", "rssi"):
            value = getattr(net, key, None)
            if isinstance(value, int) and not isinstance(value, bool):
                row[key] = value
        if row:
            rows.append(row)
    out = {"device": clean(entry.slug), "network_count": len(networks)}
    if rows:
        out["radio"] = rows
    return out


async def wifi_join(registry, adapter, entry, ssid_env, password_env, account_env, account_password_env, keytype="wpa2_psk", confirm=None):
    authorize(registry, entry, "wifi_join", confirm)
    # "wpa2_psk" and "none" appear in python-kasa and the app. UNVERIFIED: "wpa_psk".
    if keytype not in {"wpa2_psk", "wpa_psk", "none"}:
        raise RegistryError("Unsupported Wi-Fi security type")
    ssid, password = env_value(ssid_env), env_value(password_env, empty=keytype == "none")
    username, account_password = env_value(account_env), env_value(account_password_env)
    if len(ssid.encode()) > 32:
        raise RegistryError("Wi-Fi network name exceeds 32 bytes")
    await invoke(registry, adapter, entry, "wifi_join", ssid=ssid, password=password, keytype=keytype, username=username, account_password=account_password)
    state = await refresh(registry, entry)
    return {"device": clean(entry.slug), "changed": "wifi_requested", **state}


async def reset(registry, adapter, entry, factory=False, delay=1, confirm=None):
    operation = "reset_factory" if factory else "reset_reboot"
    authorize(registry, entry, operation, confirm)
    if not isinstance(delay, int) or isinstance(delay, bool) or not 1 <= delay <= 30:
        raise RegistryError("Reboot delay must be from 1 to 30 seconds")
    await invoke(registry, adapter, entry, operation, delay=delay)
    state = await refresh(registry, entry)
    return {"device": clean(entry.slug), "changed": "factory_reset_requested" if factory else "reboot_requested", **state}


class HostWifi(Protocol):
    async def current(self) -> str: ...
    async def join(self, network: str) -> None: ...
    async def restore(self, previous: str) -> None: ...


async def system_command(*args):
    """No shell, no command/output logging, and no subprocess output in errors."""
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), 30)
        except BaseException:
            proc.kill()
            await proc.communicate()
            raise
        if proc.returncode:
            raise RegistryError("Host Wi-Fi command failed")
        return stdout.decode(errors="replace").strip()
    except (OSError, TimeoutError):
        raise RegistryError("Host Wi-Fi command could not complete") from None


def interface_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}", value):
        raise RegistryError("Invalid Wi-Fi interface name")
    return value


class MacOSWifi:
    def __init__(self, interface, run=system_command):
        self.interface, self.run = interface_name(interface), run
        self._joined = None

    async def current(self):
        text = await self.run("/usr/sbin/networksetup", "-getairportnetwork", self.interface)
        prefix = "Current Wi-Fi Network: "
        if not text.startswith(prefix) or not text[len(prefix):]:
            raise RegistryError("The host must have a restorable Wi-Fi network before provisioning")
        return text[len(prefix):]

    async def join(self, network):
        self._joined = network
        await self.run("/usr/sbin/networksetup", "-setairportnetwork", self.interface, network)

    async def restore(self, previous):
        # The OS must remember credentials for the previous network.
        restored = False
        try:
            await self.run("/usr/sbin/networksetup", "-setairportnetwork", self.interface, previous)
            if await self.current() != previous:
                raise RegistryError("The previous host Wi-Fi network was not restored")
            restored = True
        finally:
            # Joining adds the open setup network to the preferred list, so the
            # host would join any later network with that name. Remove it.
            # UNVERIFIED on a real Mac: the exact preferred-list behavior.
            joined, self._joined = self._joined, None
            if joined is not None:
                try:
                    await self.run("/usr/sbin/networksetup", "-removepreferredwirelessnetwork", self.interface, joined)
                except Exception:
                    if restored:
                        raise RegistryError("Host Wi-Fi restored, but the setup network stayed in its saved networks") from None


class LinuxWifi:
    # A dedicated profile that never connects on its own. `nmcli device wifi
    # connect` would save an auto-connecting open profile for the setup network.
    # UNVERIFIED on a real Linux host: the nmcli argument forms below.
    PROFILE = "tolpo-softap"

    def __init__(self, interface, run=system_command):
        self.interface, self.run = interface_name(interface), run

    async def current(self):
        value = await self.run("nmcli", "-g", "GENERAL.CON-UUID", "device", "show", self.interface)
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", value):
            raise RegistryError("The host must have a saved Wi-Fi connection before provisioning")
        return value

    async def _drop_profile(self):
        await self.run("nmcli", "connection", "delete", "id", self.PROFILE)

    async def join(self, network):
        # A profile left by an interrupted run is replaced, never reused.
        with contextlib.suppress(RegistryError):
            await self._drop_profile()
        # network is one argv value after its keyword, never an option.
        await self.run(
            "nmcli", "connection", "add", "type", "wifi", "ifname", self.interface,
            "con-name", self.PROFILE, "autoconnect", "no", "ssid", network,
        )
        await self.run("nmcli", "connection", "up", "id", self.PROFILE, "ifname", self.interface)

    async def restore(self, previous):
        restored = False
        try:
            await self.run("nmcli", "connection", "up", "uuid", previous, "ifname", self.interface)
            if await self.current() != previous:
                raise RegistryError("The previous host Wi-Fi connection was not restored")
            restored = True
        finally:
            try:
                await self._drop_profile()
            except Exception:
                if restored:
                    raise RegistryError("Host Wi-Fi restored, but the setup profile could not be removed") from None


def host_wifi(interface):
    if sys.platform == "darwin":
        return MacOSWifi(interface)
    if sys.platform.startswith("linux"):
        return LinuxWifi(interface)
    raise RegistryError("Host Wi-Fi provisioning requires macOS or Linux")


@dataclass(repr=False)
class SoftAPPlan:
    target: str
    device_id: str
    ap_host: str
    ap_env: str
    ssid_env: str
    wifi_password_env: str
    account_env: str
    account_password_env: str
    interface: str
    # Environment names only. Values are read after authorization at execution.
    wait_seconds: int = 90

    def validate(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self.target):
            raise RegistryError("Provisioning target must be a short lowercase identifier")
        if not isinstance(self.device_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.device_id):
            raise RegistryError("Provide the exact expected device id")
        try:
            address = ipaddress.ip_address(self.ap_host)
        except ValueError:
            raise RegistryError("Provide the device AP's local IP address") from None
        if address.version != 4 or not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast:
            raise RegistryError("Provide the device AP's local IPv4 address")
        interface_name(self.interface)
        for name in (self.ap_env, self.ssid_env, self.wifi_password_env, self.account_env, self.account_password_env):
            env_name(name)
        if not isinstance(self.wait_seconds, int) or isinstance(self.wait_seconds, bool) or not 1 <= self.wait_seconds <= 180:
            raise RegistryError("LAN wait must be from 1 to 180 seconds")


def softap_entry(plan):
    from types import SimpleNamespace
    from kasa import DeviceType

    # This is a policy target, not a registry device or a guessed connection.
    return Entry(SimpleNamespace(device_type=DeviceType.Plug), plan.target)


def softap_plan(registry, plan, confirm=None):
    plan.validate()
    # The phrase names the expected device id, not only the agent's label.
    authorize(registry, softap_entry(plan), "pair_softap", confirm, subject=plan.device_id)
    return {"target": plan.target, "steps": [
        "Save the host's current Wi-Fi connection.",
        "Join the device's open setup Wi-Fi network.",
        "Authenticate with the owner's account and verify the expected SMART device id.",
        "Send home Wi-Fi settings and the account through python-kasa wifi_join.",
        "Restore the host's previous network, including after a failure.",
        "Wait on the LAN using the existing registry rescan and verify the device and cloud state.",
    ]}


class KasaConnector:
    async def connect(self, host, username, password):
        dev = await Discover.discover_single(host, credentials=Credentials(username, password), discovery_timeout=5)
        if dev is None:
            raise DeviceUnavailable("The setup device did not answer")
        try:
            if not isinstance(dev, SmartDevice) or isinstance(dev, SmartCamDevice):
                raise RegistryError("SoftAP self-binding is supported here only for SMART devices")
            # Discovery alone has not authenticated or loaded device time,
            # which SMART wifi_join includes in its quick-setup request.
            await asyncio.wait_for(dev.update(), 20)
            return dev
        except BaseException:
            await dev.disconnect()
            raise


_host_lock = asyncio.Lock()


async def _restore(host, previous):
    task = asyncio.create_task(asyncio.wait_for(_host_call(host, "restore", previous), 45))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Repeated caller cancellation must not cancel the restore task.
            cancelled = True
        except Exception:
            break  # Inspect the task below without forwarding raw output.
    try:
        task.result()
    except TimeoutError:
        raise RegistryError("Host Wi-Fi restoration timed out") from None
    if cancelled:
        raise asyncio.CancelledError()


async def _host_call(host, operation, *args):
    try:
        return await getattr(host, operation)(*args)
    except asyncio.CancelledError:
        raise
    except RegistryError as exc:
        # Our host adapters write these messages; they hold no command output.
        raise RegistryError(f"Host Wi-Fi {operation} failed: {exc}") from None
    except Exception:
        raise RegistryError(f"Host Wi-Fi {operation} failed") from None


async def softap_provision(registry, adapter, plan, host, connector=None, confirm=None, sleep=asyncio.sleep):
    softap_plan(registry, plan, confirm)
    connector = connector or KasaConnector()
    ap = env_value(plan.ap_env)
    ssid = env_value(plan.ssid_env)
    password = env_value(plan.wifi_password_env)
    username = env_value(plan.account_env)
    account_password = env_value(plan.account_password_env)
    if len(ap.encode()) > 32 or len(ssid.encode()) > 32 or ap.startswith("-"):
        raise RegistryError("Invalid provisioning Wi-Fi network name")
    before = {e.device.device_id for e in registry.entries()}
    async with _host_lock:
        previous = await _host_call(host, "current")
        try:
            await _host_call(host, "join", ap)
            dev = None
            try:
                with private_logs():
                    dev = await connector.connect(plan.ap_host, username, account_password)
                    if not isinstance(dev, SmartDevice) or isinstance(dev, SmartCamDevice):
                        raise RegistryError("SoftAP self-binding is supported here only for SMART devices")
                    if dev.device_id != plan.device_id:
                        raise RegistryError("The setup device does not match the expected device id")
                    entry = Entry(dev, plan.target)
                    if entry.kind not in {"plug", "light", "switch", "hub"}:
                        raise RegistryError("This device family has no supported SMART SoftAP onboarding")
                    # One lifecycle authorization covers this self-binding plan.
                    await invoke(registry, adapter, entry, "wifi_join", ssid=ssid, password=password, keytype="wpa2_psk", username=username, account_password=account_password)
                    await refresh(registry, entry)
            except RegistryError:
                raise
            except AuthenticationError:
                raise DeviceUnavailable(f"{plan.target} rejected the account credentials") from None
            except asyncio.CancelledError:
                raise
            except Exception:
                raise DeviceUnavailable(f"{plan.target} failed during SoftAP provisioning") from None
            finally:
                if dev is not None:
                    with private_logs():
                        try:
                            await dev.disconnect()
                        except Exception:
                            raise DeviceUnavailable(f"{plan.target} could not close the setup connection") from None
        finally:
            await _restore(host, previous)
        # Keep the host lock until verification completes. Another provisioning
        # call must not move the host off the LAN midway through this scan.
        deadline = asyncio.get_running_loop().time() + plan.wait_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise DeviceUnavailable(f"{plan.target} did not appear on the LAN after provisioning; host Wi-Fi restored")
            try:
                await asyncio.wait_for(rescan_new(registry, before), remaining)
            except TimeoutError:
                raise DeviceUnavailable(f"{plan.target} LAN verification timed out; host Wi-Fi restored") from None
            match = next((e for e in registry.entries() if e.device.device_id == plan.device_id and e.root is None), None)
            if match:
                await refresh(registry, match)
                if match.online:
                    out = {"target": plan.target, "verified": "lan", "host_wifi": "restored", "device": device_view(match)}
                    feat = match.device.features.get("cloud_connection")
                    if feat is not None and isinstance(feat.value, bool):
                        out["cloud_connected"] = feat.value
                    new = [device_view(e) for e in registry.entries() if e.device.device_id not in before and e.online]
                    if new:
                        out["new_devices"] = new
                    return out
            await sleep(min(3, max(0, deadline - asyncio.get_running_loop().time())))
