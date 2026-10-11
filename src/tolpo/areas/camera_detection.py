"""Camera detection, alarm actions and local event metadata. No media or cloud calls."""

import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from kasa.exceptions import AuthenticationError
from pydantic import Field, StrictInt, StrictStr

from .. import policy
from ..policy import Tier
from ..registry import BadRequest, DeviceUnavailable
from ..views import clean

KINDS = ["camera", "doorbell"]
# component, module, section, method stem, preferred sensitivity field
DETECTIONS = {
    "motion": ("detection", "motion_detection", "motion_det", "Detection", "digital_sensitivity"),
    "person": ("personDetection", "people_detection", "detection", "PersonDetection", "sensitivity"),
    "pet": ("petDetection", "pet_detection", "detection", "PetDetection", "sensitivity"),
    "vehicle": ("vehicleDetection", "vehicle_detection", "detection", "VehicleDetection", "sensitivity"),
    "line_crossing": ("linecrossingDetection", "linecrossing_detection", "detection", "LinecrossingDetection", "sensitivity"),
    "intrusion": ("intrusionDetection", "intrusion_detection", "detection", "IntrusionDetection", "sensitivity"),
    "tamper": ("tamperDetection", "tamper_detection", "tamper_det", "TamperDetection", "digital_sensitivity"),
    "baby_cry": ("babyCryDetection", "sound_detection", "bcd", "BCD", "sensitivity_v2"),
    "bark": ("barkDetection", "bark_detection", "detection", "BarkDetection", "sensitivity"),
    "meow": ("meowDetection", "meow_detection", "detection", "MeowDetection", "sensitivity"),
    "glass_break": ("glassDetection", "glass_detection", "detection", "GlassDetection", "sensitivity"),
    "package": ("packageDetection", "package_detection", "detection", "PackageDetection", "sensitivity"),
}
Detection = Literal["motion", "person", "pet", "vehicle", "line_crossing", "intrusion", "tamper", "baby_cry", "bark", "meow", "glass_break", "package"]
ZONES = {
    "motion": ("getDetectionConfig", "addDetectionRegion", "add_md_regions"),
    "line_crossing": ("getLinecrossingDetectionRegion", "addLinecrossingDetectionRegion", "add_regions"),
    "intrusion": ("getIntrusionDetectionRegion", "addIntrusionDetectionRegion", "add_regions"),
    "package": ("getPackageDetectionRegion", "addPackageDetectionRegion", "add_pkgd_regions"),
}
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
EVENT_TYPES = {
    1: "timing", 2: "motion", 3: "tamper", 4: "line_crossing", 5: "intrusion", 6: "person",
    7: "baby_cry", 8: "vehicle", 9: "pet", 10: "ring", 11: "bark", 12: "meow", 13: "glass_break",
    14: "smoke_alarm", 15: "package_delivered", 16: "package_picked_up", 17: "doorbell_missed",
    18: "doorbell_answered", 19: "anti_theft", 23: "baby_left_bed", 24: "caregiver", 25: "baby_asleep",
    26: "baby_awake", 27: "face_cover", 28: "safe_fence_out", 30: "baby_motion", 31: "panoramic_video",
    33: "animal", 36: "co_alarm",
}

METHODS = []
for _component, _module, _section, _stem, _sensitivity in DETECTIONS.values():
    METHODS.extend([
        policy.method(f"get{_stem}Config", Tier.READ, KINDS, "Read a camera detection setting."),
        policy.method(f"set{_stem}Config", Tier.SETTING, KINDS, "Change camera detection behavior."),
    ])
for _kind, (_get, _add, _section) in ZONES.items():
    if _get != "getDetectionConfig":
        METHODS.append(policy.method(_get, Tier.READ, KINDS, "Read camera detection zones."))
    METHODS.append(policy.method(_add, Tier.SETTING, KINDS, "Replace camera detection zones."))
for _stem in ("LinecrossingDetection", "IntrusionDetection"):
    METHODS.append(policy.method(f"set{_stem}Schedule", Tier.SETTING, KINDS, "Change camera detection arming windows."))
for _method in ("getPersonDetectionCapability", "getBCDCapability", "getAlertConfig", "getAlertEventType",
                "getManualAlarmStatus", "getWhitelampCapability", "getWhitelampConfig", "getFloodlightCapability",
                "getFloodlightConfig", "getLastAlarmInfo", "searchDetectionList", "getMsgPushEventList"):
    METHODS.append(policy.method(_method, Tier.READ, KINDS, "Read camera detection or alert information."))
for _method in ("setWhitelampConfig", "setFloodlightConfig", "setMsgPushEventList"):
    METHODS.append(policy.method(_method, Tier.SETTING, KINDS, "Change camera lighting or notification behavior."))
for _method in ("setAlertConfig", "setAlertEventType", "doManualAlarm", "manualFloodlightOp", "setForceWhitelampState"):
    METHODS.append(policy.method(_method, Tier.SECURITY, KINDS, "Change camera alarm actions that can sound or flash."))


def _camera(registry, device):
    entry = registry.resolve_one(device)
    if entry.kind not in KINDS:
        raise BadRequest(f"{clean(entry.slug)} cannot use camera detection or alarm tools")
    return entry


