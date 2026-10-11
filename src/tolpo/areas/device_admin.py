"""Device settings, SMART firmware and tolpo's own rooms and groups."""

import base64
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kasa import Module
from kasa.smart import SmartDevice
from kasa.smartcam import SmartCamDevice
from pydantic import Field

from .. import policy
from ..calls import NeedsConfirmation
from ..policy import Tier
from ..registry import DeviceUnavailable, Forbidden, NotFound, RegistryError, Unsupported, failure_code
from ..views import clean
from .common import CAMERA_KINDS, SMART_KINDS

CAM_DIALECT_KINDS = [*CAMERA_KINDS, "hub"]

METHODS = [
    policy.method("component_nego", Tier.READ, SMART_KINDS, "Read SMART device components."),
    policy.method("setDeviceAlias", Tier.SETTING, CAM_DIALECT_KINDS, "Rename a camera, doorbell or camera protocol hub."),
    policy.method("get_device_time", Tier.READ, SMART_KINDS, "Read the device clock and time zone."),
    policy.method("set_device_time", Tier.SETTING, SMART_KINDS, "Change the device clock and time zone."),
    policy.method("getTimezone", Tier.READ, CAM_DIALECT_KINDS, "Read the camera time zone."),
    policy.method("getClockStatus", Tier.READ, CAM_DIALECT_KINDS, "Read the camera clock."),
    policy.method("setTimezone", Tier.SETTING, CAM_DIALECT_KINDS, "Change the camera time zone."),
    policy.method("set_led_info", Tier.SETTING, SMART_KINDS, "Change the indicator LED rule or schedule."),
    policy.method("getLedStatus", Tier.READ, CAM_DIALECT_KINDS, "Read the camera indicator LED state."),
    policy.method("setLedStatus", Tier.SECURITY, CAM_DIALECT_KINDS, "Change the camera indicator light. Off hides that the camera is recording."),
    policy.method("get_latest_fw", Tier.CONTROL, SMART_KINDS, "Ask the device to check the vendor cloud for a firmware update."),
    policy.method("get_auto_update_info", Tier.READ, SMART_KINDS, "Read automatic firmware update settings."),
    # Turning automatic updates on lets the device install firmware and reboot unattended, and turning them off stops
    # security fixes. The audit groups both with firmware installation, and the camera firmware switch is level 4 too.
    policy.method("set_auto_update_info", Tier.LIFECYCLE, SMART_KINDS, "Change automatic firmware updates and their window."),
    policy.method("get_fw_download_state", Tier.READ, SMART_KINDS, "Read firmware download and upgrade progress."),
    policy.method("fw_download", Tier.LIFECYCLE, SMART_KINDS, "Install firmware fetched by the device and reboot it."),
]

DeviceRef = Annotated[str, Field(description="Device slug or exact name")]
Name = Annotated[str, Field(description="Local room or group name", min_length=1, max_length=64)]
Minute = Annotated[int, Field(description="Minutes after local midnight", ge=0, le=1439, strict=True)]
Confirm = Annotated[str | None, Field(description="Exact phrase: fw_download <device slug>")]
AutoConfirm = Annotated[str | None, Field(description="Exact phrase: set_auto_update_info <device slug>")]


def _name(value: str) -> str:
    if not isinstance(value, str):
        raise RegistryError("The name must be text.")
    result = clean(value)
    if not result or len(value) > 64:
        raise RegistryError("The name must contain 1 to 64 characters.")
    return result


def _camera_dialect(entry):
    return entry.kind in CAMERA_KINDS or isinstance(entry.device, SmartCamDevice) or "getDeviceInfo" in getattr(entry.device, "internal_state", {})


def _smart(entry):
    # IOT devices have a different request envelope, even for matching kinds.
    native = isinstance(entry.device, SmartDevice) and not isinstance(entry.device, SmartCamDevice)
    if entry.kind not in SMART_KINDS or not (native or "get_device_info" in getattr(entry.device, "internal_state", {})):
        raise Unsupported(f"{clean(entry.slug)}: this operation requires the SMART protocol.")


