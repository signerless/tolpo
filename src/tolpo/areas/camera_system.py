"""Capability-gated SMARTCAM controls. Requests use the existing authenticated caller.

Wire facts: camera-settings.md, python-kasa PanTilt/LensMask/Led/Time modules,
and the app's request builders (method and field names only). No app code is copied.
"""

from dataclasses import dataclass, replace
from typing import Annotated, Any, Literal

from kasa.exceptions import AuthenticationError
from pydantic import Field

from .. import policy
from ..policy import Tier
from ..registry import BadRequest, DeviceUnavailable
from ..views import clean

KINDS = ["camera", "doorbell"]
Device = Annotated[str, Field(description="Camera or doorbell id or name")]
Confirm = Annotated[str | None, Field(description="Exact '<method> <device id>' confirmation for level 3 or 4")]


@dataclass(frozen=True)
class Read:
    method: str
    component: str
    module: str
    section: str
    fields: tuple[str, ...]
    selector: str = "name"


READS = {
    "presets": Read("getPresetConfig", "ptz", "preset", "preset", ("id", "name")),
    "angles": Read("getMotorAngleRange", "ptz", "panoramic_view", "get_angle_range", ("x_angle_range", "y_angle_range"), "direct"),
    "patrol": Read("getPatrolStatus", "patrol", "patrol", "get_patrol_status", ("value",), "direct"),
    # UNVERIFIED: getPatrolInfo response section is inferred from its request section.
    "patrol_points": Read("getPatrolInfo", "patrol", "patrol", "get_patrol_info", ("id", "name", "wait_time", "enabled", "index", "patrol_info"), "direct"),
    "tracking": Read("getTargetTrackConfig", "targetTrack", "target_track", "target_track_info", ("enabled", "people_enabled", "pet_enabled", "vehicle_enabled")),
}
WRITES = {
    "stopMove": (Tier.CONTROL, "Stop camera movement."),
    "cruiseMove": (Tier.CONTROL, "Sweep the camera once."),
    "cruiseStop": (Tier.CONTROL, "Stop a camera sweep."),
    "manualCalibrate": (Tier.CONTROL, "Calibrate the camera motor."),
    "addMotorPostion": (Tier.SETTING, "Save a camera viewpoint."),
    "deletePreset": (Tier.SETTING, "Delete camera viewpoints."),
    "setPatrolStatus": (Tier.SETTING, "Start or stop camera patrol."),
    "addPatrol": (Tier.SETTING, "Add a camera patrol point."),
    "editPatrol": (Tier.SETTING, "Edit a camera patrol point."),
    "deletePatrol": (Tier.SETTING, "Delete a camera patrol point."),
    "setTargetTrackConfig": (Tier.SETTING, "Change automatic camera tracking."),
}


def _catalog():
    reads = {r.method for r in READS.values()}
    shared = [policy.method("getChildDeviceComponentList", Tier.READ, ["hub"], "Read component support for hub-managed cameras.")]
    return shared + [policy.method(m, Tier.READ, KINDS, "Read camera settings or capabilities.") for m in sorted(reads)] + [
        policy.method(m, tier, KINDS, summary) for m, (tier, summary) in WRITES.items()
    ]


def _pick(value, fields):
    """Only named, known fields leave the house. Unknown fields are never echoed."""
    if isinstance(value, dict):
        out = {k: _pick(v, fields) for k, v in value.items() if k in fields}
        return {k: v for k, v in out.items() if v is not None and v != "" and v != [] and v != {}}
    if isinstance(value, list):
        return [v for item in value[:32] if (v := _pick(item, fields)) is not None and v != {} and v != ""]
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, (bool, int, float)):
        return value
    return None


def _section(result, module, section):
    if not isinstance(result, dict):
        raise BadRequest("The camera returned an unexpected response shape")
    block = result.get(module)
    if not isinstance(block, dict) or section not in block:
        raise BadRequest("The camera returned an unexpected section shape")
    return block[section]


def _rejected(value):
    if isinstance(value, dict):
        return ("error_code" in value and value["error_code"] not in (0, "0")) or any(_rejected(v) for v in value.values())
    if isinstance(value, list):
        return any(_rejected(v) for v in value)
    return False


async def _call(caller, entry, method, params, confirm=None):
    try:
        result = await caller.call(entry, method, params, confirm=confirm)
    except DeviceUnavailable as exc:
        cause = exc.__cause__
        reason = "did not answer in time" if isinstance(cause, TimeoutError) else "rejected the account credentials" if isinstance(cause, AuthenticationError) else "reported a device error"
        raise DeviceUnavailable(f"{entry.slug}: {method}: camera {reason}") from exc
    if _rejected(result):
        raise DeviceUnavailable(f"{entry.slug}: {method} was rejected by the camera")
    return result


def _response_section(entry, method, result, module, section):
    try:
        return _section(result, module, section)
    except BadRequest:
        raise DeviceUnavailable(f"{entry.slug}: {method} returned an unexpected response shape") from None


async def components(registry, caller, device):
    entry = registry.resolve_one(device)
    if entry.kind not in KINDS:
        raise BadRequest(f"{entry.slug} is not a camera or doorbell")
    if entry.root is not None and entry.root.kind == "hub":
        items = await child_components(caller, entry)
    else:
        items = await direct_components(caller, entry)
    if not isinstance(items, list):
        raise BadRequest(f"{entry.slug}: camera component list is unavailable")
    found = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            version = item.get("version", 0)
            if isinstance(version, int) and not isinstance(version, bool) and version > 0:
                found[item["name"]] = version
    return entry, found


async def direct_components(caller, entry):
    try:
        result = await _call(caller, entry, "getAppComponentList", {"app_component": {"name": "app_component_list"}})
    except DeviceUnavailable as exc:
        if "did not answer in time" in str(exc):
            raise DeviceUnavailable(f"{exc}. A sleeping battery camera may need BLE hardware") from exc
        raise
    return _response_section(entry, "getAppComponentList", result, "app_component", "app_component_list")


async def child_components(caller, entry):
    """The hub supplies the component list for camera-protocol children."""
    start = 0
    for _ in range(32):
        result = await _call(caller, entry.root, "getChildDeviceComponentList", {"childControl": {"start_index": start}})
        if not isinstance(result, dict) or not isinstance(result.get("child_component_list"), list):
            raise DeviceUnavailable(f"{entry.slug}: the hub returned an unexpected component response")
        rows = result["child_component_list"]
        for row in rows:
            if isinstance(row, dict) and row.get("device_id") == entry.device.device_id:
                return row.get("component_list")
        total = result.get("sum")
        if type(total) is not int or total < 0 or not rows or start + len(rows) >= total:
            break
        start += len(rows)
    raise BadRequest(f"{entry.slug}: the hub did not report this camera's components")