async def _call(caller, entry, method, params, *, confirm=None):
    try:
        result = await caller.call(entry, method, params, confirm=confirm)
    except DeviceUnavailable as exc:
        reason = "rejected the account credentials" if isinstance(exc.__cause__, AuthenticationError) else (
            "did not answer in time" if isinstance(exc.__cause__, TimeoutError) else "reported an error")
        raise DeviceUnavailable(f"{clean(entry.slug)} {reason} for {method}") from None
    # Some canned or older protocol responses retain the envelope. Do not return raw errors.
    if isinstance(result, dict) and result.get("error_code", 0) != 0:
        raise DeviceUnavailable(f"{clean(entry.slug)} reported an error for {method}")
    if isinstance(result, dict) and "result" in result:
        result = result["result"]
    if not isinstance(result, dict):
        raise DeviceUnavailable(f"{clean(entry.slug)} returned an invalid response for {method}")
    return result


async def _components(caller, entry):
    data = await _call(caller, entry, "getAppComponentList", {"app_component": {"name": "app_component_list"}})
    items = _module(data, "app_component", entry).get("app_component_list")
    if not isinstance(items, list):
        raise DeviceUnavailable(f"{clean(entry.slug)} did not report camera components")
    return {item["name"] for item in items if isinstance(item, dict) and isinstance(item.get("name"), str)
            and type(item.get("version")) is int and item["version"] > 0}


def _require(entry, components, component, action):
    if component not in components:
        raise BadRequest(f"{clean(entry.slug)} does not support {action}")


def _module(data, module, entry):
    value = data.get(module)
    if not isinstance(value, dict):
        raise DeviceUnavailable(f"{clean(entry.slug)} did not report {module}")
    return value


def _section(data, module, section, entry):
    value = _module(data, module, entry).get(section)
    if not isinstance(value, dict):
        raise DeviceUnavailable(f"{clean(entry.slug)} did not report {section}")
    return value


def _compact(data, fields):
    """Select known fields only. Flatten strings and bound lists. Never expose raw sections."""
    out = {}
    for key in fields:
        value = data.get(key)
        if value is None or value == "" or value == [] or value == {}:
            continue
        if isinstance(value, str):
            out[key] = clean(value, 128)
        elif isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, list):
            out[key] = [clean(v, 64) if isinstance(v, str) else v for v in value[:32]
                        if isinstance(v, str) and v or key.endswith("_range") and type(v) is int]
            if not out[key]:
                del out[key]
        elif isinstance(value, dict) and key.endswith("_range"):
            bounds = {k: v for k, v in value.items() if k in ("min", "max") and (
                type(v) is int or isinstance(v, str) and re.fullmatch(r"\d{1,12}", v))}
            if len(bounds) == 2:
                out[key] = bounds
    return out


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise BadRequest(f"{label} must be an integer from {low} to {high}")
    return value


def _boolean(value, label):
    if type(value) is not bool:
        raise BadRequest(f"{label} must be true or false")
    return "on" if value else "off"


def _in_range(value, advertised, label):
    """Accept explicit numeric min/max, min-max text, or a list of allowed values. Fail closed."""
    valid = False
    if isinstance(advertised, dict) and set(advertised) >= {"min", "max"}:
        try:
            valid = type(value) is int and int(advertised["min"]) <= value <= int(advertised["max"])
        except (ValueError, TypeError):
            pass
    elif isinstance(advertised, list):
        valid = type(value) in (int, str) and str(value) in [str(v) for v in advertised if type(v) in (int, str)]
    elif isinstance(advertised, str):
        match = re.fullmatch(r"(\d+)-(\d+)", advertised)
        if match:
            valid = type(value) is int and int(match[1]) <= value <= int(match[2])
        else:
            valid = type(value) in (int, str) and str(value) in advertised.split(",")
    if not valid:
        raise BadRequest(f"{label} is outside the reported range or the camera did not report a usable range")
    return str(value)


async def _detection(caller, entry, detection, probe_ranges=False):
    if detection not in DETECTIONS:
        raise BadRequest("Unknown camera detection type")
    component, module, section, stem, preferred = DETECTIONS[detection]
    components = await _components(caller, entry)
    _require(entry, components, component, f"{detection} detection")
    names = [section] + (["arming_schedule"] if detection in ("line_crossing", "intrusion") else [])
    params = {module: {"name": names}}
    data = await _call(caller, entry, f"get{stem}Config", params)
    config = _section(data, module, section, entry)
    capability = _module(data, module, entry).get("capability", {})
    if not isinstance(capability, dict):
        capability = {}
    if probe_ranges and detection in ("person", "baby_cry"):
        method = "getPersonDetectionCapability" if detection == "person" else "getBCDCapability"
        # UNVERIFIED: these method names are audited, but their module/name parameters and response section
        # have no fixture or surviving app builder. Check people_detection/sound_detection.capability on hardware.
        probe = await _call(caller, entry, method, {module: {"name": ["capability"]}})
        capability = _module(probe, module, entry).get("capability", {})
        if not isinstance(capability, dict):
            raise DeviceUnavailable(f"{clean(entry.slug)} returned an invalid detection capability")
    return (module, section, stem, preferred, config, capability, data)