def _module(entry, name, *, smart=False):
    if smart:
        _smart(entry)
    module = entry.device.modules.get(name)
    if module is None or getattr(module, "disabled", False):
        raise Unsupported(f"{clean(entry.slug)}: the {name} module is unavailable.")
    return module


def _pick(data, fields, limit=64):
    """Only known scalar fields reach the agent. Unknown and nested values stay private."""
    if not isinstance(data, dict):
        return {}
    out = {}
    for key in fields:
        value = data.get(key)
        if isinstance(value, str):
            value = clean(value, limit)
        if value is not None and value != "" and isinstance(value, (str, bool, int, float)):
            if not isinstance(value, float) or math.isfinite(value):
                out[key] = value
    return out


async def _call(caller, entry, method, params=None, *, confirm=None):
    try:
        return await caller.call(entry, method, params, confirm=confirm)
    except DeviceUnavailable as exc:
        # The shared caller may include raw library text. Keep device values out of errors too.
        text = str(exc)
        code = failure_code(exc)
        reason = f"device reported an error ({code})" if code else "device reported an error"
        if "rejected the account" in text:
            reason = "account credentials were rejected"
        elif "did not answer" in text:
            reason = "device did not answer in time"
        raise DeviceUnavailable(f"{clean(entry.slug)}: {method} failed: {reason}.") from None


async def _write(registry, caller, entry, method, params=None, *, changed, confirm=None):
    await _call(caller, entry, method, params, confirm=confirm)
    await registry.refresh(entry, force=True)
    out = {"device": clean(entry.slug), "changed": changed}
    if not entry.online:
        out["refresh"] = "Device is unavailable after the write; state was not verified."
    return out


async def rename_device(registry, caller, device, name):
    entry = registry.resolve_one(device)
    name = _name(name)
    if _camera_dialect(entry):
        method, params = "setDeviceAlias", {"system": {"sys": {"dev_alias": name}}}
    else:
        _smart(entry)
        method, params = "set_device_info", {"nickname": base64.b64encode(name.encode()).decode()}
    return await _write(registry, caller, entry, method, params, changed={"name": name})


async def set_metadata(registry, caller, device, icon=None, latitude=None, longitude=None):
    # UNVERIFIED: python-kasa writes neither field. The audit names `avatar` as a set_device_info field, and the real
    # get_device_info answers carry `avatar`, `latitude` and `longitude`. That set_device_info accepts the coordinates
    # as integer degrees times 10000 (the scale python-kasa reads them with) is not confirmed on a device.
    entry = registry.resolve_one(device)
    _smart(entry)
    params, changed = {}, {}
    if icon is not None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", icon):
            raise RegistryError("Icon must be a built-in avatar identifier.")
        params["avatar"] = changed["icon"] = icon
    if (latitude is None) != (longitude is None):
        raise RegistryError("Latitude and longitude must be provided together.")
    if latitude is not None:
        if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise RegistryError("Coordinates are outside their valid range.")
        params.update(latitude=int(latitude * 10000), longitude=int(longitude * 10000))
        changed["location"] = "updated"
    if not params:
        raise RegistryError("Provide an icon or coordinates.")
    return await _write(registry, caller, entry, "set_device_info", params, changed=changed)