def require(entry, found, feature):
    if feature == "system" and "batCamSystem" in found:
        return
    if feature and feature not in found:
        raise BadRequest(f"{entry.slug} does not report support for {feature}")


async def read_setting(registry, caller, device, setting):
    if setting not in READS:
        raise BadRequest("Unknown camera setting")
    spec = READS[setting]
    entry, found = await components(registry, caller, device)
    require(entry, found, spec.component)
    check_variant(entry, found, spec)
    if (spec.component == "clips" and found["clips"] == 1) or (spec.component == "wakeUp" and found["wakeUp"] == 1):
        spec = replace(spec, section="*", selector="flat_dict")
    elif spec.component == "pir" and found["pir"] == 1:
        spec = replace(spec, module="pir_verify", section="*", selector="flat_dict")
    result = await raw_read(caller, entry, spec)
    return {"device": entry.slug, **({setting: value} if (value := _pick(result, spec.fields)) else {})}


async def write(registry, caller, entry, method, module, section, payload, changed, confirm=None):
    await _call(caller, entry, method, {module: payload if section == "*" else {section: payload}}, confirm)
    try:
        await registry.refresh(entry, force=True)
    except DeviceUnavailable:
        raise DeviceUnavailable(f"{entry.slug}: {method} was accepted but camera state could not be refreshed") from None
    return {"device": entry.slug, "changed": changed}


def integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise BadRequest(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def switch(value):
    if type(value) is not bool:
        raise BadRequest("Enabled must be true or false")
    return "on" if value else "off"


def text_input(value, label, limit=64):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or clean(value, limit) != value:
        raise BadRequest(f"{label} must be plain text of 1 to {limit} characters")
    return value


async def ptz_stop(registry, caller, device):
    """Stop must always be sent. It skips the component read, so a slow or odd component list cannot block it.
    A camera without a motor answers with an error, which is reported."""
    entry = registry.resolve_one(device)
    if entry.kind not in KINDS:
        raise BadRequest(f"{entry.slug} is not a camera or doorbell")
    return await write(registry, caller, entry, "stopMove", "motor", "stop", "null", "movement stopped")


async def ptz_action(registry, caller, device, action, **values):
    if action == "stop":
        return await ptz_stop(registry, caller, device)
    entry, found = await components(registry, caller, device)
    require(entry, found, "patrol" if action in {"patrol", "add", "edit", "delete"} else "targetTrack" if action == "tracking" else "ptz")
    if action == "cruise":
        axis = values["axis"]
        if axis not in {"horizontal", "vertical", "stop"}:
            raise BadRequest("Sweep axis must be horizontal, vertical or stop")
        method, section, payload = ("cruiseStop", "cruise_stop", "null") if axis == "stop" else ("cruiseMove", "cruise", {"coord": "x" if axis == "horizontal" else "y"})
        return await write(registry, caller, entry, method, "motor", section, payload, f"sweep {axis}")
    if action == "calibrate":
        mode = values["position"]
        if mode not in {"factory", "saved"}:
            raise BadRequest("Calibration position must be factory or saved")
        return await write(registry, caller, entry, "manualCalibrate", "motor", "manual_cali", {"cal_to_resume": "0" if mode == "factory" else "1"}, f"calibration requested ({mode})")
    if action == "save":
        name = text_input(values["name"], "Preset name")
        return await write(registry, caller, entry, "addMotorPostion", "preset", "set_preset", {"name": name, "save_ptz": "1"}, {"preset_saved": name})
    if action == "remove":
        ids = values["ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 32:
            raise BadRequest("Choose 1 to 32 preset ids")
        ids = [str(integer(i, 0, 255, "Preset id")) for i in ids]
        presets = await _call(caller, entry, "getPresetConfig", {"preset": {"name": ["preset"]}})
        known = _section(presets, "preset", "preset").get("id", [])
        if any(i not in {str(p) for p in known} for i in ids):
            raise BadRequest("A selected preset does not exist")
        return await write(registry, caller, entry, "deletePreset", "preset", "remove_preset", {"id": ids}, {"presets_deleted": ids})
    if action == "patrol":
        enabled = switch(values["enabled"])
        return await write(registry, caller, entry, "setPatrolStatus", "patrol", "set_patrol_status", {"value": enabled}, {"patrol": enabled})
    if action in {"add", "edit", "delete"}:
        point = str(integer(values["preset_id"], 0, 255, "Preset id"))
        payload = {"id": point}
        if action != "delete":
            payload.update(name=text_input(values["name"], "Point name"), wait_time=str(integer(values["wait_seconds"], 1, 3600, "Wait seconds")))
        method = {"add": "addPatrol", "edit": "editPatrol", "delete": "deletePatrol"}[action]
        return await write(registry, caller, entry, method, "patrol", f"{action}_patrol", payload, {"patrol_point": point, "action": action})
    if action == "tracking":
        enabled = switch(values["enabled"])
        return await write(registry, caller, entry, "setTargetTrackConfig", "target_track", "target_track_info", {"enabled": enabled}, {"tracking": enabled})
    raise BadRequest("Unknown PTZ action")


# Image settings and their model-reported ranges.
READS.update({
    "night_vision": Read("getNightVisionModeConfig", "nightVisionMode", "image", "switch", ("night_vision_mode",)),
    "night_capability": Read("getNightVisionCapability", "nightVisionMode", "image_capability", "supplement_lamp", ("night_vision_mode_range",)),
    "rotation": Read("getRotationStatus", "videoRotation", "image", "switch", ("flip_type", "rotate_type_by_app")),
    "rotation_capability": Read("getImageCapability", "videoRotation", "image_capability", "flip", ("flip_type", "app_rotate_support")),
    "style": Read("getImageStyleConfig", "imageStyle", "image", "common", ("style", "luma")),
    "style_capability": Read("getImageStyleCapability", "imageStyle", "image_style_capability", "image", ("image_style_range", "image_style_support", "luma_support")),
    "video_capability": Read("getVideoCapability", "video", "video_capability", "main", ("hdrs", "auto_hdr")),
    "led": Read("getLedStatus", "led", "led", "config", ("enabled", "status")),
    "privacy": Read("getLensMaskConfig", "lensMask", "lens_mask", "lens_mask_info", ("enabled",)),
    "privacy_zones": Read("getCoverConfig", "blockZone", "cover", "cover", ("enabled",)),
    "privacy_regions": Read("getCoverRegion", "blockZone", "cover", "region_info", ("x_coor", "y_coor", "width", "height", "point_num", *(f"pt{i}_{axis}" for i in range(1, 13) for axis in ("x", "y"))), "table"),
    "osd": Read("getOsd", "osd", "OSD", "*", ("date", "logo", "label_info", "label_info_1", "enabled", "x_coor", "y_coor", "text", "is_hour12", "time_type")),
    "osd_capability": Read("getOsdCapability", "osd", "osd_capability", "*", ("date_default", "logo_default", "label_info_default", "capability", "osd_week_support", "icon_support", "x_coor", "y_coor")),
})
WRITES.update({m: (Tier.SETTING, s) for m, s in {
    "setNightVisionModeConfig": "Change camera night vision mode.",
    "setRotationStatus": "Change camera image orientation.",
    "setHDR": "Change camera HDR processing.",  # UNVERIFIED module and section, see image_change
    "setImageStyleConfig": "Change camera image style.",
    "setOsd": "Change camera video overlays.",
}.items()})
# Turning the indicator light off hides that the camera records, and turning privacy zones off shows areas the
# owner blacked out. One method carries both directions, so both are level 3. setLedStatus must be level 3 in
# every area that declares it (camera_system, hub, device_admin), or the loader fails on merge.
WRITES["setLedStatus"] = (Tier.SECURITY, "Change the camera indicator light. Off hides that the camera is recording.")
WRITES["setCoverConfig"] = (Tier.SECURITY, "Enable or disable camera privacy zones. Off shows the blacked-out areas.")
# The shared method catalog has no parameter-dependent tiers. Keep the whole
# lens-mask method at SECURITY until the shared caller can distinguish ON (1).
WRITES["setLensMaskConfig"] = (Tier.SECURITY, "Change camera privacy mode.")


async def raw_read(caller, entry, spec):
    if spec.selector == "flat_dict":
        payload = {}
    elif spec.selector == "flat":
        payload = "null"
    elif spec.selector == "direct_list":
        payload = {spec.section: []}
    elif spec.section == "*":
        if spec.method == "getOsd":
            payload = {"name": ["date", "logo"], "table": ["label_info"]}
        else:
            payload = {"name": ["date_default", "logo_default", "label_info_default", "capability"], "table": ["capability"]}
    else:
        payload = {spec.section: {}} if spec.selector == "direct" else {spec.selector: [spec.section]}
    result = await _call(caller, entry, spec.method, {spec.module: payload})
    if spec.selector == "direct_list":
        if not isinstance(result, dict):
            raise DeviceUnavailable(f"{entry.slug}: {spec.method} returned an unexpected response shape")
        if result and not any(k in result for k in spec.fields):
            raise DeviceUnavailable(f"{entry.slug}: {spec.method} returned an unexpected response shape")
        return result
    if spec.section == "*":
        if not isinstance(result, dict) or not isinstance(result.get(spec.module), dict):
            raise DeviceUnavailable(f"{entry.slug}: {spec.method} returned an unexpected response shape")
        return result[spec.module]
    return _response_section(entry, spec.method, result, spec.module, spec.section)


async def capability(caller, entry, found, setting):
    spec = READS[setting]
    require(entry, found, spec.component)
    check_variant(entry, found, spec)
    result = await raw_read(caller, entry, spec)
    if not isinstance(result, dict):
        raise BadRequest(f"{entry.slug}: capability response is unavailable")
    return result


def choice(value, choices, label):
    if not isinstance(choices, list) or not choices or value not in choices:
        raise BadRequest(f"{label} is not in the camera's reported supported values")
    return value


async def image_change(registry, caller, device, setting, value, confirm=None):
    entry, found = await components(registry, caller, device)
    if "dualCam" in found:
        raise BadRequest(f"{entry.slug}: multi-lens image changes need a lens-specific request")
    if setting == "night_vision":
        cap = await capability(caller, entry, found, "night_capability")
        value = choice(value, cap.get("night_vision_mode_range"), "Night vision mode")
        return await write(registry, caller, entry, "setNightVisionModeConfig", "image", "switch", {"night_vision_mode": value}, {setting: value})
    if setting == "flip":
        cap = await capability(caller, entry, found, "rotation_capability")
        value = choice(value, cap.get("flip_type"), "Flip mode")
        return await write(registry, caller, entry, "setRotationStatus", "image", "switch", {"flip_type": value}, {setting: value})
    if setting == "style":
        cap = await capability(caller, entry, found, "style_capability")
        value = choice(value, cap.get("image_style_range"), "Image style")
        return await write(registry, caller, entry, "setImageStyleConfig", "image", "common", {"style": value}, {setting: value})
    if setting == "hdr":
        # UNVERIFIED: the audit could not resolve setHDR's module and section; `video.set_hdr` is inferred.
        flag = switch(value)
        cap = await capability(caller, entry, found, "video_capability")
        hdr = "1" if flag == "on" else "0"
        choice(hdr, cap.get("hdrs"), "HDR mode")
        return await write(registry, caller, entry, "setHDR", "video", "set_hdr", {"secname": "main", "hdr": int(hdr)}, {setting: value})
    if setting in {"led", "privacy", "privacy_zones"}:
        spec = READS[setting]
        require(entry, found, spec.component)
        flag = switch(value)
        if setting == "led" and found["led"] != 1:
            raise BadRequest(f"{entry.slug}: this LED version needs a different status payload")
        method = {"led": "setLedStatus", "privacy": "setLensMaskConfig", "privacy_zones": "setCoverConfig"}[setting]
        return await write(registry, caller, entry, method, spec.module, spec.section, {"enabled": flag}, {setting: value}, confirm)
    raise BadRequest("Unknown image setting")


async def osd_change(registry, caller, device, element, enabled, text=None):
    if element not in {"time", "name", "logo"}:
        raise BadRequest("OSD element must be time, name or logo")
    flag = switch(enabled)
    if text is not None:
        text = text_input(text, "Overlay text")
        if element != "name":
            raise BadRequest("Only the name overlay accepts text")
    entry, found = await components(registry, caller, device)
    require(entry, found, "osd")
    if "batCamOsd" in found or "dualCam" in found:
        raise BadRequest(f"{entry.slug}: this model needs a different OSD payload")
    cap = await capability(caller, entry, found, "osd_capability")
    section = {"time": "date", "name": "label_info_1", "logo": "logo"}[element]
    default = {"time": "date_default", "name": "label_info_default", "logo": "logo_default"}[element]
    if not isinstance(cap.get(default), dict) or not cap[default]:
        raise BadRequest(f"{entry.slug} does not report support for the {element} overlay")
    current = await raw_read(caller, entry, READS["osd"])
    old = current.get(section)
    if element == "name" and old is None:
        table = current.get("label_info", [])
        if isinstance(table, list):
            old = next((row[section] for row in table if isinstance(row, dict) and section in row), None)
    if not isinstance(old, dict):
        raise BadRequest(f"{entry.slug}: current overlay position is unavailable")
    payload = _pick(old, ("x_coor", "y_coor", "text", "enabled", "is_hour12", "time_type"))
    payload["enabled"] = flag
    if text is not None:
        payload["text"] = text
    return await write(registry, caller, entry, "setOsd", "OSD", section, payload, {"overlay": element, "enabled": enabled, **({"text": text} if text is not None else {})})


READS.update({
    "battery_status": Read("getBatteryStatus", "battery", "battery", "status", ("battery_percent", "battery_charging", "battery_voltage", "battery_temperature", "battery_overheated", "low_battery", "power_mode", "battery_capacity", "battery_chg_temp_status")),
    "battery_capability": Read("getBatteryCapability", "battery", "battery", "capability", ("battery_power_mode_range", "battery_operating_mode_range", "battery_power_mode_support", "battery_operating_mode_support", "charging_privacy_mode_support", "charging_use_support", "battery_statistic_support")),
    "power_mode": Read("getPowerMode", "battery", "battery", "power", ("mode",)),
    "working_mode": Read("getBatteryOperatingMode", "battery", "battery", "operating", ("mode", "follow_config")),
    "clips": Read("getClipsConfig", "clips", "clips", "config", ("clips_length", "record_buffer", "retrigger_time")),
    "wake_up": Read("getWakeUpConfig", "wakeUp", "wake_up", "config", ("wake_up_type",)),
    "pir": Read("getPirSensitivity", "pir", "pir", "config", ("sensitivity",)),
    "charging": Read("getChargingMode", "battery", "battery", "charging_mode", ("adapter_mode", "charging_privacy_mode")),
    # UNVERIFIED: the audit names the field `status`; `enabled` is kept in case a model uses it. No fixture yet.
    "charging_privacy": Read("getChargingPrivacyMode", "battery", "battery", "charging_privacy_mode", ("status", "enabled")),
    # UNVERIFIED: statistic response nesting needs a camera or doorbell fixture.
    "statistics": Read("getBatteryStatistic", "batCamStatistics", "battery", "statistic", ("day", "day_num", "time", "bat_per", "bat_usage", "day_runtime", "hour_runtime", "pir_wakeup_with_event", "pir_only_wakeup", "solar_charge_cap", "always_on_status", "bat_status")),
})
WRITES.update({
    "setPowerMode": (Tier.SETTING, "Change camera power source mode."),
    "setBatteryOperatingMode": (Tier.SETTING, "Change camera battery working mode."),
    "setClipsConfig": (Tier.SETTING, "Change camera clip duration and retrigger behavior."),
    "setWakeUpConfig": (Tier.SETTING, "Change what wakes a battery camera."),
    # The app switches charging privacy with setChargingMode (`battery.charging_mode`, field `charging_privacy_mode`),
    # not with setChargingPrivacyMode. UNVERIFIED on a device: no battery camera fixture has this response yet.
    "setChargingMode": (Tier.SECURITY, "Change camera privacy while charging. Off lets a charging camera record."),
})


def check_variant(entry, found, spec):
    if spec.module == "battery" and found.get("battery", 0) < 3:
        raise BadRequest(f"{entry.slug}: this battery dialect requires battery component version 3 or later")


async def battery_change(registry, caller, device, setting, value, confirm=None):
    if setting not in {"power_mode", "working_mode", "wake_up", "charging_privacy"}:
        raise BadRequest("Unknown battery setting")
    entry, found = await components(registry, caller, device)
    spec = READS[setting]
    require(entry, found, spec.component)
    check_variant(entry, found, spec)
    if setting in {"power_mode", "working_mode", "charging_privacy"}:
        cap = await capability(caller, entry, found, "battery_capability")
    if setting in {"power_mode", "working_mode"}:
        field = "battery_power_mode_range" if setting == "power_mode" else "battery_operating_mode_range"
        value = choice(value, cap.get(field), "Battery mode")
        payload = {"mode": value}
        if setting == "working_mode":
            payload["follow_config"] = True
        method = "setPowerMode" if setting == "power_mode" else "setBatteryOperatingMode"
    elif setting == "wake_up":
        value = choice(value, ["detection", "doorbell"], "Wake-up type")
        payload, method = {"wake_up_type": value}, "setWakeUpConfig"
    else:
        if cap.get("charging_privacy_mode_support") not in ("1", "on", True):
            raise BadRequest(f"{entry.slug} does not report charging privacy support")
        payload, method = {"charging_privacy_mode": switch(value)}, "setChargingMode"
        spec = replace(spec, section="charging_mode")
    return await write(registry, caller, entry, method, spec.module, spec.section, payload, {setting: value}, confirm)


async def clips_change(registry, caller, device, length, buffer, retrigger):
    payload = {"clips_length": integer(length, 1, 3600, "Clip seconds"), "record_buffer": integer(buffer, 0, 3600, "Buffer seconds"), "retrigger_time": integer(retrigger, 0, 3600, "Retrigger seconds")}
    entry, found = await components(registry, caller, device)
    require(entry, found, "clips")
    check_variant(entry, found, READS["clips"])
    return await write(registry, caller, entry, "setClipsConfig", "clips", "config" if found["clips"] >= 2 else "*", payload, payload)


async def battery_statistics(registry, caller, device, days):
    integer(days, 1, 31, "Days")
    entry, found = await components(registry, caller, device)
    require(entry, found, "batCamStatistics")
    check_variant(entry, found, READS["statistics"])
    result = await _call(caller, entry, "getBatteryStatistic", {"battery": {"statistic": {"days": days}}})
    data = _pick(_response_section(entry, "getBatteryStatistic", result, "battery", "statistic"), READS["statistics"].fields)
    return {"device": entry.slug, **({"statistics": data} if data else {})}


READS.update({
    "microphone": Read("getAudioConfig", "audio", "audio_config", "microphone", ("volume", "encode_type", "sampling_rate", "bitrate", "channels", "mute", "noise_cancelling", "echo_cancelling", "factory_noise_cancelling")),
    "speaker": Read("getAudioConfig", "audio", "audio_config", "speaker", ("volume",)),
    "microphone_capability": Read("getAudioConfig", "audioCapability", "audio_capability", "device_microphone", ("volume", "mute")),
    "microphone_noise": Read("getAudioConfig", "audio", "audio_config", "microphone_algo", ("aec", "ns", "hs", "sys_aec")),
    "speaker_noise": Read("getAudioConfig", "audio", "audio_config", "speaker_algo", ("ns", "ns_mode", "hs")),
    "record_audio": Read("getAudioConfig", "audio", "audio_config", "record_audio", ("enabled",)),
    "voice_enhancement": Read("getVoiceEnhancementConfig", "voiceEnhancement", "audio_config", "voice_enhancement", ("enabled",)),
})
WRITES.update({
    "setMicrophoneVolume": (Tier.SETTING, "Change camera microphone volume or mute."),
    "setSpeakerVolume": (Tier.SETTING, "Change camera speaker volume."),
    "setRecordAudio": (Tier.SETTING, "Change whether camera recordings include audio."),
    "setVoiceEnhancement": (Tier.SETTING, "Change camera voice enhancement."),
})


async def audio_change(registry, caller, device, setting, value):
    if setting not in {"microphone", "speaker", "record_audio", "voice_enhancement"}:
        raise BadRequest("Unknown audio setting")
    entry, found = await components(registry, caller, device)
    spec = READS[setting]
    require(entry, found, spec.component)
    if setting in {"microphone", "speaker"}:
        integer(value, 0, 100, "Volume")
        current = await raw_read(caller, entry, spec)
        if not isinstance(current, dict) or "volume" not in current:
            raise BadRequest(f"{entry.slug} does not report {setting} volume support")
        # Keep the current microphone codec, channels, bitrate and sample rate.
        payload = _pick(current, ("volume", "encode_type", "sampling_rate", "bitrate", "channels")) if setting == "microphone" else {}
        payload["volume"] = str(value)
        method = "setMicrophoneVolume" if setting == "microphone" else "setSpeakerVolume"
    else:
        payload = {"enabled": switch(value)}
        method = "setRecordAudio" if setting == "record_audio" else "setVoiceEnhancement"
    return await write(registry, caller, entry, method, spec.module, spec.section, payload, {setting: value})


READS.update({
    "timezone": Read("getTimezone", "timezone", "system", "basic", ("timezone", "zone_id", "timing_mode")),
    "clock": Read("getClockStatus", "timezone", "system", "clock_status", ("local_time", "seconds_from_1970")),
    "wifi": Read("getConnectionType", "", "network", "get_connection_type", ("rssi", "rssiValue", "link_type", "signal_intensity", "channel"), "direct_list"),
    "upnp": Read("getUpnpInfo", "upnpc", "upnpc", "upnpc_info", ("enabled", "mode")),
    "compatibility": Read("getThirdPartyCompatibility", "localCtrl", "local_ctrl", "third_party_setting", ("enabled",)),
    "account": Read("getThirdAccount", "account", "user_management", "third_account", ("enabled",)),
    "reboot_schedule": Read("getReboot", "reboot", "timing_reboot", "reboot", ("enabled", "day", "time", "random_range")),
    "firmware": Read("getCloudConfig", "firmware", "cloud_config", "upgrade_info", ("fw_download_progress", "fw_download_status", "fw_verify_status", "connect_status", "reconnect_time")),
    "firmware_status": Read("getFirmwareUpdateStatus", "firmware", "cloud_config", "upgrade_status", ("status", "lastUpgradingSuccess")),
    # UNVERIFIED: flat firmware capability response nesting needs a fixture.
    "firmware_capability": Read("getFirmwareCapability", "firmware", "auto_upgrade", "*", ("auto_upgrade_support", "upgrade_process_support"), "flat"),
    "auto_update": Read("getFirmwareAutoUpgradeConfig", "firmware", "auto_upgrade", "common", ("enabled", "time", "random_range")),
})
WRITES.update({
    "setTimezone": (Tier.SETTING, "Change camera time zone and use NTP."),
    "setUpnpInfo": (Tier.SECURITY, "Change camera router port mapping. On can make the camera reachable from the internet."),
    # Off may stop local clients, tolpo included, from reaching the camera, and only the Tapo app can turn it back on.
    # On widens local exposure. Losing the agent's own access is lifecycle class, like a Wi-Fi change.
    "setThirdPartyCompatibility": (Tier.LIFECYCLE, "Change the camera third-party compatibility flag. Off may lock tolpo out of the camera."),
    "setAccountEnabled": (Tier.SECURITY, "Enable or disable an existing camera stream login."),
    "rebootDevice": (Tier.SETTING, "Restart the camera."),
    "setReboot": (Tier.SETTING, "Change the camera reboot schedule."),
    "reset": (Tier.LIFECYCLE, "Factory reset the camera and lose its settings."),
    "softReset": (Tier.LIFECYCLE, "Factory reset a newer or battery camera."),
    "configRestore": (Tier.LIFECYCLE, "Restore camera settings to their defaults."),
    # Not a read: the camera contacts the vendor cloud and updates its own upgrade state.
    "checkFirmwareVersionByCloud": (Tier.CONTROL, "Ask the camera to check the vendor firmware catalogue."),
    "startFirmwareUpgrade": (Tier.LIFECYCLE, "Install camera firmware from the vendor cloud."),
    "setFirmwareAutoUpgradeConfig": (Tier.LIFECYCLE, "Change unattended camera firmware updates."),
})


async def system_change(registry, caller, device, setting, value=None, confirm=None):
    entry, found = await components(registry, caller, device)
    if setting == "timezone":
        from datetime import datetime
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        require(entry, found, "timezone")
        text_input(value, "IANA time zone")
        try:
            zone = ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise BadRequest("Use a valid IANA time zone") from None
        offset = datetime.now(zone).utcoffset()
        total = int(offset.total_seconds() // 60)
        timezone = f"UTC{'+' if total >= 0 else '-'}{abs(total) // 60:02d}:{abs(total) % 60:02d}"
        payload = {"timezone": timezone, "zone_id": value, "timing_mode": "ntp"}
        return await write(registry, caller, entry, "setTimezone", "system", "basic", payload, {"zone_id": value, "timing_mode": "ntp"})
    if setting in {"upnp", "compatibility", "account"}:
        spec = READS[setting]
        require(entry, found, spec.component)
        flag = switch(value)
        if setting == "upnp":
            current = await raw_read(caller, entry, spec)
            if not isinstance(current, dict) or not isinstance(current.get("mode"), str):
                raise BadRequest(f"{entry.slug}: current UPnP mapping mode is unavailable")
            mode = text_input(current["mode"], "UPnP mapping mode")
            method, section, payload = "setUpnpInfo", "upnpc_info", {"enabled": flag, "mode": mode}
        elif setting == "compatibility":
            method, section, payload = "setThirdPartyCompatibility", "third_party_setting", {"enabled": flag}
        else:
            # This only toggles an existing account. Creation needs excluded encryption.
            current = await raw_read(caller, entry, spec)
            if not isinstance(current, dict) or "enabled" not in current or (value and not isinstance(current.get("username"), str)) or (value and not current["username"].strip()):
                raise BadRequest(f"{entry.slug}: no existing stream login was reported")
            method, section, payload = "setAccountEnabled", "set_account_enabled", {"secname": "third_account", "enabled": flag}
        return await write(registry, caller, entry, method, spec.module, section, payload, {setting: value}, confirm)
    if setting in {"reboot", "factory_reset", "restore"}:
        require(entry, found, "system")
        if setting == "reboot":
            method, section, payload = "rebootDevice", "reboot", {}
        elif setting == "restore":
            method, section, payload = "configRestore", "config_restore", {}
        elif found.get("system", 0) >= 4 or "battery" in found:
            method, section, payload = "softReset", "soft_reset", {"type": "factory"}
        else:
            method, section, payload = "reset", "reset", {}
        return await write(registry, caller, entry, method, "system", section, payload, f"{setting} requested", confirm)
    if setting in {"firmware_check", "firmware_update"}:
        require(entry, found, "firmware")
        method, section = ("checkFirmwareVersionByCloud", "check_fw_version") if setting == "firmware_check" else ("startFirmwareUpgrade", "fw_download")
        return await write(registry, caller, entry, method, "cloud_config", section, "null", f"{setting} requested", confirm)
    if setting == "auto_update":
        cap = await capability(caller, entry, found, "firmware_capability")
        if cap.get("auto_upgrade_support") not in ("1", "on", True):
            raise BadRequest(f"{entry.slug} does not report automatic firmware update support")
        current = await raw_read(caller, entry, READS["auto_update"])
        if not isinstance(current, dict) or "time" not in current or "random_range" not in current:
            raise BadRequest(f"{entry.slug}: current firmware update window is unavailable")
        payload = _pick(current, READS["auto_update"].fields)
        payload["enabled"] = switch(value)
        return await write(registry, caller, entry, "setFirmwareAutoUpgradeConfig", "auto_upgrade", "common", payload, {"auto_update": value}, confirm)
    raise BadRequest("Unknown system setting")


async def reboot_schedule(registry, caller, device, enabled, time=None):
    import re

    flag = switch(enabled)
    if time is not None and (not isinstance(time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time)):
        raise BadRequest("Reboot time must be HH:MM in the camera's local time zone")
    entry, found = await components(registry, caller, device)
    require(entry, found, "reboot")
    current = await raw_read(caller, entry, READS["reboot_schedule"])
    if not isinstance(current, dict) or "day" not in current or "time" not in current:
        raise BadRequest(f"{entry.slug}: current reboot schedule is unavailable")
    payload = _pick(current, READS["reboot_schedule"].fields)
    payload["enabled"] = flag
    if time is not None:
        payload["time"] = f"{time}:00"
    return await write(registry, caller, entry, "setReboot", "timing_reboot", "reboot", payload, {"reboot_schedule": payload})


def tools(ctx):
    async def run(fn, device, *args, **kwargs):
        await ctx.ready_for([device])
        with ctx.translated():
            return await fn(ctx.registry, ctx.caller, device, *args, **kwargs)

    @ctx.tool(title="Camera PTZ settings")
    async def camera_ptz_status(device: Device, setting: Annotated[Literal["presets", "angles", "patrol", "patrol_points", "tracking"], Field(description="PTZ setting to read")] = "presets") -> dict[str, Any]:
        """Read supported camera viewpoints, angle ranges, patrol or tracking."""
        return await run(read_setting, device, setting)

    @ctx.tool(title="Stop camera movement", tier=Tier.CONTROL)
    async def camera_ptz_stop(device: Device) -> dict[str, Any]:
        """Stop pan and tilt movement now. Sent without any capability read first, so it works even when other reads fail.
        A sweep stops with camera_ptz_cruise axis=stop and a patrol with camera_ptz_patrol enabled=false."""
        return await run(ptz_stop, device)

    @ctx.tool(title="Camera sweep", tier=Tier.CONTROL)
    async def camera_ptz_cruise(device: Device, axis: Annotated[Literal["horizontal", "vertical", "stop"], Field(description="One sweep axis, or stop")]) -> dict[str, Any]:
        """Sweep the horizontal or vertical range once, or stop the sweep."""
        return await run(ptz_action, device, "cruise", axis=axis)

    @ctx.tool(title="Calibrate camera position", tier=Tier.CONTROL)
    async def camera_ptz_calibrate(device: Device, position: Annotated[Literal["factory", "saved"], Field(description="Position to resume after calibration")] = "factory") -> dict[str, Any]:
        """Move the camera to its limits to calibrate its motor."""
        return await run(ptz_action, device, "calibrate", position=position)

    @ctx.tool(title="Save camera viewpoint", tier=Tier.SETTING)
    async def camera_ptz_save_preset(device: Device, name: Annotated[str, Field(description="Plain name of the current viewpoint", min_length=1, max_length=64)]) -> dict[str, Any]:
        """Save the current pan and tilt position under a name."""
        return await run(ptz_action, device, "save", name=name)

    @ctx.tool(title="Delete camera viewpoints", tier=Tier.SETTING)
    async def camera_ptz_delete_presets(device: Device, ids: Annotated[list[int], Field(description="Existing viewpoint ids", min_length=1, max_length=32)]) -> dict[str, Any]:
        """Delete selected saved viewpoints after checking they exist."""
        return await run(ptz_action, device, "remove", ids=ids)

    @ctx.tool(title="Camera patrol", tier=Tier.SETTING)
    async def camera_ptz_patrol(device: Device, enabled: Annotated[bool, Field(description="Start or stop the configured patrol")]) -> dict[str, Any]:
        """Start or stop an existing camera patrol. Patrol uses battery power."""
        return await run(ptz_action, device, "patrol", enabled=enabled)

    @ctx.tool(title="Camera patrol point", tier=Tier.SETTING)
    async def camera_ptz_patrol_point(device: Device, action: Annotated[Literal["add", "edit", "delete"], Field(description="Change to a patrol point")], preset_id: Annotated[int, Field(description="Viewpoint id", ge=0, le=255)], name: Annotated[str, Field(description="Point name for add or edit", max_length=64)] = "Viewpoint", wait_seconds: Annotated[int, Field(description="Wait at this point for add or edit", ge=1, le=3600)] = 30) -> dict[str, Any]:
        """Add, edit or delete one point in a camera's patrol."""
        return await run(ptz_action, device, action, preset_id=preset_id, name=name, wait_seconds=wait_seconds)

    @ctx.tool(title="Camera automatic tracking", tier=Tier.SETTING)
    async def camera_ptz_auto_tracking(device: Device, enabled: Annotated[bool, Field(description="Whether the camera follows targets automatically")]) -> dict[str, Any]:
        """Enable or disable basic target tracking on a supported camera."""
        return await run(ptz_action, device, "tracking", enabled=enabled)

    @ctx.tool(title="Camera image settings")
    async def camera_image_read(device: Device, setting: Annotated[Literal["night_vision", "night_capability", "rotation", "rotation_capability", "style", "style_capability", "video_capability", "led", "privacy_zones", "privacy_regions", "osd", "osd_capability"], Field(description="Image setting or supported ranges to read")]) -> dict[str, Any]:
        """Read supported image settings and ranges. Private device fields are omitted."""
        return await run(read_setting, device, setting)

    @ctx.tool(title="Camera image mode", tier=Tier.SETTING)
    async def camera_image_mode(device: Device, setting: Annotated[Literal["night_vision", "flip", "style"], Field(description="Image mode to change")], value: Annotated[str, Field(description="Exact mode from camera_image_read capability values", max_length=64)]) -> dict[str, Any]:
        """Set night vision, flip or mirror, or image style within the reported range."""
        return await run(image_change, device, setting, value)

    @ctx.tool(title="Camera HDR", tier=Tier.SETTING)
    async def camera_image_hdr(device: Device, enabled: Annotated[bool, Field(description="Enable HDR for the main video stream")]) -> dict[str, Any]:
        """Set main-stream HDR after checking the camera's supported HDR values."""
        return await run(image_change, device, "hdr", enabled)

    @ctx.tool(title="Camera video overlay", tier=Tier.SETTING)
    async def camera_image_osd(device: Device, element: Annotated[Literal["time", "name", "logo"], Field(description="Overlay to change")], enabled: Annotated[bool, Field(description="Show this overlay")], text: Annotated[str | None, Field(description="Plain name overlay text, or keep existing text", max_length=64)] = None) -> dict[str, Any]:
        """Show or hide an overlay while preserving its current position and time format."""
        return await run(osd_change, device, element, enabled, text)

    @ctx.tool(title="Camera privacy zones", tier=Tier.SECURITY)
    async def camera_image_privacy_zones(device: Device, enabled: Annotated[bool, Field(description="Apply the existing blacked-out regions. False shows those areas in the video")], confirm: Confirm = None) -> dict[str, Any]:
        """Enable or disable existing privacy zones. Level 3 with confirmation in both directions, because off shows
        areas the owner blacked out. Read their geometry with camera_image_read."""
        return await run(image_change, device, "privacy_zones", enabled, confirm)

    @ctx.tool(title="Camera indicator light", tier=Tier.SECURITY)
    async def camera_image_led(device: Device, enabled: Annotated[bool, Field(description="Turn the status LED on or off. Off hides that the camera is recording")], confirm: Confirm = None) -> dict[str, Any]:
        """Change the status indicator on cameras with the supported LED dialect. Level 3 with confirmation in both
        directions, because a dark LED hides that the camera records."""
        return await run(image_change, device, "led", enabled, confirm)

    @ctx.tool(title="Camera privacy status")
    async def camera_system_privacy_status(device: Device) -> dict[str, Any]:
        """Read whether the camera lens mask is enabled."""
        return await run(read_setting, device, "privacy")

    @ctx.tool(title="Camera privacy mode", tier=Tier.SECURITY)
    async def camera_system_privacy(device: Device, enabled: Annotated[bool, Field(description="True closes the lens mask. False allows streaming and recording")], confirm: Confirm = None) -> dict[str, Any]:
        """Change the lens mask with confirmation. Both directions currently need level 3."""
        return await run(image_change, device, "privacy", enabled, confirm)


    @ctx.tool(title="Camera battery settings")
    async def camera_battery_read(device: Device, setting: Annotated[Literal["battery_status", "battery_capability", "power_mode", "working_mode", "clips", "wake_up", "pir", "charging", "charging_privacy"], Field(description="Battery setting to read")]) -> dict[str, Any]:
        """Read battery settings from an awake, reachable camera. Sleeping cameras may need BLE."""
        return await run(read_setting, device, setting)

    @ctx.tool(title="Camera battery mode", tier=Tier.SETTING)
    async def camera_battery_mode(device: Device, setting: Annotated[Literal["power_mode", "working_mode"], Field(description="Power source or working mode")], value: Annotated[str, Field(description="Exact mode from the reported battery capability range", max_length=64)]) -> dict[str, Any]:
        """Change a supported battery mode. Custom working parameters are kept by the camera."""
        return await run(battery_change, device, setting, value)

    @ctx.tool(title="Camera clip duration", tier=Tier.SETTING)
    async def camera_battery_clips(device: Device, length_seconds: Annotated[int, Field(description="Maximum clip length. The camera may enforce a narrower range", ge=1, le=3600)], buffer_seconds: Annotated[int, Field(description="Recording buffer in seconds", ge=0, le=3600)], retrigger_seconds: Annotated[int, Field(description="Time before another clip can start", ge=0, le=3600)]) -> dict[str, Any]:
        """Change clip length, recording buffer and retrigger time on supported battery cameras."""
        return await run(clips_change, device, length_seconds, buffer_seconds, retrigger_seconds)

    @ctx.tool(title="Camera wake-up behavior", tier=Tier.SETTING)
    async def camera_battery_wake_up(device: Device, wake_type: Annotated[Literal["detection", "doorbell"], Field(description="Event type that wakes the camera")]) -> dict[str, Any]:
        """Choose a wake-up trigger. This does not wake a sleeping camera. BLE may be needed."""
        return await run(battery_change, device, "wake_up", wake_type)

    @ctx.tool(title="Camera charging privacy", tier=Tier.SECURITY)
    async def camera_battery_charging_privacy(device: Device, enabled: Annotated[bool, Field(description="Keep the lens masked while charging")], confirm: Confirm = None) -> dict[str, Any]:
        """Change supported charging privacy behavior with level 3 confirmation."""
        return await run(battery_change, device, "charging_privacy", enabled, confirm)

    @ctx.tool(title="Camera battery statistics")
    async def camera_battery_statistics(device: Device, days: Annotated[int, Field(description="Number of recent days to read", ge=1, le=31)] = 7) -> dict[str, Any]:
        """Read compact daily usage, wake-up counts and solar charging statistics."""
        return await run(battery_statistics, device, days)


    @ctx.tool(title="Camera audio settings")
    async def camera_audio_read(device: Device, setting: Annotated[Literal["microphone", "speaker", "microphone_capability", "microphone_noise", "speaker_noise", "record_audio", "voice_enhancement"], Field(description="Audio setting or noise suppression state to read")]) -> dict[str, Any]:
        """Read microphone, speaker, recording audio and noise processing settings."""
        return await run(read_setting, device, setting)

    @ctx.tool(title="Camera audio volume", tier=Tier.SETTING)
    async def camera_audio_volume(device: Device, target: Annotated[Literal["microphone", "speaker"], Field(description="Volume to change")], volume: Annotated[int, Field(description="Volume from 0 (mute) to 100", ge=0, le=100)]) -> dict[str, Any]:
        """Set microphone or speaker volume. This does not play any sound."""
        return await run(audio_change, device, target, volume)

    @ctx.tool(title="Camera recording audio", tier=Tier.SETTING)
    async def camera_audio_record(device: Device, enabled: Annotated[bool, Field(description="Include microphone audio with camera recordings")]) -> dict[str, Any]:
        """Enable or disable microphone audio in recordings."""
        return await run(audio_change, device, "record_audio", enabled)

    @ctx.tool(title="Camera voice enhancement", tier=Tier.SETTING)
    async def camera_audio_voice_enhancement(device: Device, enabled: Annotated[bool, Field(description="Enable supported voice enhancement")]) -> dict[str, Any]:
        """Change the supported voice enhancement option."""
        return await run(audio_change, device, "voice_enhancement", enabled)


    @ctx.tool(title="Camera system settings")
    async def camera_system_read(device: Device, setting: Annotated[Literal["timezone", "clock", "wifi", "upnp", "compatibility", "account", "reboot_schedule", "firmware", "firmware_status", "firmware_capability", "auto_update"], Field(description="Safe system setting to read")]) -> dict[str, Any]:
        """Read time, signal, network exposure or firmware state. Login and network identities are hidden."""
        return await run(read_setting, device, setting)

    @ctx.tool(title="Camera time zone", tier=Tier.SETTING)
    async def camera_system_timezone(device: Device, zone_id: Annotated[str, Field(description="IANA time zone, for example Europe/Berlin", max_length=64)]) -> dict[str, Any]:
        """Set the camera time zone and NTP mode. The camera synchronizes its own clock."""
        return await run(system_change, device, "timezone", zone_id)

    @ctx.tool(title="Camera UPnP", tier=Tier.SECURITY)
    async def camera_system_upnp(device: Device, enabled: Annotated[bool, Field(description="Permit router port mapping for this camera")], confirm: Confirm = None) -> dict[str, Any]:
        """Enable or disable UPnP port mapping with level 3 confirmation. On lets the camera ask the router to open a
        port, which can make the camera reachable from the internet."""
        return await run(system_change, device, "upnp", enabled, confirm)

    @ctx.tool(title="Camera third-party compatibility", tier=Tier.LIFECYCLE)
    async def camera_system_compatibility(device: Device, enabled: Annotated[bool, Field(description="Allow local third-party integrations on this camera")], confirm: Confirm = None) -> dict[str, Any]:
        """Change the camera's third-party compatibility flag with level 4 confirmation. Warning: turning it off may
        stop tolpo itself from reaching this camera, and only the Tapo app can turn it back on. Turning it on lets
        other local clients use the camera. The cloud account flag is separate and is not changed."""
        return await run(system_change, device, "compatibility", enabled, confirm)

    @ctx.tool(title="Existing camera stream login", tier=Tier.SECURITY)
    async def camera_system_account_enabled(device: Device, enabled: Annotated[bool, Field(description="Enable an existing RTSP and ONVIF camera login")], confirm: Confirm = None) -> dict[str, Any]:
        """Enable or disable an existing stream account with confirmation. Credentials are never returned."""
        return await run(system_change, device, "account", enabled, confirm)

    @ctx.tool(title="Restart camera", tier=Tier.SETTING)
    async def camera_system_reboot(device: Device) -> dict[str, Any]:
        """Restart a supported camera. State refresh may report the expected temporary outage."""
        return await run(system_change, device, "reboot")

    @ctx.tool(title="Camera scheduled reboot", tier=Tier.SETTING)
    async def camera_system_reboot_schedule(device: Device, enabled: Annotated[bool, Field(description="Enable scheduled restarts")], time: Annotated[str | None, Field(description="Optional HH:MM in camera local time. Keep the current days and random spread")] = None) -> dict[str, Any]:
        """Change scheduled reboot status or time while preserving the current days and random spread."""
        return await run(reboot_schedule, device, enabled, time)

    @ctx.tool(title="Check camera firmware", tier=Tier.CONTROL)
    async def camera_system_firmware_check(device: Device) -> dict[str, Any]:
        """Ask the camera to check the vendor firmware catalogue. Installs nothing. Read the result with camera_system_read."""
        return await run(system_change, device, "firmware_check")

    @ctx.tool(title="Update camera firmware", tier=Tier.LIFECYCLE)
    async def camera_system_firmware_update(device: Device, confirm: Confirm = None) -> dict[str, Any]:
        """Start firmware installation with level 4 confirmation. The camera downloads from the vendor."""
        return await run(system_change, device, "firmware_update", confirm=confirm)

    @ctx.tool(title="Camera automatic firmware updates", tier=Tier.LIFECYCLE)
    async def camera_system_auto_update(device: Device, enabled: Annotated[bool, Field(description="Allow unattended firmware installation in the current update window")], confirm: Confirm = None) -> dict[str, Any]:
        """Change automatic firmware updates with level 4 confirmation and keep the current window."""
        return await run(system_change, device, "auto_update", enabled, confirm)

    @ctx.tool(title="Reset or restore camera", tier=Tier.LIFECYCLE)
    async def camera_system_reset(device: Device, action: Annotated[Literal["factory_reset", "restore"], Field(description="Factory reset loses settings and may lose connectivity. Restore resets setting groups")], confirm: Confirm = None) -> dict[str, Any]:
        """Factory reset or restore defaults with level 4 confirmation. Newer cameras use softReset."""
        return await run(system_change, device, action, confirm=confirm)


METHODS = _catalog()