async def detection_get(registry, caller, device, detection, include_ranges=False):
    entry = _camera(registry, device)
    module, section, stem, preferred, config, capability, data = await _detection(caller, entry, detection, include_ranges)
    out = {"device": clean(entry.slug), "detection": detection}
    selected = _compact(config, ("enabled", "sensitivity", "digital_sensitivity", "sensitivity_v2", "det_sensitivity", "enhanced", "target", "type",
                                "people_enabled", "vehicle_enabled", "non_vehicle_enabled"))
    if selected:
        out["settings"] = selected
    ranges = _compact(capability, ("sensitivity_range",))
    if ranges:
        out["capability"] = ranges
    schedule = _module(data, module, entry).get("arming_schedule")
    if isinstance(schedule, dict) and (selected := _compact(schedule, DAYS)):
        out["schedule"] = selected
    return out


async def detection_set(registry, caller, device, detection, enabled=None, sensitivity=None):
    if enabled is None and sensitivity is None:
        raise BadRequest("Choose enabled or sensitivity for the detection change")
    patch = {}
    if enabled is not None:
        patch["enabled"] = _boolean(enabled, "enabled")
    if sensitivity is not None and type(sensitivity) not in (int, str):
        raise BadRequest("sensitivity must be an integer or an advertised choice")
    entry = _camera(registry, device)
    module, section, stem, preferred, config, capability, data = await _detection(caller, entry, detection, sensitivity is not None)
    if sensitivity is not None:
        candidates = (preferred, "digital_sensitivity", "sensitivity") if detection == "baby_cry" else (preferred, "sensitivity")
        field = next((key for key in candidates if key in config and config[key] not in (None, "")), None)
        if field is None:
            raise BadRequest(f"{clean(entry.slug)} does not support {detection} sensitivity tuning")
        patch[field] = _in_range(sensitivity, capability.get("sensitivity_range", config.get("sensitivity_range")), "sensitivity")
    await _call(caller, entry, f"set{stem}Config", {module: {section: patch}})
    await registry.refresh(entry, force=True)
    return {"device": clean(entry.slug), "detection": detection, "changed": patch}


def _zone_fields(detection):
    if detection == "motion":
        return ("x_coor", "y_coor", "width", "height")
    count = 2 if detection == "line_crossing" else 4 if detection == "intrusion" else 8
    return tuple(f"pt{i}_{axis}" for i in range(1, count + 1) for axis in ("x", "y")) + (
        ("direction", "sensitivity") if detection == "line_crossing" else
        ("sensitivity", "threshold", "percentage") if detection == "intrusion" else ("point_num",))


async def zones_get(registry, caller, device, detection):
    if detection not in ZONES:
        raise BadRequest(f"{detection} does not have separate zones in this area")
    entry = _camera(registry, device)
    component, module, section, stem, preferred = DETECTIONS[detection]
    _require(entry, await _components(caller, entry), component, f"{detection} zones")
    method = ZONES[detection][0]
    data = await _call(caller, entry, method, {module: {"table": ["region_info"]}})
    raw = _module(data, module, entry).get("region_info")
    if not isinstance(raw, list):
        raise DeviceUnavailable(f"{clean(entry.slug)} did not report detection zones")
    zones = []
    for item in raw[:16]:
        if not isinstance(item, dict):
            continue
        # Older cameras name each table row, for example region_info_1.
        if not any(key in item for key in _zone_fields(detection)):
            item = next((v for v in item.values() if isinstance(v, dict)), {})
        if selected := _compact(item, _zone_fields(detection)):
            zones.append(selected)
    out = {"device": clean(entry.slug), "detection": detection, "zone_count": len(raw)}
    if zones:
        out["zones"] = zones
    return out


def _validate_zones(detection, zones):
    if detection not in ZONES:
        raise BadRequest("This detection does not have separate zones in this area")
    if not isinstance(zones, list) or len(zones) > 4:
        raise BadRequest("zones must be a list of at most four zones")
    wire = []
    for zone in zones:
        if not isinstance(zone, dict) or set(zone) - set(_zone_fields(detection)):
            raise BadRequest("Zone contains unsupported fields")
        coords = _zone_fields(detection)[:4] if detection == "motion" else tuple(
            f"pt{i}_{axis}" for i in range(1, (2 if detection == "line_crossing" else 4 if detection == "intrusion"
                                            else _integer(zone.get("point_num"), 3, 8, "point_num")) + 1)
            for axis in ("x", "y"))
        allowed = set(coords) | ({"direction", "sensitivity"} if detection == "line_crossing" else
                                {"sensitivity", "threshold", "percentage"} if detection == "intrusion" else
                                {"point_num"} if detection == "package" else set())
        if set(zone) != allowed:
            raise BadRequest("Zone must include exactly its coordinates and detection fields")
        row = {key: str(_integer(zone.get(key), 0, 10000, key)) for key in coords}
        if detection == "motion":
            if zone["width"] <= 0 or zone["height"] <= 0 or zone["x_coor"] + zone["width"] > 10000 or zone["y_coor"] + zone["height"] > 10000:
                raise BadRequest("Motion rectangle must have positive size and fit inside the image")
        elif detection == "line_crossing":
            if (zone["pt1_x"], zone["pt1_y"]) == (zone["pt2_x"], zone["pt2_y"]):
                raise BadRequest("Line crossing needs two distinct points")
            if zone["direction"] not in ("both", "AtoB", "BtoA"):
                raise BadRequest("direction must be both, AtoB or BtoA")
            row["direction"] = zone["direction"]
            row["sensitivity"] = str(_integer(zone["sensitivity"], 0, 100, "sensitivity"))
        elif detection == "intrusion":
            for key in ("sensitivity", "threshold", "percentage"):
                row[key] = str(_integer(zone[key], 0, 100, key))
        elif detection == "package":
            row["point_num"] = str(zone["point_num"])
        wire.append(row)
    return wire