async def device_clock(registry, caller, device, timezone=None, timestamp=None):
    entry = registry.resolve_one(device)
    _module(entry, Module.Time, smart=not _camera_dialect(entry))
    camera = _camera_dialect(entry)
    if timezone is None:
        if timestamp is not None:
            raise RegistryError("A time zone is required when setting the clock.")
        if not camera:
            data = await _call(caller, entry, "get_device_time")
            return {"device": clean(entry.slug), **_pick(data, ("timestamp", "time_diff", "region"))}
        zone = await _call(caller, entry, "getTimezone", {"system": {"name": "basic"}})
        clock = await _call(caller, entry, "getClockStatus", {"system": {"name": "clock_status"}})
        return {"device": clean(entry.slug), **_pick(zone.get("system", {}).get("basic", {}), ("timezone", "zone_id")),
                **_pick(clock.get("system", {}).get("clock_status", {}), ("seconds_from_1970",))}
    try:
        tz = ZoneInfo(timezone)
        if timestamp is not None and (type(timestamp) is not int or not 0 <= timestamp <= 253402214400):
            raise ValueError
        dt = datetime.now(tz) if timestamp is None else datetime.fromtimestamp(timestamp, tz)
    except (ZoneInfoNotFoundError, ValueError, OverflowError, OSError):
        raise RegistryError("Provide a valid IANA time zone and Unix timestamp.") from None
    offset = int(dt.utcoffset().total_seconds() / 60)
    if camera:
        if timestamp is not None:
            raise Unsupported(f"{clean(entry.slug)}: camera clock writes are unsupported; only its time zone can be set.")
        sign = "+" if offset >= 0 else "-"
        hours, minutes = divmod(abs(offset), 60)
        params = {"system": {"basic": {"timezone": f"UTC{sign}{hours:02d}:{minutes:02d}", "zone_id": timezone}}}
        method, changed = "setTimezone", {"timezone": clean(timezone)}
    else:
        params = {"timestamp": int(dt.timestamp()), "time_diff": offset, "region": timezone}
        method, changed = "set_device_time", params
    return await _write(registry, caller, entry, method, params, changed=changed)


_LED_FIELDS = ("led_rule", "led_status", "start_time", "end_time", "night_mode_type", "sunrise_offset", "sunset_offset")


async def device_led(registry, caller, device, rule=None, start=None, end=None, confirm=None):
    entry = registry.resolve_one(device)
    module = _module(entry, Module.Led, smart=not _camera_dialect(entry))
    if rule not in (None, "always", "never", "night_mode"):
        raise RegistryError("LED rule must be always, never or night_mode.")
    schedule = start is not None or end is not None
    if schedule and (rule != "night_mode" or type(start) is not int or type(end) is not int or not 0 <= start <= 1439 or not 0 <= end <= 1439 or start == end):
        raise RegistryError("A night schedule needs distinct start and end minutes from 0 to 1439.")
    if _camera_dialect(entry):
        if rule == "night_mode" or schedule:
            raise Unsupported(f"{clean(entry.slug)}: camera LED schedules are unsupported.")
        if rule is None:
            data = await _call(caller, entry, "getLedStatus", {"led": {"name": "config"}})
            return {"device": clean(entry.slug), **_pick(data.get("led", {}).get("config", {}), ("enabled",))}
        params = {"led": {"config": {"enabled": "on" if rule == "always" else "off"}}}
        return await _write(registry, caller, entry, "setLedStatus", params, changed={"led_rule": rule}, confirm=confirm)
    # python-kasa rate-limits get_led_info because repeated queries can crash P100.
    # Use the module's existing response rather than issuing another LED read.
    data = dict(module.data)
    visible = _pick(data, _LED_FIELDS)
    if isinstance(data.get("night_mode"), dict):
        night = _pick(data["night_mode"], _LED_FIELDS[2:])
        if night:
            visible["night_mode"] = night
    if rule is None:
        return {"device": clean(entry.slug), **visible}
    params = {**visible, "led_rule": rule}
    if schedule:
        night = {"night_mode_type": "custom", "start_time": start, "end_time": end}
        # Match the device's existing layout: old SMART flat fields, newer nested night_mode.
        if "night_mode" in data:
            params["night_mode"] = {**params.get("night_mode", {}), **night}
        elif "night_mode_type" in data:
            params.update(night)
        else:
            raise Unsupported(f"{clean(entry.slug)}: LED schedule layout is unavailable.")
    elif rule == "night_mode" and "night_mode" not in data and "night_mode_type" not in data:
        raise Unsupported(f"{clean(entry.slug)}: no LED night schedule is available.")
    return await _write(registry, caller, entry, "set_led_info", params, changed=params)


def _firmware(entry, auto=False):
    module = _module(entry, Module.Firmware, smart=True)
    if auto and module.supported_version <= 1:
        raise Unsupported(f"{clean(entry.slug)}: automatic firmware updates are unsupported.")
    return module


def _latest(data):
    out = _pick(data, ("type", "need_to_upgrade", "fw_ver", "release_date", "fw_size"))
    out.update(_pick(data, ("release_note",), limit=1200))
    return out


async def firmware_info(registry, caller, device, check=False, progress=False):
    entry = registry.resolve_one(device)
    module = _firmware(entry)
    if check:
        return {"device": clean(entry.slug), **_latest(await _call(caller, entry, "get_latest_fw"))}
    if progress:
        data = await _call(caller, entry, "get_fw_download_state")
        return {"device": clean(entry.slug), **_pick(data, ("status", "download_progress", "upgrade_time", "reboot_time", "auto_upgrade"))}
    info = await _call(caller, entry, "get_device_info")
    out = {"device": clean(entry.slug), **_pick(info, ("fw_ver", "hw_ver"))}
    if module.supported_version > 1:
        data = await _call(caller, entry, "get_auto_update_info")
        auto = _pick(data, ("enable", "time", "random_range"))
        if auto:
            out["auto_update"] = auto
    cached = getattr(module, "firmware_update_info", None)
    if cached:
        out["latest_cached"] = _latest({"type": cached.status, "need_to_upgrade": cached.needs_upgrade,
                                       "fw_ver": cached.version, "release_note": cached.release_notes,
                                       "release_date": cached.release_date.isoformat() if cached.release_date else None})
    return out


async def firmware_auto_update(registry, caller, device, enabled, time=None, random_range=None, confirm=None):
    entry = registry.resolve_one(device)
    _firmware(entry, auto=True)
    if type(enabled) is not bool:
        raise RegistryError("Enabled must be a boolean.")
    for value in (time, random_range):
        if value is not None and (type(value) is not int or not 0 <= value <= 1439):
            raise RegistryError("Window values must be minutes from 0 to 1439.")
    # Refuse before the read, so a blocked or unconfirmed change sends nothing.
    plan = caller.plan(entry, "set_auto_update_info")
    if not plan["allowed"]:
        raise Forbidden("Changing automatic firmware updates is a level 4 action and the owner's policy blocks it.")
    if confirm != plan.get("confirm"):
        raise NeedsConfirmation(f"Changing automatic firmware updates requires confirm='set_auto_update_info {clean(entry.slug)}'.")
    current = await _call(caller, entry, "get_auto_update_info")
    params = _pick(current, ("enable", "time", "random_range"))
    params["enable"] = enabled
    if time is not None:
        params["time"] = time
    if random_range is not None:
        params["random_range"] = random_range
    return await _write(registry, caller, entry, "set_auto_update_info", params, changed={"auto_update": params}, confirm=confirm)


async def firmware_install(registry, caller, device, version, confirm=None):
    entry = registry.resolve_one(device)
    _firmware(entry)
    plan = caller.plan(entry, "fw_download")
    if not plan["allowed"]:
        raise Forbidden("Firmware installation is a level 4 action and the owner's policy blocks it.")
    if confirm != plan.get("confirm"):
        raise NeedsConfirmation(f"Firmware installation requires confirm='fw_download {clean(entry.slug)}'.")
    latest = await _call(caller, entry, "get_latest_fw")
    if not isinstance(version, str) or not version or len(version) > 128:
        raise RegistryError("Provide the expected firmware version.")
    if not latest.get("type") or latest.get("fw_ver") != version:
        raise RegistryError("The available firmware does not match the requested version, or no update is available.")
    result = await _write(registry, caller, entry, "fw_download", changed={"firmware_install": "started", "version": clean(version, 128)}, confirm=confirm)
    result["status"] = "Start request accepted. Use firmware_progress to check completion."
    return result