async def zones_set(registry, caller, device, detection, zones):
    wire = _validate_zones(detection, zones)
    entry = _camera(registry, device)
    component, module, section, stem, preferred = DETECTIONS[detection]
    _require(entry, await _components(caller, entry), component, f"{detection} zones")
    method, write_section = ZONES[detection][1:]
    # Motion, intrusion and package wrap their rows in region_info; line crossing sends the list directly.
    payload = wire if detection == "line_crossing" else {"region_info": wire}
    await _call(caller, entry, method, {module: {write_section: payload}})
    await registry.refresh(entry, force=True)
    return {"device": clean(entry.slug), "detection": detection, "changed": {"zones": wire}}


async def schedule_set(registry, caller, device, detection, schedule):
    if detection not in ("line_crossing", "intrusion"):
        raise BadRequest("Only line crossing and intrusion have detection arming schedules")
    if not isinstance(schedule, dict) or not schedule or set(schedule) - set(DAYS):
        raise BadRequest("schedule must name weekdays from monday to sunday")
    patch = {}
    for day, windows in schedule.items():
        if not isinstance(windows, list) or len(windows) > 6:
            raise BadRequest("Each day accepts up to six HHMM-HHMM windows")
        last = -1
        for window in windows:
            if not isinstance(window, str) or not re.fullmatch(r"\d{4}-\d{4}", window):
                raise BadRequest("Arming windows must use HHMM-HHMM")
            start, end = window.split("-")
            if not (int(start[:2]) < 24 and int(start[2:]) < 60 and
                    (end == "2400" or int(end[:2]) < 24 and int(end[2:]) < 60)) or not last <= int(start) < int(end):
                raise BadRequest("Arming windows must be valid, ordered and not overlap")
            last = int(end)
        patch[day] = json.dumps(windows, separators=(",", ":"))
    entry = _camera(registry, device)
    module, section, stem, preferred, config, capability, data = await _detection(caller, entry, detection)
    current = _module(data, module, entry).get("arming_schedule")
    if not isinstance(current, dict) or not all(isinstance(current.get(day), str) for day in DAYS):
        raise BadRequest(f"{clean(entry.slug)} did not report a complete arming schedule")
    merged = {day: patch.get(day, current[day]) for day in DAYS}
    await _call(caller, entry, f"set{stem}Schedule", {module: {"arming_schedule": merged}})
    await registry.refresh(entry, force=True)
    return {"device": clean(entry.slug), "detection": detection, "changed": {"schedule": patch}}