async def diagnostics(registry, caller, device):
    entry = registry.resolve_one(device)
    if _camera_dialect(entry):
        if entry.root is not None:
            # A child camera gets normalized basic info and components from its parent.
            out = _pick(getattr(entry.device, "sys_info", {}), ("model", "fw_ver", "hw_ver"))
            for field in ("rssi", "signal_level", "uptime", "cam_uptime"):
                value = getattr(entry.device, "sys_info", {}).get(field)
                if type(value) in (int, float):
                    out.update(_pick({field: value}, (field,)))
            components = getattr(entry.device, "_components_raw", {})
            safe = [_pick(item, ("name", "version")) for item in components.get("app_component_list", [])[:100] if isinstance(item, dict)]
            safe = [item for item in safe if item]
            return {"device": clean(entry.slug), "source": "cached parent state", **out, **({"components": safe} if safe else {})}
        if entry.kind == "hub":
            # Common camera reads are not declared for hubs in the shared catalog.
            # The negotiated responses are already available, without another wire call.
            raw = entry.device.internal_state.get("getDeviceInfo", {})
        else:
            raw = await _call(caller, entry, "getDeviceInfo", {"device_info": {"name": ["basic_info", "info"]}})
        info = raw.get("device_info", {}).get("basic_info", {})
        out = _pick(info, ("device_model", "sw_version", "hw_version"))
        if entry.kind == "hub":
            components = entry.device.internal_state.get("getAppComponentList", {})
            out["source"] = "cached negotiation"
        else:
            components = await _call(caller, entry, "getAppComponentList", {"app_component": {"name": "app_component_list"}})
        items = components.get("app_component", {}).get("app_component_list", [])
    else:
        _smart(entry)
        info = await _call(caller, entry, "get_device_info")
        out = _pick(info, ("model", "fw_ver", "hw_ver"))
        for field in ("rssi", "signal_level", "on_time", "uptime"):
            if type(info.get(field)) in (int, float):
                out.update(_pick(info, (field,)))
        if entry.root is not None:
            # Children receive component negotiation through their parent, not component_nego.
            components = getattr(entry.device, "_components_raw", {})
        else:
            components = await _call(caller, entry, "component_nego")
        items = components.get("component_list", [])
    safe = [_pick(item, ("id", "ver_code", "name", "version")) for item in items[:100] if isinstance(item, dict)]
    safe = [item for item in safe if item]
    if safe:
        out["components"] = safe
    return {"device": clean(entry.slug), **out}


def compatibility_info() -> dict[str, Any]:
    return {"detectable_locally": False, "account_switch": "homeAssistantCompatibility",
            "status": "unknown", "note": "Third-Party Compatibility may gate local access. Its account setting cannot be read locally. Local authentication failure alone does not identify this switch."}


# These collections are local data, never cloud families or device-side group ids.
def _state_path() -> Path:
    return Path(os.environ.get("TOLPO_STATE_DIR", str(Path.home() / ".local/state/tolpo"))) / "rooms.json"


def _load_map(path):
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or set(data) != {"version", "rooms", "groups"}:
            raise ValueError
        for collection in ("rooms", "groups"):
            if not isinstance(data[collection], dict) or len(data[collection]) > 256:
                raise ValueError
            seen = set()
            for name, members in data[collection].items():
                if _name(name) != name or name.casefold() in seen or not isinstance(members, list) or len(members) > 256:
                    raise ValueError
                seen.add(name.casefold())
                if len(set(members)) != len(members) or any(not isinstance(s, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", s) for s in members):
                    raise ValueError
        return data
    except FileNotFoundError:
        return {"version": 1, "rooms": {}, "groups": {}}
    except (OSError, ValueError, TypeError, UnicodeDecodeError, RegistryError):
        raise RegistryError("The local rooms file is unreadable or invalid; it was not replaced.") from None


def _find(collection, name):
    wanted = _name(name).casefold()
    for saved in collection:
        if saved.casefold() == wanted:
            return saved
    raise NotFound("No local room or group has that name.")


def resolve_room(name: str) -> list[str]:
    """Resolve a tolpo room to saved slugs. No discovery or device action."""
    rooms = _load_map(_state_path())["rooms"]
    return list(rooms[_find(rooms, name)])


@contextmanager
def _locked_map():
    # flock coordinates separate tolpo processes; os.replace makes readers see a complete file.
    try:
        import fcntl
    except ImportError:  # Windows has no flock. The replace stays atomic, only the lock is skipped.
        fcntl = None

    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600), "r+") as lock:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            data = _load_map(path)
            yield data
            if any(len(data[k]) > 256 for k in ("rooms", "groups")):
                raise RegistryError("At most 256 rooms and 256 groups can be stored.")
            fd, temporary = tempfile.mkstemp(prefix=".rooms-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    except OSError:
        raise RegistryError("The local rooms file could not be saved.") from None


def local_collection(registry, collection, action, name=None, new_name=None, devices=None):
    if collection not in ("rooms", "groups") or action not in ("list", "create", "rename", "delete", "assign"):
        raise RegistryError("Unknown local collection operation.")
    if action == "list":
        return {collection: [{"name": n, "devices": members} for n, members in sorted(_load_map(_state_path())[collection].items())]}
    if registry.cfg.tier < Tier.CONTROL:
        raise Forbidden("Changing local rooms or groups is a level 1 action and the owner's policy blocks it.")
    name = _name(name)
    members = None
    if action == "assign":
        if not isinstance(devices, list) or len(devices) > 256:
            raise RegistryError("Provide a list of at most 256 devices.")
        members = list(dict.fromkeys(registry.resolve_one(ref).slug for ref in devices))
    with _locked_map() as data:
        items = data[collection]
        if action == "create":
            if any(n.casefold() == name.casefold() for n in items):
                raise RegistryError("A local room or group already has that name.")
            items[name] = []
        else:
            name = _find(items, name)
            if action == "delete":
                del items[name]
            elif action == "rename":
                new_name = _name(new_name)
                if any(n != name and n.casefold() == new_name.casefold() for n in items):
                    raise RegistryError("A local room or group already has that name.")
                items[new_name] = items.pop(name)
            else:
                items[name] = members
                if collection == "rooms":
                    for other in items:
                        if other != name:
                            items[other] = [slug for slug in items[other] if slug not in members]
    return {"collection": collection, "action": action, "name": new_name if action == "rename" else name,
            **({"devices": members} if members is not None else {})}


async def room_power(registry, caller, name, on):
    if registry.cfg.tier < Tier.CONTROL:
        raise Forbidden("Room power is a level 1 action and the owner's policy blocks it.")
    if type(on) is not bool:
        raise RegistryError("Power state must be a boolean.")
    slugs = resolve_room(name)
    entries = [registry.by_slug(slug) for slug in slugs]
    # Check the whole room first. Camera state is privacy, never a power relay.
    for entry in entries:
        if entry.kind not in policy.POWER_KINDS:
            raise Unsupported(f"{clean(entry.slug)}: room power supports only lights, plugs and switches.")
        registry._require_write(entry, "state")
    results = []
    for entry in entries:
        try:
            await registry.set_power(entry, on)
            results.append({"device": clean(entry.slug), "ok": True, "on": on,
                            **({"refresh": "Device state was not verified."} if not entry.online else {})})
        except RegistryError:
            results.append({"device": clean(entry.slug), "ok": False, "error": "Power write failed; device is unavailable."})
    return {"room": _name(name), "results": results}


def tools(ctx):
    async def run(fn, device, *args, **kwargs):
        await ctx.ready_for([device])
        with ctx.translated():
            return await fn(ctx.registry, ctx.caller, device, *args, **kwargs)

    @ctx.tool(title="Rename device", tier=Tier.SETTING)
    async def device_rename(device: DeviceRef, name: Annotated[str, Field(description="New device alias", min_length=1, max_length=64)]) -> dict[str, Any]:
        """Rename a SMART device, camera or camera protocol hub. Its current tolpo slug stays usable."""
        return await run(rename_device, device, name)

    @ctx.tool(title="Device icon and location", tier=Tier.SETTING)
    async def device_set_metadata(device: DeviceRef, icon: Annotated[str | None, Field(description="Built-in avatar identifier")] = None,
                                  latitude: Annotated[float | None, Field(description="Latitude in degrees", ge=-90, le=90)] = None,
                                  longitude: Annotated[float | None, Field(description="Longitude in degrees", ge=-180, le=180)] = None) -> dict[str, Any]:
        """Set a SMART device icon or sunrise location. Both coordinates are required together."""
        return await run(set_metadata, device, icon, latitude, longitude)

    @ctx.tool(title="Read device clock")
    async def device_get_clock(device: DeviceRef) -> dict[str, Any]:
        """Read device time and time zone."""
        return await run(device_clock, device)

    @ctx.tool(title="Set device clock", tier=Tier.SETTING)
    async def device_set_clock(device: DeviceRef, timezone: Annotated[str, Field(description="IANA time zone, for example Europe/Berlin")],
                               timestamp: Annotated[int | None, Field(description="Unix seconds; omitted means now on SMART. Cameras support only the zone.", ge=0, strict=True)] = None) -> dict[str, Any]:
        """Set SMART time and zone. Cameras accept only the zone, with timestamp omitted."""
        return await run(device_clock, device, timezone, timestamp)

    @ctx.tool(title="Read device LED")
    async def device_get_led(device: DeviceRef) -> dict[str, Any]:
        """Read the indicator rule. SMART uses cached LED data to avoid repeated device queries."""
        return await run(device_led, device)

    @ctx.tool(title="Set device LED", tier=Tier.SECURITY)
    async def device_set_led(device: DeviceRef, rule: Annotated[Literal["always", "never", "night_mode"], Field(description="Indicator LED rule")],
                             start: Minute | None = None, end: Minute | None = None,
                             confirm: Annotated[str | None, Field(description="Exact phrase: setLedStatus <device slug>. Needed for cameras only")] = None) -> dict[str, Any]:
        """Set an LED rule or custom night schedule. Cameras support always and never only. Turning a camera light off
        hides that it is recording, so on a camera this is a level 3 action that needs the confirmation phrase."""
        return await run(device_led, device, rule, start, end, confirm)

    @ctx.tool(title="Read firmware")
    async def firmware_get(device: DeviceRef) -> dict[str, Any]:
        """Read SMART versions, automatic update settings and locally cached release notes."""
        return await run(firmware_info, device)

    @ctx.tool(title="Check firmware", tier=Tier.CONTROL)
    async def firmware_check(device: DeviceRef) -> dict[str, Any]:
        """Ask a SMART device to check the vendor cloud for an update and return local release notes. The device makes
        the request, not tolpo, and it can take longer than a local read."""
        return await run(firmware_info, device, check=True)

    @ctx.tool(title="Firmware progress")
    async def firmware_progress(device: DeviceRef) -> dict[str, Any]:
        """Read SMART firmware download and install progress. No polling or retries."""
        return await run(firmware_info, device, progress=True)

    @ctx.tool(title="Automatic firmware updates", tier=Tier.LIFECYCLE)
    async def firmware_set_auto_update(device: DeviceRef, enabled: Annotated[bool, Field(description="Enable automatic updates", strict=True)],
                                       time: Minute | None = None,
                                       random_range: Annotated[int | None, Field(description="Random spread in minutes", ge=0, le=1439, strict=True)] = None,
                                       confirm: AutoConfirm = None) -> dict[str, Any]:
        """Change SMART automatic updates with confirmation. When on, the device installs firmware and reboots by itself.
        Omitted window fields keep their current values."""
        return await run(firmware_auto_update, device, enabled, time, random_range, confirm)

    @ctx.tool(title="Install firmware", tier=Tier.LIFECYCLE)
    async def firmware_install_update(device: DeviceRef, version: Annotated[str, Field(description="Exact available firmware version to install", min_length=1, max_length=128)],
                                      confirm: Confirm = None) -> dict[str, Any]:
        """Start one SMART firmware install with confirmation. The device fetches the image and reboots."""
        return await run(firmware_install, device, version, confirm)

    @ctx.tool(title="Device diagnostics")
    async def device_diagnostics(device: DeviceRef) -> dict[str, Any]:
        """Read versions, available signal and runtime counters and components. No secrets or log export."""
        return await run(diagnostics, device)

    @ctx.tool(title="Third-Party Compatibility awareness")
    async def device_third_party_compatibility() -> dict[str, Any]:
        """Explain the account switch that may gate local access. Its value cannot be detected locally."""
        return compatibility_info()

    def local(kind, action, name=None, new_name=None, devices=None):
        with ctx.translated():
            return local_collection(ctx.registry, kind, action, name, new_name, devices)

    @ctx.tool(title="List local rooms")
    async def room_list() -> dict[str, Any]:
        """List tolpo's local rooms and their device slugs."""
        return local("rooms", "list")

    @ctx.tool(title="Create local room", tier=Tier.CONTROL)
    async def room_create(name: Name) -> dict[str, Any]:
        """Create an empty room in tolpo's local file."""
        return local("rooms", "create", name)

    @ctx.tool(title="Rename local room", tier=Tier.CONTROL)
    async def room_rename(name: Name, new_name: Name) -> dict[str, Any]:
        """Rename a local room and keep its device assignments."""
        return local("rooms", "rename", name, new_name)

    @ctx.tool(title="Delete local room", tier=Tier.CONTROL)
    async def room_delete(name: Name) -> dict[str, Any]:
        """Delete a local room without changing any device."""
        return local("rooms", "delete", name)

    @ctx.tool(title="Assign local room devices", tier=Tier.CONTROL)
    async def room_assign(name: Name, devices: Annotated[list[str], Field(description="Exact device refs replacing room membership; empty clears it", max_length=256)]) -> dict[str, Any]:
        """Replace room membership. A device belongs to at most one local room."""
        await ctx.ready_for(devices)
        return local("rooms", "assign", name, devices=devices)

    @ctx.tool(title="Set room power", tier=Tier.CONTROL)
    async def room_set_power(name: Name, on: Annotated[bool, Field(description="Power state for all room lights, plugs and switches", strict=True)]) -> dict[str, Any]:
        """Set room power after checking every member. Report each failed device separately."""
        with ctx.translated():
            slugs = resolve_room(name)
        await ctx.ready_for(slugs)
        with ctx.translated():
            return await room_power(ctx.registry, ctx.caller, name, on)

    @ctx.tool(title="List local groups")
    async def group_list() -> dict[str, Any]:
        """List tolpo's local groups and their device slugs."""
        return local("groups", "list")

    @ctx.tool(title="Create local group", tier=Tier.CONTROL)
    async def group_create(name: Name) -> dict[str, Any]:
        """Create an empty local group. It does not change Tapo app groups."""
        return local("groups", "create", name)

    @ctx.tool(title="Rename local group", tier=Tier.CONTROL)
    async def group_rename(name: Name, new_name: Name) -> dict[str, Any]:
        """Rename a local group and keep its members."""
        return local("groups", "rename", name, new_name)

    @ctx.tool(title="Delete local group", tier=Tier.CONTROL)
    async def group_delete(name: Name) -> dict[str, Any]:
        """Delete a local group without changing any device."""
        return local("groups", "delete", name)

    @ctx.tool(title="Assign local group devices", tier=Tier.CONTROL)
    async def group_assign(name: Name, devices: Annotated[list[str], Field(description="Exact device refs replacing group membership; empty clears it", max_length=256)]) -> dict[str, Any]:
        """Replace local group members. Devices can belong to several groups."""
        await ctx.ready_for(devices)
        return local("groups", "assign", name, devices=devices)