def tools(ctx):
    _other_tools(ctx)
    @ctx.tool(title="Camera detection settings")
    async def camera_detect_get(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
        detection: Annotated[Detection, Field(description="Detection type to read")],
        include_ranges: Annotated[bool, Field(description="Also probe sensitivity capabilities for person and baby cry", strict=True)] = False,
    ) -> dict[str, Any]:
        """Read a supported detection switch, sensitivity range and arming schedule."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await detection_get(ctx.registry, ctx.caller, device, detection, include_ranges)

    @ctx.tool(title="Change camera detection", tier=Tier.SETTING)
    async def camera_detect_set(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
        detection: Annotated[Detection, Field(description="Detection type to change")],
        enabled: Annotated[bool | None, Field(description="Enable or disable this detection", strict=True)] = None,
        sensitivity: Annotated[StrictInt | StrictStr | None, Field(description="Sensitivity inside the range reported by this model")] = None,
    ) -> dict[str, Any]:
        """Change detection behavior at level 2. Missing fields and ranges are refused."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await detection_set(ctx.registry, ctx.caller, device, detection, enabled, sensitivity)

    @ctx.tool(title="Camera detection zones")
    async def camera_detect_zones_get(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
        detection: Annotated[Detection, Field(description="Motion, line_crossing, intrusion or package")],
    ) -> dict[str, Any]:
        """Read the zones for a detection that supports separate regions."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await zones_get(ctx.registry, ctx.caller, device, detection)

    @ctx.tool(title="Replace camera detection zones", tier=Tier.SETTING)
    async def camera_detect_zones_set(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
        detection: Annotated[Detection, Field(description="Motion, line_crossing, intrusion or package")],
        zones: Annotated[list[dict[str, Any]], Field(description="Up to four zones with integer coordinates 0..10000. See camera detection docs for fields. Empty clears zones.")],
    ) -> dict[str, Any]:
        """Replace supported detection zones at level 2. Extra fields are refused."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await zones_set(ctx.registry, ctx.caller, device, detection, zones)

    @ctx.tool(title="Change camera arming windows", tier=Tier.SETTING)
    async def camera_detect_schedule_set(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
        detection: Annotated[Detection, Field(description="Only line_crossing or intrusion")],
        schedule: Annotated[dict[str, list[str]], Field(description="Weekday names mapped to ordered HHMM-HHMM windows. Empty day disables it. Other days stay unchanged.")],
    ) -> dict[str, Any]:
        """Change arming windows at level 2 for line crossing or intrusion."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await schedule_set(ctx.registry, ctx.caller, device, detection, schedule)


ALARM_FIELDS = ("enabled", "alarm_mode", "alarm_type", "alarm_volume", "alarm_duration", "light_type",
                "light_alarm_enabled", "sound_alarm_enabled", "light_rbl_enabled")
ALARM_CAPS = ("alarm_func", "alarm_type", "alarm_duration_support", "alarm_volume_support",
              "red_blue_light_alarm_support", "alert_event_type_support", "alarm_volume_range", "light_type_range")


async def _alarm(caller, entry):
    _require(entry, await _components(caller, entry), "alert", "camera alarm actions")
    data = await _call(caller, entry, "getAlertConfig", {"msg_alarm": {"name": ["chn1_msg_alarm_info", "capability"]}})
    config = _section(data, "msg_alarm", "chn1_msg_alarm_info", entry)
    capability = _module(data, "msg_alarm", entry).get("capability", {})
    if not isinstance(capability, dict):
        raise DeviceUnavailable(f"{clean(entry.slug)} returned an invalid alarm capability")
    return config, capability


async def alarm_get(registry, caller, device):
    entry = _camera(registry, device)
    config, capability = await _alarm(caller, entry)
    out = {"device": clean(entry.slug)}
    if selected := _compact(config, ALARM_FIELDS):
        out["settings"] = selected
    if selected := _compact(capability, ALARM_CAPS):
        out["capability"] = selected
    return out


def _supported_flag(capability, key, entry, action):
    if capability.get(key) not in ("on", "1", 1, True):
        raise BadRequest(f"{clean(entry.slug)} does not support {action}")


async def alarm_set(registry, caller, device, settings, confirm=None):
    if not isinstance(settings, dict) or not settings or set(settings) - set(ALARM_FIELDS):
        raise BadRequest("Choose only supported camera alarm fields")
    entry = _camera(registry, device)
    config, capability = await _alarm(caller, entry)
    patch = {}
    for key, value in settings.items():
        if key not in config:
            raise BadRequest(f"{clean(entry.slug)} does not report alarm field {key}")
        if key == "enabled" or key.endswith("enabled"):
            patch[key] = _boolean(value, key)
            if key == "light_rbl_enabled":
                _supported_flag(capability, "red_blue_light_alarm_support", entry, "red and blue alarm lights")
            if key in ("sound_alarm_enabled", "light_alarm_enabled"):
                mode = "sound" if key.startswith("sound") else "light"
                if mode not in capability.get("alarm_func", []):
                    raise BadRequest(f"{clean(entry.slug)} does not support {mode} alarm actions")
        elif key == "alarm_mode":
            choices = capability.get("alarm_func", [])
            if not isinstance(value, list) or not value or any(v not in ("sound", "light") or v not in choices for v in value) or len(value) != len(set(value)):
                raise BadRequest("alarm_mode must select supported sound and light actions")
            patch[key] = value
        elif key == "alarm_type":
            patch[key] = _in_range(value, capability.get("alarm_type"), "alarm_type")
        elif key == "alarm_volume":
            _supported_flag(capability, "alarm_volume_support", entry, "alarm volume")
            # UNVERIFIED: numeric camera volume encoding is not established by the audit.
            # Refuse unless the device supplies explicit alarm_volume_range choices or bounds.
            patch[key] = _in_range(value, capability.get("alarm_volume_range"), key)
        elif key == "alarm_duration":
            _supported_flag(capability, "alarm_duration_support", entry, "alarm duration")
            patch[key] = str(_integer(value, 1, 600, key))
        elif key == "light_type":
            if "light" not in capability.get("alarm_func", []):
                raise BadRequest(f"{clean(entry.slug)} does not support light alarm actions")
            # UNVERIFIED: fixtures return numeric light_type "1", while the audit's newer enum
            # uses flicker/lighting. Do not translate numeric values without a reported range.
            choices = ("flicker", "lighting") if config[key] in ("flicker", "lighting") else capability.get("light_type_range")
            patch[key] = _in_range(value, list(choices) if isinstance(choices, tuple) else choices, key)
    await _call(caller, entry, "setAlertConfig", {"msg_alarm": {"chn1_msg_alarm_info": patch}}, confirm=confirm)
    await registry.refresh(entry, force=True)
    return {"device": clean(entry.slug), "changed": patch}


async def manual_alarm(registry, caller, device, action=None, confirm=None):
    if action is not None and action not in ("start", "stop"):
        raise BadRequest("Manual alarm action must be start or stop")
    entry = _camera(registry, device)
    _require(entry, await _components(caller, entry), "manualAlarm", "manual alarm")
    if action is None:
        result = await _call(caller, entry, "getManualAlarmStatus", {"msg_alarm": {"get_manual_alarm_status": ""}})
        status = _section(result, "msg_alarm", "get_manual_alarm_status", entry)
        out = {"device": clean(entry.slug)}
        if selected := _compact(status, ("status", "rest_time")):
            out["status"] = selected
        return out
    await _call(caller, entry, "doManualAlarm", {"msg_alarm": {"manual_alarm": {"action": action}}}, confirm=confirm)
    await registry.refresh(entry, force=True)
    return {"device": clean(entry.slug), "changed": {"manual_alarm": action}}


async def _event_table(caller, entry, notifications):
    component = "msgPush" if notifications else "alert"
    _require(entry, await _components(caller, entry), component, "notification choices" if notifications else "alarm triggers")
    module, section, method = ("msg_push", "msg_push_event", "getMsgPushEventList") if notifications else ("msg_alarm", "msg_alarm_type", "getAlertEventType")
    data = await _call(caller, entry, method, {module: {"table": [section]}})
    rows = _module(data, module, entry).get(section)
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in rows):
        raise DeviceUnavailable(f"{clean(entry.slug)} returned an invalid event choice table")
    return module, section, rows


async def choices_get(registry, caller, device, notifications=False):
    entry = _camera(registry, device)
    module, section, rows = await _event_table(caller, entry, notifications)
    out = {"device": clean(entry.slug), "choice_count": len(rows)}
    if rows:
        out["choices"] = [_compact(row, ("name", "enabled", "interruption_level") if notifications else ("name", "enabled")) for row in rows[:32]]
    return out


async def choices_set(registry, caller, device, event, enabled, interruption_level=None, notifications=False, confirm=None):
    if not isinstance(event, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", event):
        raise BadRequest("event must be an identifier from the device choice table")
    switch = _boolean(enabled, "enabled")
    if interruption_level is not None and (not notifications or interruption_level not in ("none", "general", "critical")):
        raise BadRequest("Notification interruption_level must be none, general or critical")
    entry = _camera(registry, device)
    module, section, rows = await _event_table(caller, entry, notifications)
    found = [row for row in rows if row["name"] == event]
    if len(found) != 1:
        raise BadRequest(f"{clean(entry.slug)} does not report event choice {event}")
    fields = ("name", "enabled", "interruption_level") if notifications else ("name", "enabled")
    # Preserve all other choices, without forwarding arbitrary device fields.
    payload = [{key: row[key] for key in fields if key in row} for row in rows]
    for row in payload:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", row["name"]) or row.get("enabled") not in ("on", "off"):
            raise BadRequest(f"{clean(entry.slug)} reported an unsupported event choice value")
        if notifications and row.get("interruption_level", "none") not in ("none", "general", "critical"):
            raise BadRequest(f"{clean(entry.slug)} reported an unsupported notification level")
        if row["name"] == event:
            row["enabled"] = switch
            if interruption_level is not None:
                if "interruption_level" not in found[0]:
                    raise BadRequest(f"{clean(entry.slug)} does not report notification interruption levels")
                row["interruption_level"] = interruption_level
    method = "setMsgPushEventList" if notifications else "setAlertEventType"
    await _call(caller, entry, method, {module: {section: payload}}, confirm=confirm)
    await registry.refresh(entry, force=True)
    changed = {"event": event, "enabled": switch}
    if interruption_level is not None:
        changed["interruption_level"] = interruption_level
    return {"device": clean(entry.slug), "changed": changed}


async def _lighting(caller, entry, light):
    if light not in ("spotlight", "floodlight"):
        raise BadRequest("light must be spotlight or floodlight")
    _require(entry, await _components(caller, entry), "whiteLamp" if light == "spotlight" else "floodlight", light)
    if light == "spotlight":
        data = await _call(caller, entry, "getWhitelampCapability", {"image_capability": {"name": ["white_lamp"]}})
        capability = _section(data, "image_capability", "white_lamp", entry)
        data = await _call(caller, entry, "getWhitelampConfig", {"image": {"name": ["switch"]}})
        config = _section(data, "image", "switch", entry)
    else:
        data = await _call(caller, entry, "getFloodlightCapability", {"floodlight": {"name": ["capability"]}})
        capability = _section(data, "floodlight", "capability", entry)
        data = await _call(caller, entry, "getFloodlightConfig", {"floodlight": {"name": ["config"]}})
        config = _section(data, "floodlight", "config", entry)
    return config, capability


async def lighting_get(registry, caller, device, light):
    entry = _camera(registry, device)
    config, capability = await _lighting(caller, entry, light)
    fields = ("wtl_intensity_level", "wtl_force_time") if light == "spotlight" else (
        "intensity_level", "auto_off_enabled", "manual_duration", "trigger_duration", "img_det_tri_enabled")
    caps = ("wtl_intensity_range", "time_limit_max_sec", "wtl_intensity_support", "time_limit_support") if light == "spotlight" else (
        "min_intensity", "intensity_level_max", "manual_duration_max", "trigger_duration_max")
    out = {"device": clean(entry.slug), "light": light}
    if selected := _compact(config, fields):
        out["settings"] = selected
    if selected := _compact(capability, caps):
        out["capability"] = selected
    return out


async def lighting_set(registry, caller, device, light, brightness=None, duration=None):
    if brightness is None and duration is None:
        raise BadRequest("Choose lighting brightness or duration")
    if brightness is not None:
        _integer(brightness, 0, 100, "brightness")
    if duration is not None:
        _integer(duration, 1, 86400, "duration")
    entry = _camera(registry, device)
    config, capability = await _lighting(caller, entry, light)
    patch = {}
    if brightness is not None:
        if light == "spotlight":
            _supported_flag(capability, "wtl_intensity_support", entry, "spotlight brightness")
            patch["wtl_intensity_level"] = _in_range(brightness, capability.get("wtl_intensity_range"), "brightness")
        else:
            patch["intensity_level"] = _in_range(brightness, {"min": capability.get("min_intensity"), "max": capability.get("intensity_level_max")}, "brightness")
    if duration is not None:
        if light == "spotlight":
            _supported_flag(capability, "time_limit_support", entry, "spotlight duration")
        cap = capability.get("time_limit_max_sec" if light == "spotlight" else "manual_duration_max")
        patch["wtl_force_time" if light == "spotlight" else "manual_duration"] = _in_range(duration, {"min": 1, "max": cap}, "duration")
    if any(key not in config for key in patch):
        raise BadRequest(f"{clean(entry.slug)} does not report those lighting settings")
    method, module, section = ("setWhitelampConfig", "image", "switch") if light == "spotlight" else ("setFloodlightConfig", "floodlight", "config")
    await _call(caller, entry, method, {module: {section: patch}})
    await registry.refresh(entry, force=True)
    return {"device": clean(entry.slug), "light": light, "changed": patch}


async def lighting_switch(registry, caller, device, light, enabled, confirm=None):
    switch = _boolean(enabled, "enabled")
    entry = _camera(registry, device)
    await _lighting(caller, entry, light)
    if light == "spotlight":
        method, params = "setForceWhitelampState", {"image": {"switch": {"force_wtl_state": switch}}}
    else:
        method, params = "manualFloodlightOp", {"floodlight": {"manual_floodlight_op": {"action": "start" if enabled else "stop"}}}
    await _call(caller, entry, method, params, confirm=confirm)
    await registry.refresh(entry, force=True)
    return {"device": clean(entry.slug), "light": light, "changed": {"enabled": enabled}}


def _timestamp(value):
    if isinstance(value, datetime):
        try:
            number = int(value.timestamp())
            return number if 0 < number <= 253402300799 else None
        except (ValueError, OverflowError, OSError):
            return None
    if type(value) in (int, str):
        try:
            number = int(value)
            return number if 0 < number <= 253402300799 else None
        except (ValueError, OverflowError):
            pass
    return None


def _event(value):
    if type(value) is int or isinstance(value, str):
        try:
            code = int(value)
            return {"code": code, "type": EVENT_TYPES.get(code, "unknown")}
        except ValueError:
            if value in EVENT_TYPES.values():
                return {"type": str(value)}
    return {"type": "unknown"}


async def events_recent(registry, caller, device, start_time=None, end_time=None, limit=20, start_index=0):
    _integer(limit, 1, 100, "limit")
    _integer(start_index, 0, 100000, "start_index")
    if (start_time is None) != (end_time is None):
        raise BadRequest("Supply both start_time and end_time for SD events")
    if start_time is not None:
        _integer(start_time, 1, 253402300799, "start_time")
        _integer(end_time, 1, 253402300799, "end_time")
        if start_time >= end_time or end_time - start_time > 86400:
            raise BadRequest("SD event window must be ordered and no longer than one day")
    entry = _camera(registry, device)
    components = await _components(caller, entry)
    out = {"device": clean(entry.slug)}
    if "detection" in components:
        data = await _call(caller, entry, "getLastAlarmInfo", {"system": {"name": ["last_alarm_info"]}})
        last = _section(data, "system", "last_alarm_info", entry)
        if timestamp := _timestamp(last.get("last_alarm_time")):
            out["last"] = {"timestamp": timestamp, **_event(last.get("last_alarm_type"))}
    elif start_time is None:
        raise BadRequest(f"{clean(entry.slug)} does not support last-alert polling")
    # python-kasa's last alert features are cached values, labeled as such. Raw type names never escape.
    features = entry.device.features
    if "last_alert_timestamp" in features and (timestamp := _timestamp(features["last_alert_timestamp"].value)):
        value = features["last_alert_type"].value if "last_alert_type" in features else None
        out["cached_last"] = {"timestamp": timestamp, **_event(value)}
    if start_time is not None:
        _require(entry, components, "playback", "SD event metadata")
        _require(entry, components, "sdCard", "SD event metadata")
        params = {"playback": {"search_detection_list": {"start_time": start_time, "end_time": end_time,
                  "channel": 0, "start_index": start_index, "end_index": start_index + limit - 1}}}
        data = await _call(caller, entry, "searchDetectionList", params)
        playback = _module(data, "playback", entry)
        rows = playback.get("search_detection_list")
        if not isinstance(rows, list):
            raise DeviceUnavailable(f"{clean(entry.slug)} did not report SD event metadata")
        events = []
        for row in rows[:limit]:
            if not isinstance(row, dict) or not (timestamp := _timestamp(row.get("start_time"))):
                continue
            if not start_time <= timestamp <= end_time:
                continue
            event = {"timestamp": timestamp, **_event(row.get("alarm_type"))}
            if end := _timestamp(row.get("end_time")):
                event["end_time"] = end
            events.append(event)
        events.sort(key=lambda event: event["timestamp"], reverse=True)
        out["event_count"] = len(events)
        if events:
            out["events"] = events
        # The list is one bounded page, sorted locally, not necessarily the newest page on the device.
        out["window"] = {"start_time": start_time, "end_time": end_time, "start_index": start_index}
        if playback.get("to_be_continued") in (1, "1", True):
            out["next_index"] = start_index + len(rows)
    return out


def _other_tools(ctx):
    @ctx.tool(title="Camera alarm settings")
    async def camera_alarm_get(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
    ) -> dict[str, Any]:
        """Read detection-triggered sound and light alarm settings and capabilities."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await alarm_get(ctx.registry, ctx.caller, device)

    @ctx.tool(title="Change camera alarm actions", tier=Tier.SECURITY)
    async def camera_alarm_set(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
        settings: Annotated[dict[str, Any], Field(description="Alarm fields: enabled, alarm_mode, alarm_type, alarm_volume, alarm_duration, light_type, light_alarm_enabled, sound_alarm_enabled, light_rbl_enabled. Switches are booleans.")],
        confirm: Annotated[str | None, Field(description="Required phrase: setAlertConfig <device id>")] = None,
    ) -> dict[str, Any]:
        """Change supported alarm actions at level 3 with confirmation. Alarms can sound or flash."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await alarm_set(ctx.registry, ctx.caller, device, settings, confirm)

    @ctx.tool(title="Camera alarm detection triggers")
    async def camera_alarm_triggers_get(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
    ) -> dict[str, Any]:
        """Read which device-reported event types trigger the camera alarm."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await choices_get(ctx.registry, ctx.caller, device)

    @ctx.tool(title="Change camera alarm trigger", tier=Tier.SECURITY)
    async def camera_alarm_triggers_set(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
        event: Annotated[str, Field(description="Exact event name from camera_alarm_triggers_get")],
        enabled: Annotated[bool, Field(description="Whether this detection triggers the alarm", strict=True)],
        confirm: Annotated[str | None, Field(description="Required phrase: setAlertEventType <device id>")] = None,
    ) -> dict[str, Any]:
        """Change one alarm trigger at level 3. Preserve the other event choices."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await choices_set(ctx.registry, ctx.caller, device, event, enabled, confirm=confirm)

    @ctx.tool(title="Camera manual alarm status")
    async def camera_alarm_status(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
    ) -> dict[str, Any]:
        """Read the manual alarm status and remaining time."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await manual_alarm(ctx.registry, ctx.caller, device)

    @ctx.tool(title="Start or stop camera alarm", tier=Tier.SECURITY)
    async def camera_alarm_manual(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
        action: Annotated[Literal["start", "stop"], Field(description="Start the audible and bright alarm now, or stop it")],
        confirm: Annotated[str | None, Field(description="Required phrase for either action: doManualAlarm <device id>")] = None,
    ) -> dict[str, Any]:
        """Start or stop the manual alarm at level 3 with confirmation for this device."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await manual_alarm(ctx.registry, ctx.caller, device, action, confirm)

    @ctx.tool(title="Camera light settings")
    async def camera_alarm_light_get(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
        light: Annotated[Literal["spotlight", "floodlight"], Field(description="Which physical camera light to read")],
    ) -> dict[str, Any]:
        """Read spotlight or floodlight brightness, duration and model limits."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await lighting_get(ctx.registry, ctx.caller, device, light)

    @ctx.tool(title="Change camera light settings", tier=Tier.SETTING)
    async def camera_alarm_light_set(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
        light: Annotated[Literal["spotlight", "floodlight"], Field(description="Which physical camera light to configure")],
        brightness: Annotated[int | None, Field(description="Brightness within the reported model range", ge=0, le=100, strict=True)] = None,
        duration: Annotated[int | None, Field(description="On duration in seconds within the reported model limit", ge=1, le=86400, strict=True)] = None,
    ) -> dict[str, Any]:
        """Change supported camera light brightness or duration at level 2."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await lighting_set(ctx.registry, ctx.caller, device, light, brightness, duration)

    @ctx.tool(title="Switch camera light now", tier=Tier.SECURITY)
    async def camera_alarm_light_switch(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
        light: Annotated[Literal["spotlight", "floodlight"], Field(description="Physical camera light to switch")],
        enabled: Annotated[bool, Field(description="Turn the light on or off now", strict=True)],
        confirm: Annotated[str | None, Field(description="Required phrase: setForceWhitelampState <device id> for spotlight, manualFloodlightOp <device id> for floodlight")] = None,
    ) -> dict[str, Any]:
        """Switch a bright physical light now at level 3 with confirmation."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await lighting_switch(ctx.registry, ctx.caller, device, light, enabled, confirm)

    @ctx.tool(title="Recent local camera events")
    async def camera_events_recent(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
        start_time: Annotated[int | None, Field(description="Optional Unix seconds for SD window start. Supply end_time too.", strict=True)] = None,
        end_time: Annotated[int | None, Field(description="Optional Unix seconds for SD window end. At most one day.", strict=True)] = None,
        limit: Annotated[int, Field(description="Maximum SD events in one page", ge=1, le=100, strict=True)] = 20,
        start_index: Annotated[int, Field(description="SD page start index. Use next_index to continue.", ge=0, le=100000, strict=True)] = 0,
    ) -> dict[str, Any]:
        """Poll the last local alert and cached python-kasa alert. Optionally read one SD metadata page. No clips or thumbnails."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await events_recent(ctx.registry, ctx.caller, device, start_time, end_time, limit, start_index)

    @ctx.tool(title="Camera notification choices")
    async def camera_detect_notifications_get(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
    ) -> dict[str, Any]:
        """Read device-side notification switches and interruption levels by event type. Push delivery is cloud."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await choices_get(ctx.registry, ctx.caller, device, notifications=True)

    @ctx.tool(title="Change camera notification choice", tier=Tier.SETTING)
    async def camera_detect_notifications_set(
        device: Annotated[str, Field(description="Camera or doorbell id or name")],
        event: Annotated[str, Field(description="Exact event name from camera_detect_notifications_get")],
        enabled: Annotated[bool, Field(description="Enable or disable the device-side notification for this event", strict=True)],
        interruption_level: Annotated[Literal["none", "general", "critical"] | None, Field(description="Optional device-reported notification urgency")] = None,
    ) -> dict[str, Any]:
        """Change one device-side notification choice at level 2. Preserve other types. No cloud push calls."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await choices_set(ctx.registry, ctx.caller, device, event, enabled, interruption_level, notifications=True)
