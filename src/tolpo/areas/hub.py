"""Local hub events and child settings. All requests use the policy caller."""

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import math
from typing import Annotated, Any, Literal

from kasa import Module
from pydantic import Field

from .. import policy
from ..calls import DeviceCaller
from ..policy import Tier
from ..registry import Registry, RegistryError
from ..views import clean

METHODS = [
    # Same level and kinds as the automation area, which polls the same log.
    policy.method("get_trigger_logs", Tier.READ, ["sensor", "switch"], "Read recent child trigger events."),
    *[policy.method(name, Tier.READ, ["hub"], summary) for name, summary in (
        ("get_alarm_configure", "Read the hub alarm settings."),
        ("get_support_alarm_type_list", "Read the hub alarm and chime tones."),
        ("getSirenConfig", "Read the hub siren settings."),
        ("getSirenTypeList", "Read the hub siren tones."),
        ("getSirenStatus", "Read whether the hub siren is sounding."),
    )],
    policy.method("set_alarm_configure", Tier.SETTING, ["hub"], "Change the hub alarm tone, volume or duration."),
    policy.method("setSirenConfig", Tier.SETTING, ["hub"], "Change the hub siren tone, volume or duration."),
    policy.method("play_alarm", Tier.SECURITY, ["hub"], "Sound the hub alarm or chime tone."),
    policy.method("stop_alarm", Tier.SECURITY, ["hub"], "Stop the hub alarm."),
    policy.method("setSirenStatus", Tier.SECURITY, ["hub"], "Start or stop the hub siren."),
]

DeviceRef = Annotated[str, Field(description="Device id or name")]
Confirmation = Annotated[str | None, Field(description="Exact method and device id confirmation phrase")]
Unit = Literal["celsius", "fahrenheit"]
Volume = Literal["mute", "low", "normal", "high"]
SensorSetting = Literal["sensitivity", "alarm", "comfort_temperature", "comfort_humidity", "report_mode", "led"]
SENSOR_SETTINGS = {
    "sensitivity": ("get_sensitivity_info", "set_sensitivity_info", {"T100", "T150"}, ("sensitivity", "report_interval")),
    "alarm": ("get_alarm_config", "set_alarm_config", {"T300"}, ("type", "volume", "duration")),
    "comfort_temperature": ("get_comfort_temp_config", "set_comfort_temp_config", {"T310", "T315"},
                            ("min_temp", "max_temp", "temp_unit", "notification")),
    "comfort_humidity": ("get_comfort_humidity_config", "set_comfort_humidity_config", {"T310", "T315"},
                         ("min_humidity", "max_humidity", "notification")),
    "report_mode": ("get_report_mode", "set_report_mode", {"T310", "T315"}, ("report_mode",)),
    "led": ("get_led_control", "set_led_control", None, ("led_onoff",)),
}
METHODS += [policy.method(spec[0], Tier.READ, ["sensor"], f"Read the sensor {name.replace('_', ' ')} setting.")
            for name, spec in SENSOR_SETTINGS.items()]
METHODS += [policy.method(spec[1], Tier.SETTING, ["sensor"], f"Change the sensor {name.replace('_', ' ')} setting.")
            for name, spec in SENSOR_SETTINGS.items() if name != "alarm"]
# The leak siren is a protection: muting it or shortening it weakens leak warnings, so its settings are level 3.
METHODS += [policy.method("set_alarm_config", Tier.SECURITY, ["sensor"],
                          "Change the leak sensor siren volume, tone or duration, which can mute leak warnings.")]
METHODS += [policy.method("play_alarm", Tier.SECURITY, ["sensor"], "Test the leak sensor's audible siren.")]
ValveSetting = Literal["boost", "hold", "window", "calibration", "descaling", "descaling_auto",
                       "shutdown", "work_mode", "night_mode", "early_start", "frost", "schedule"]
ValveMode = Literal["silent_mode", "standard_mode", "turbo_mode"]
ScheduleMode = Literal["everyday", "weekday_sat_sun"]
VALVE_KINDS = {"valve", "thermostat"}
EVENT_KINDS = {"sensor", "switch"}  # children whose trigger log tolpo reads (contact, motion, leak, button, dial)
VALVE_SETTINGS = {
    "boost": ("get_boost_info", ("enable", "boost_time", "boost_remain_time")),
    "hold": ("get_holdon_info", ("enable", "hold_on_time", "hold_on_remain_time")),
    "window": ("get_window_open_detection", ("enable", "detected_action")),
    "calibration": ("get_progress_calibration_info", ("status",)),
    "descaling": ("get_remove_scale_status", ("status",)),
    "descaling_auto": ("get_remove_scale_info", ("auto",)),
    "shutdown": ("get_shutdown_info", ("enable", "mode", "anti_clogging_enable", "anti_clogging_day", "anti_clogging_min")),
    "work_mode": ("get_trv_mode", ("trv_mode",)),
    "night_mode": ("get_night_mode_info", ("enable",)),
    "early_start": ("get_early_start", ("enable",)),
    "frost": ("get_frost_protection", ("min_temp", "temp_unit")),
    "schedule": ("get_sc_schedule_info", ("enable", "mode")),
}
METHODS += [policy.method(spec[0], Tier.READ, VALVE_KINDS, f"Read the valve {name.replace('_', ' ')} setting.")
            for name, spec in VALVE_SETTINGS.items()]
METHODS += [policy.method(name, Tier.SETTING, VALVE_KINDS, summary) for name, summary in (
    ("set_boost_info", "Start or stop timed valve boost."),
    ("set_holdon_info", "Start or stop timed valve hold."),
    ("set_default_boost_info_v2", "Change the default boost temperature and duration."),
    ("set_default_holdon_info", "Change the default hold duration."),
    ("set_window_open_detection", "Change valve open-window detection."),
    ("progress_calibration", "Start mechanical valve calibration."),
    ("remove_scale", "Start mechanical valve descaling."),
    ("set_remove_scale_info", "Change automatic valve descaling."),
    ("set_trv_mode", "Change valve motor behavior."),
    ("set_night_mode_info", "Change valve night mode."),
    ("set_early_start", "Change valve early heating for schedules."),
    ("set_sc_schedule_info", "Enable or choose the valve schedule mode."),
)]
METHODS += [
    policy.method("set_frost_protection", Tier.SECURITY, VALVE_KINDS, "Change the valve frost-protection minimum."),
    policy.method("set_shutdown_info", Tier.SECURITY, VALVE_KINDS, "Change valve shutdown, which can leave the valve fully closed."),
    policy.method("get_sc_schedule_rules", Tier.READ, VALVE_KINDS, "Read local valve schedule rules."),
    policy.method("get_temperature_records", Tier.READ, VALVE_KINDS, "Read locally stored valve temperature history."),
    policy.method("get_temp_humidity_records", Tier.READ, ["sensor"], "Read locally stored 24-hour sensor history."),
    policy.method("get_led_info", Tier.READ, ["hub"], "Read the hub LED settings."),
    policy.method("getLedStatus", Tier.READ, ["hub"], "Read the hub LED settings."),
    policy.method("set_led_info", Tier.SETTING, ["hub"], "Change the hub status LED behavior."),
    policy.method("setLedStatus", Tier.SETTING, ["hub"], "Change the hub status LED behavior."),
    policy.method("get_device_load_info", Tier.READ, ["hub"], "Read the hub load and memory counters."),
    policy.method("getDeviceLoadInfo", Tier.READ, ["hub"], "Read the hub load and memory counters."),
]


def _entry(registry, ref, kinds):
    entry = registry.resolve_one(ref)
    if entry.kind not in kinds:
        raise RegistryError(f"{clean(entry.slug)} does not support this action")
    return entry


def _integer(value, minimum, maximum, name):
    if type(value) is not int or not minimum <= value <= maximum:
        raise RegistryError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _number(value, minimum, maximum, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise RegistryError(f"{name} must be a number from {minimum} to {maximum}")
    return value


def _object(value, entry, method):
    if not isinstance(value, dict):
        raise RegistryError(f"{clean(entry.slug)} returned invalid data for {method}")
    return value


def _choice(value, choices, name):
    if value not in choices:
        raise RegistryError(f"Invalid {name}")
    return value


def _bool(value, name):
    if type(value) is not bool:
        raise RegistryError(f"{name} must be true or false")
    return value


def _child(registry, device, kinds, models=None):
    entry = _entry(registry, device, kinds)
    if entry.root is None or entry.root.kind != "hub":
        raise RegistryError(f"{clean(entry.slug)} is not a hub child")
    if models is not None and entry.device.model.split("(")[0] not in models:
        raise RegistryError(f"{clean(entry.slug)} does not support this setting")
    return entry


def _fields(data, names):
    """Allow only named scalar fields. Never forward a device's arbitrary response."""
    out = {}
    for name in names:
        value = data.get(name)
        if isinstance(value, str):
            value = clean(value)
        if value is not None and value != "" and type(value) in (str, int, float, bool):
            if type(value) is float and not math.isfinite(value):
                continue
            out[name] = value
    return out


async def _write(registry, caller, entry, method, params, changed, *, confirm=None):
    await caller.call(entry, method, params, confirm=confirm)
    await registry.refresh(entry, force=True)
    out = {"device": clean(entry.slug), "changed": changed}
    if not entry.online or not entry.top.online:
        out["state_refreshed"] = False
        out["reason"] = clean(entry.error or entry.top.error or "State refresh failed")
    return out


def _smartcam(entry):
    # H200 and H500 use SMARTCAM. Prefer the actual connected device class.
    from kasa.smartcam import SmartCamDevice

    return isinstance(entry.device, SmartCamDevice) or entry.device.model.split("(")[0] in {"H200", "H500"}


async def alarm_config(registry, caller, device: str) -> dict[str, Any]:
    entry = _entry(registry, device, {"hub"})
    cam = _smartcam(entry)
    get_config, get_tones = ("getSirenConfig", "getSirenTypeList") if cam else (
        "get_alarm_configure", "get_support_alarm_type_list")
    params = {"siren": {}} if cam else None
    config = _object(await caller.call(entry, get_config, params), entry, get_config)
    tones = _object(await caller.call(entry, get_tones, params), entry, get_tones)
    config = _object(config.get("siren", config), entry, get_config)
    tones = tones.get("siren", tones)
    tone_list = tones.get("siren_type_list" if cam else "alarm_type_list") if isinstance(tones, dict) else None
    if not isinstance(tone_list, list) or not all(isinstance(t, str) for t in tone_list):
        raise RegistryError(f"{clean(entry.slug)} returned an invalid tone list")
    out = {"device": clean(entry.slug), "settings": _fields(config, ("siren_type", "type", "volume", "duration")),
           "tones": [clean(t) for t in tone_list[:100]]}
    if cam:
        status = _object(await caller.call(entry, "getSirenStatus", {"siren": {}}), entry, "getSirenStatus")
        out.update(_fields(status.get("siren", status), ("status",)))
    else:
        out.update(_fields(getattr(entry.device, "sys_info", {}), ("in_alarm", "in_alarm_source")))
    return out


async def set_alarm(registry, caller, device: str, *, sound: str | None = None,
                    volume: str | int | None = None, duration: int | None = None) -> dict[str, Any]:
    entry = _entry(registry, device, {"hub"})
    cam = _smartcam(entry)
    if sound is None and volume is None and duration is None:
        raise RegistryError("Choose at least one alarm setting")
    if duration is not None:
        # UNVERIFIED: python-kasa allows 0 on H200, but what 0 means (no sound or no time limit) is unknown.
        # A siren must always stop by itself, so 0 is refused.
        _integer(duration, 1, 6000 if cam else 600, "duration")
    if volume is not None:
        # Muting the hub siren removes a safety net. setSirenConfig is level 2 in every area, so this
        # level 2 tool refuses mute (H100 "mute", H200 0) instead of hiding a level 3 action inside it.
        if cam:
            _integer(volume, 1, 10, "volume")
        else:
            _choice(volume, ("low", "normal", "high"), "volume")
    if sound is not None:
        # Compare raw names, not shortened display labels.
        method = "getSirenTypeList" if cam else "get_support_alarm_type_list"
        tones = _object(await caller.call(entry, method, {"siren": {}} if cam else None), entry, method)
        tones = _object(tones.get("siren", tones), entry, method)
        _choice(sound, tones.get("siren_type_list" if cam else "alarm_type_list", []), "sound")
    changed = {}
    if sound is not None:
        changed["siren_type" if cam else "type"] = sound
    if volume is not None:
        changed["volume"] = str(volume) if cam else volume
    if duration is not None:
        changed["duration"] = duration
    # SMART setters carry the full existing configuration. Preserve only public known fields.
    payload = changed
    if not cam:
        current = _object(await caller.call(entry, "get_alarm_configure"), entry, "get_alarm_configure")
        if not all(key in current for key in ("type", "volume", "duration")):
            raise RegistryError(f"{clean(entry.slug)} returned invalid alarm settings")
        payload = {key: current[key] for key in ("type", "volume", "duration")}
        payload.update(changed)
    return await _write(registry, caller, entry, "setSirenConfig" if cam else "set_alarm_configure",
                        {"siren": payload} if cam else payload,
                        _fields(changed, ("siren_type", "type", "volume", "duration")))


async def sound_siren(registry, caller, device: str, enabled: bool, *, confirm=None) -> dict[str, Any]:
    _bool(enabled, "enabled")
    entry = _entry(registry, device, {"hub"})
    if _smartcam(entry):
        method, params = "setSirenStatus", {"siren": {"status": "on" if enabled else "off"}}
    else:
        method, params = ("play_alarm" if enabled else "stop_alarm"), None
    return await _write(registry, caller, entry, method, params, {"siren": enabled}, confirm=confirm)


async def sensor_setting(registry, caller, device: str, setting: SensorSetting) -> dict[str, Any]:
    _choice(setting, SENSOR_SETTINGS, "sensor setting")
    method, _, models, fields = SENSOR_SETTINGS[setting]
    entry = _child(registry, device, {"sensor"}, models)
    data = _object(await caller.call(entry, method), entry, method)
    return {"device": clean(entry.slug), "setting": setting, "values": _fields(data, fields)}


async def _sensor_write(registry, caller, device, setting, params, *, confirm=None):
    _, method, models, fields = SENSOR_SETTINGS[setting]
    entry = _child(registry, device, {"sensor"}, models)
    return await _write(registry, caller, entry, method, params, _fields(params, fields), confirm=confirm)


async def set_sensitivity(registry, caller, device: str, *, sensitivity: str | None = None,
                          report_interval: int | None = None) -> dict[str, Any]:
    params = {}
    if sensitivity is not None:
        # UNVERIFIED: the audit infers the `sensitivity` field name from an unannotated app field.
        params["sensitivity"] = _choice(sensitivity, ("low", "normal", "high"), "sensitivity")
    if report_interval is not None:
        # UNVERIFIED: the real interval limits are unknown. The owner's sensors report 16 seconds.
        params["report_interval"] = _integer(report_interval, 1, 86400, "report_interval")
    if not params:
        raise RegistryError("Choose sensitivity or report interval")
    return await _sensor_write(registry, caller, device, "sensitivity", params)


async def set_sensor_alarm(registry, caller, device: str, volume: Volume, *, duration: int | None = None,
                           sound: str | None = None, confirm=None) -> dict[str, Any]:
    params = {"volume": _choice(volume, ("mute", "low", "normal", "high"), "volume")}
    if duration is not None:
        # UNVERIFIED: the T300 duration limits are not in the audit. 1 to 600 seconds matches the H100 siren.
        params["duration"] = _integer(duration, 1, 600, "duration")
    if sound is not None:
        if not isinstance(sound, str) or not sound or len(sound) > 64 or clean(sound) != sound:
            raise RegistryError("Invalid sensor alarm tone")
        # UNVERIFIED: T300 tone choices are not supplied by the audit. Accept only its existing type.
        entry = _child(registry, device, {"sensor"}, {"T300"})
        config = _object(await caller.call(entry, "get_alarm_config"), entry, "get_alarm_config")
        if sound != config.get("type"):
            raise RegistryError("The sensor alarm tone must match its current type")
        params["type"] = sound
    return await _sensor_write(registry, caller, device, "alarm", params, confirm=confirm)


async def test_sensor_alarm(registry, caller, device: str, volume: Volume, *, confirm=None) -> dict[str, Any]:
    _choice(volume, ("low", "normal", "high"), "test volume")
    entry = _child(registry, device, {"sensor"}, {"T300"})
    return await _write(registry, caller, entry, "play_alarm", {"volume": volume}, {"siren_test": True, "volume": volume},
                        confirm=confirm)


async def set_comfort_temperature(registry, caller, device: str, minimum: float, maximum: float,
                                  unit: Unit, *, notification: bool | None = None) -> dict[str, Any]:
    _choice(unit, ("celsius", "fahrenheit"), "temperature unit")
    lo, hi = (-40, 85) if unit == "celsius" else (-40, 185)
    _number(minimum, lo, hi, "minimum")
    _number(maximum, lo, hi, "maximum")
    if minimum >= maximum:
        raise RegistryError("Minimum temperature must be below maximum")
    params = {"min_temp": minimum, "max_temp": maximum, "temp_unit": unit}
    if notification is not None:
        params["notification"] = _bool(notification, "notification")
    return await _sensor_write(registry, caller, device, "comfort_temperature", params)


async def set_comfort_humidity(registry, caller, device: str, minimum: int, maximum: int,
                               *, notification: bool | None = None) -> dict[str, Any]:
    _integer(minimum, 0, 100, "minimum")
    _integer(maximum, 0, 100, "maximum")
    if minimum >= maximum:
        raise RegistryError("Minimum humidity must be below maximum")
    params = {"min_humidity": minimum, "max_humidity": maximum}
    if notification is not None:
        params["notification"] = _bool(notification, "notification")
    return await _sensor_write(registry, caller, device, "comfort_humidity", params)


async def set_sensor_mode(registry, caller, device: str, mode: str) -> dict[str, Any]:
    return await _sensor_write(registry, caller, device, "report_mode",
                               {"report_mode": _choice(mode, ("high_sensitivity", "long_battery_life"), "report mode")})


async def set_sensor_led(registry, caller, device: str, enabled: bool) -> dict[str, Any]:
    return await _sensor_write(registry, caller, device, "led", {"led_onoff": int(_bool(enabled, "enabled"))})


def _valve(registry, device):
    return _child(registry, device, VALVE_KINDS, {"KE100"})


async def valve_setting(registry, caller, device: str, setting: ValveSetting) -> dict[str, Any]:
    _choice(setting, VALVE_SETTINGS, "valve setting")
    entry = _valve(registry, device)
    method, fields = VALVE_SETTINGS[setting]
    data = _object(await caller.call(entry, method), entry, method)
    values = _fields(data, fields)
    if setting == "night_mode" and isinstance(data.get("effective_time"), dict):
        effective = _fields(data["effective_time"], ("type", "start_min", "end_min", "sunrise_offset", "sunset_offset"))
        if effective:
            values["effective_time"] = effective
    if setting == "window" and isinstance(data.get("contact_sensor_list"), list):
        # Convert child radio identifiers to known local slugs. Unknown identifiers are not returned.
        ids = set(v for v in data["contact_sensor_list"] if isinstance(v, str))
        sensors = [clean(e.slug) for e in registry.entries() if e.root is entry.root and e.device.device_id in ids]
        if sensors:
            values["sensors"] = sensors
    return {"device": clean(entry.slug), "setting": setting, "values": values}


async def valve_timed(registry, caller, device: str, mode: str, enabled: bool,
                      seconds: int | None = None) -> dict[str, Any]:
    _choice(mode, ("boost", "hold"), "timed mode")
    _bool(enabled, "enabled")
    if enabled and seconds is None:
        raise RegistryError("A duration is required when enabling timed heating")
    params = {"enable": enabled}
    if seconds is not None:
        params["boost_time" if mode == "boost" else "hold_on_time"] = _integer(seconds, 1, 86400, "seconds")
    return await _write(registry, caller, _valve(registry, device),
                        "set_boost_info" if mode == "boost" else "set_holdon_info", params, params)


async def valve_defaults(registry, caller, device: str, mode: str, seconds: int,
                         temperature: int | None = None) -> dict[str, Any]:
    _choice(mode, ("boost", "hold"), "timed mode")
    _integer(seconds, 1, 86400, "seconds")
    entry = _valve(registry, device)
    if mode == "boost":
        params = {"default_boost_time": seconds}
        if temperature is not None:
            _temperature_bounds(entry, temperature)
            params["default_boost_temp"] = _integer(temperature, -40, 212, "temperature")
        method = "set_default_boost_info_v2"
    else:
        if temperature is not None:
            raise RegistryError("Hold defaults contain duration only")
        params, method = {"default_hold_on_time": seconds}, "set_default_holdon_info"
    return await _write(registry, caller, entry, method, params, params)


def _temperature_bounds(entry, temperature):
    info = getattr(entry.device, "sys_info", {})
    lo, hi = info.get("min_control_temp"), info.get("max_control_temp")
    if type(lo) not in (int, float) or type(hi) not in (int, float):
        raise RegistryError(f"{clean(entry.slug)} has no known target temperature bounds")
    _number(temperature, lo, hi, "temperature")


async def valve_window(registry, caller, device: str, enabled: bool, action: str,
                        sensors: list[str] | None = None) -> dict[str, Any]:
    _bool(enabled, "enabled")
    _choice(action, ("notification_only", "notification_shutoff", "shutoff_only"), "window action")
    entry = _valve(registry, device)
    if sensors is not None and (not isinstance(sensors, list) or len(sensors) > 16):
        raise RegistryError("Use at most 16 contact sensors")
    ids, slugs = [], []
    for ref in sensors or []:
        sensor = _child(registry, ref, {"sensor"}, {"T110"})
        if sensor.root is not entry.root:
            raise RegistryError(f"{clean(sensor.slug)} is on another hub")
        if sensor.device.device_id not in ids:
            ids.append(sensor.device.device_id)
            slugs.append(clean(sensor.slug))
    params = {"enable": enabled, "detected_action": action}
    if sensors is not None:
        params["contact_sensor_list"] = ids
    changed = {"enable": enabled, "detected_action": action}
    if sensors is not None:
        changed["sensors"] = slugs
    return await _write(registry, caller, entry, "set_window_open_detection", params, changed)


async def valve_maintenance(registry, caller, device: str, operation: str) -> dict[str, Any]:
    _choice(operation, ("calibrate", "descale"), "maintenance operation")
    method = "progress_calibration" if operation == "calibrate" else "remove_scale"
    return await _write(registry, caller, _valve(registry, device), method, None, {"started": operation})


async def valve_auto_descale(registry, caller, device: str, enabled: bool) -> dict[str, Any]:
    params = {"auto": _bool(enabled, "enabled")}
    return await _write(registry, caller, _valve(registry, device), "set_remove_scale_info", params, params)


async def valve_shutdown(registry, caller, device: str, enabled: bool, mode: str, *,
                          anti_clogging: bool | None = None,
                          confirm=None) -> dict[str, Any]:
    params = {"enable": _bool(enabled, "enabled"), "mode": _choice(mode, ("full_closed", "full_open"), "shutdown mode")}
    if anti_clogging is not None:
        params["anti_clogging_enable"] = _bool(anti_clogging, "anti_clogging")
    return await _write(registry, caller, _valve(registry, device), "set_shutdown_info", params, params, confirm=confirm)


async def valve_work_mode(registry, caller, device: str, mode: ValveMode) -> dict[str, Any]:
    params = {"trv_mode": _choice(mode, ("silent_mode", "standard_mode", "turbo_mode"), "work mode")}
    return await _write(registry, caller, _valve(registry, device), "set_trv_mode", params, params)


async def valve_night(registry, caller, device: str, enabled: bool, start_min: int | None = None,
                       end_min: int | None = None) -> dict[str, Any]:
    params = {"enable": _bool(enabled, "enabled")}
    if (start_min is None) != (end_min is None):
        raise RegistryError("Provide both night start and end minutes")
    if start_min is not None:
        _integer(start_min, 0, 1439, "start_min")
        _integer(end_min, 0, 1439, "end_min")
        if start_min == end_min:
            raise RegistryError("Night start and end must differ")
        params["effective_time"] = {"type": "custom", "start_min": start_min, "end_min": end_min}
    return await _write(registry, caller, _valve(registry, device), "set_night_mode_info", params, params)


async def valve_early(registry, caller, device: str, enabled: bool) -> dict[str, Any]:
    params = {"enable": _bool(enabled, "enabled")}
    return await _write(registry, caller, _valve(registry, device), "set_early_start", params, params)


async def valve_frost(registry, caller, device: str, minimum: int, unit: Unit, *, confirm=None) -> dict[str, Any]:
    _choice(unit, ("celsius", "fahrenheit"), "temperature unit")
    # The app offers only 5 to 15 C and 41 to 59 F. A lower minimum risks frozen pipes.
    _integer(minimum, 5 if unit == "celsius" else 41, 15 if unit == "celsius" else 59, "minimum")
    params = {"min_temp": minimum, "temp_unit": unit}
    return await _write(registry, caller, _valve(registry, device), "set_frost_protection", params, params, confirm=confirm)


async def valve_schedule_mode(registry, caller, device: str, enabled: bool,
                              mode: ScheduleMode | None = None) -> dict[str, Any]:
    params = {"enable": _bool(enabled, "enabled")}
    if mode is not None:
        params["mode"] = _choice(mode, ("everyday", "weekday_sat_sun"), "schedule mode")
    return await _write(registry, caller, _valve(registry, device), "set_sc_schedule_info", params, params)


async def valve_schedule_rules(registry, caller, device: str, mode: ScheduleMode) -> dict[str, Any]:
    _choice(mode, ("everyday", "weekday_sat_sun"), "schedule mode")
    entry = _valve(registry, device)
    data = _object(await caller.call(entry, "get_sc_schedule_rules", {"schedule_mode": mode, "start_index": 0}),
                   entry, "get_sc_schedule_rules")
    schedules = data.get("schedule_list")
    if not isinstance(schedules, list):
        raise RegistryError(f"{clean(entry.slug)} returned invalid schedule rules")
    out = []
    for schedule in schedules[:7]:
        if not isinstance(schedule, dict) or not isinstance(schedule.get("rule_list"), list):
            raise RegistryError(f"{clean(entry.slug)} returned an invalid schedule")
        item = _fields(schedule, ("id",))
        rules = []
        for rule in schedule["rule_list"][:48]:
            if not isinstance(rule, dict):
                raise RegistryError(f"{clean(entry.slug)} returned an invalid schedule rule")
            value = _fields(rule, ("start_min", "end_min"))
            states = rule.get("desired_states")
            if isinstance(states, dict):
                safe = _fields(states, ("target_temp", "target_temp_float", "temp_unit", "frost_protection_on"))
                if safe:
                    value["desired_states"] = safe
            if value:
                rules.append(value)
        item["rules"] = rules
        out.append(item)
    return {"device": clean(entry.slug), **_fields(data, ("schedule_mode", "day_segment")), "schedules": out}


async def local_history(registry, caller, device: str, *, max_points: int = 48, valve: bool = False) -> dict[str, Any]:
    """Read native local arrays. Do not infer sample times, exception meanings or temperature scaling."""
    _integer(max_points, 1, 2880, "max_points")
    if valve:
        entry = _valve(registry, device)
        method = "get_temperature_records"
        fields = ("today", "yesterday", "today_status", "yesterday_status")
    else:
        entry = _child(registry, device, {"sensor"}, {"T310", "T315"})
        method = "get_temp_humidity_records"
        fields = ("past24h_temp", "past24h_humidity", "past24h_temp_exception", "past24h_humidity_exception")
    data = _object(await caller.call(entry, method), entry, method)
    if not any(isinstance(data.get(field), list) for field in fields):
        raise RegistryError(f"{clean(entry.slug)} returned invalid local history")
    out = {"device": clean(entry.slug), **_fields(data, ("temp_unit", "local_time", "current_temp"))}
    omitted = {}
    for field in fields:
        values = data.get(field)
        if values is None:
            continue
        if not isinstance(values, list) or any(v is not None and (
                type(v) not in (int, float) or not math.isfinite(v)) for v in values):
            raise RegistryError(f"{clean(entry.slug)} returned invalid history values")
        if values:
            out[field] = values[:max_points]
        if len(values) > max_points:
            omitted[field] = len(values) - max_points
    if omitted:
        out["omitted"] = omitted
    return out


async def rename_child(registry, caller, device: str, name: str) -> dict[str, Any]:
    if not isinstance(name, str) or not name or len(name) > 64 or clean(name) != name:
        raise RegistryError("Name must be 1 to 64 characters on one line without surrounding spaces")
    entry = _child(registry, device, {"sensor", "valve", "thermostat"})
    params = {"nickname": base64.b64encode(name.encode("utf-8")).decode("ascii")}
    return await _write(registry, caller, entry, "set_device_info", params, {"name": clean(name)})


async def hub_led_setting(registry, caller, device: str, enabled: bool | None = None) -> dict[str, Any]:
    entry = _entry(registry, device, {"hub"})
    cam = _smartcam(entry)
    if enabled is not None:
        _bool(enabled, "enabled")
        if cam:
            method, params = "setLedStatus", {"led": {"config": {"enabled": "on" if enabled else "off"}}}
        else:
            current = _object(await caller.call(entry, "get_led_info"), entry, "get_led_info")
            fields = ("led_rule", "start_time", "end_time", "night_mode_type", "sunrise_offset", "sunset_offset")
            params = {k: current[k] for k in fields if k in current}
            params["led_rule"] = "always" if enabled else "never"
            method = "set_led_info"
        return await _write(registry, caller, entry, method, params, {"led": enabled})
    method = "getLedStatus" if cam else "get_led_info"
    data = _object(await caller.call(entry, method, {"led": {"name": "config"}} if cam else None), entry, method)
    if cam:
        data = _object(data.get("led", data), entry, method)
        data = _object(data.get("config"), entry, method)
        values = _fields(data, ("enabled",))
    else:
        values = _fields(data, ("led_rule", "start_time", "end_time", "night_mode_type", "sunrise_offset", "sunset_offset"))
    return {"device": clean(entry.slug), "settings": values}


async def hub_diagnostics(registry, caller, device: str) -> dict[str, Any]:
    entry = _entry(registry, device, {"hub"})
    cam = _smartcam(entry)
    method = "getDeviceLoadInfo" if cam else "get_device_load_info"
    # UNVERIFIED: getDeviceLoadInfo and its `deviceLoad` module are app names. The H200 parameter shape is a guess.
    data = _object(await caller.call(entry, method, {"deviceLoad": {}} if cam else None), entry, method)
    data = _object(data.get("deviceLoad", data), entry, method)
    return {"device": clean(entry.slug), **_fields(data, ("cur_load_num", "max_load_num", "load_level",
        "memory_usage", "total_memory", "used_memory", "cam_cur_load_num", "cam_max_load_num", "cam_load_level",
        "matter_cur_load_num", "matter_max_load_num", "matter_load_level"))}


@dataclass(frozen=True)
class HubEvent:
    """One child event. Dedupe by (device, id, timestamp) when polling overlapping windows."""

    device: str
    id: int
    timestamp: int
    event: str
    event_id: str | None = None
    rotate_deg: float | None = None

    def compact(self) -> dict[str, Any]:
        out = {"device": self.device, "id": self.id, "timestamp": self.timestamp,
               "time": datetime.fromtimestamp(self.timestamp, timezone.utc).isoformat(), "event": self.event}
        if self.event_id:
            out["event_id"] = self.event_id
        if self.rotate_deg is not None:
            out["rotate_deg"] = self.rotate_deg
        return out


async def recent_events(registry: Registry, caller: DeviceCaller, hub: str, *, device: str | None = None,
                        limit: int = 50, after: int | None = None) -> list[HubEvent]:
    """Poll live child trigger logs, newest first. This does not use kasa's hourly module cache.

    Fetch at most 100 recent records per child. `after` is an exclusive Unix time filter.
    A failure names the child and aborts the read, so automation never mistakes failure for no events.
    """
    _integer(limit, 1, 100, "limit")
    if after is not None:
        _integer(after, 0, 253402300799, "after")
    parent = _entry(registry, hub, {"hub"})
    # Only children of this hub. A child of another hub is never read through this one.
    children = [e for e in registry.entries()
                if e.root is parent and e.kind in EVENT_KINDS and Module.TriggerLogs in e.device.modules]
    if device is not None:
        child = registry.resolve_one(device)
        if child not in children:
            raise RegistryError(f"{clean(child.slug)} has no trigger log on {clean(parent.slug)}")
        children = [child]
    events = {}
    for child in children:
        # UNVERIFIED: page_size 10 and no page_size were accepted by the owner's sensors (empty logs).
        # Whether firmware honors 100 or caps it lower is unknown.
        data = _object(await caller.call(child, "get_trigger_logs", {"start_id": 0, "page_size": 100}),
                       child, "get_trigger_logs")
        logs = data.get("logs")
        if not isinstance(logs, list):
            raise RegistryError(f"{clean(child.slug)} returned invalid trigger logs")
        for log in logs:
            if (not isinstance(log, dict) or type(log.get("id")) is not int
                    or type(log.get("timestamp")) is not int or not 0 <= log["timestamp"] <= 253402300799
                    or not isinstance(log.get("event"), str)):
                raise RegistryError(f"{clean(child.slug)} returned an invalid trigger event")
            if after is not None and log["timestamp"] <= after:
                continue
            degrees = (log.get("params") or {}).get("rotate_deg") if isinstance(log.get("params"), dict) else None
            if type(degrees) not in (int, float) or not math.isfinite(degrees):
                degrees = None
            event = HubEvent(clean(child.slug), log["id"], log["timestamp"], clean(log["event"]),
                             clean(log["eventId"]) if isinstance(log.get("eventId"), str) else None, degrees)
            events[(event.device, event.id, event.timestamp)] = event
    return sorted(events.values(), key=lambda e: (e.timestamp, e.id, e.device), reverse=True)[:limit]


def tools(ctx):
    @ctx.tool(title="Local climate sensor history")
    async def sensor_history(
        device: DeviceRef,
        max_points: Annotated[int, Field(description="Maximum values per native 24-hour array. Omitted counts report truncation", ge=1, le=2880)] = 48,
    ) -> dict[str, Any]:
        """Read T310/T315 local 24-hour temperature and humidity arrays in native order and units."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await local_history(ctx.registry, ctx.caller, device, max_points=max_points)

    @ctx.tool(title="Local valve history")
    async def valve_history(
        device: DeviceRef,
        max_points: Annotated[int, Field(description="Maximum values per native day array. Omitted counts report truncation", ge=1, le=2880)] = 48,
    ) -> dict[str, Any]:
        """Read locally stored KE100 today and yesterday temperature arrays and status flags."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await local_history(ctx.registry, ctx.caller, device, max_points=max_points, valve=True)

    @ctx.tool(title="Rename hub child", tier=Tier.SETTING)
    async def hub_child_rename(
        device: DeviceRef,
        name: Annotated[str, Field(description="New sensor or valve name on one line", min_length=1, max_length=64)],
    ) -> dict[str, Any]:
        """Rename a sensor or valve through its hub and refresh its state. Its tolpo id stays stable."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await rename_child(ctx.registry, ctx.caller, device, name)

    @ctx.tool(title="Read hub LED settings")
    async def hub_led(device: DeviceRef) -> dict[str, Any]:
        """Read H100 or H200 status LED settings."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await hub_led_setting(ctx.registry, ctx.caller, device)

    @ctx.tool(title="Set hub LED", tier=Tier.SETTING)
    async def hub_led_set(device: DeviceRef,
                          enabled: Annotated[bool, Field(description="Enable the hub status LED")]) -> dict[str, Any]:
        """Set H100 or H200 status LED behavior and refresh its state."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await hub_led_setting(ctx.registry, ctx.caller, device, enabled)

    @ctx.tool(title="Read hub load diagnostics")
    async def hub_diagnostics_read(device: DeviceRef) -> dict[str, Any]:
        """Read hub load limits and memory counters. Omits identifiers and support logs."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await hub_diagnostics(ctx.registry, ctx.caller, device)

    @ctx.tool(title="Read valve setting")
    async def valve_settings(device: DeviceRef,
                             setting: Annotated[ValveSetting, Field(description="Which KE100 setting or maintenance status to read")]) -> dict[str, Any]:
        """Read one KE100 setting or mechanical maintenance status through its hub."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await valve_setting(ctx.registry, ctx.caller, device, setting)

    @ctx.tool(title="Timed valve boost or hold", tier=Tier.SETTING)
    async def valve_timed_set(
        device: DeviceRef,
        mode: Annotated[Literal["boost", "hold"], Field(description="Boost heating or hold the current target")],
        enabled: Annotated[bool, Field(description="Start or cancel timed heating")],
        seconds: Annotated[int | None, Field(description="Duration in seconds, required when enabling", ge=1, le=86400)] = None,
    ) -> dict[str, Any]:
        """Start or cancel KE100 boost or hold, then return to its plan when the timer ends."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await valve_timed(ctx.registry, ctx.caller, device, mode, enabled, seconds)

    @ctx.tool(title="Set valve boost or hold defaults", tier=Tier.SETTING)
    async def valve_defaults_set(
        device: DeviceRef,
        mode: Annotated[Literal["boost", "hold"], Field(description="Default boost or hold settings")],
        seconds: Annotated[int, Field(description="Default duration in seconds", ge=1, le=86400)],
        temperature: Annotated[int | None, Field(description="Boost only: temperature in the valve's current unit and target range")] = None,
    ) -> dict[str, Any]:
        """Store KE100 boost duration and optional temperature, or default hold duration."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await valve_defaults(ctx.registry, ctx.caller, device, mode, seconds, temperature)

    @ctx.tool(title="Set valve open-window detection", tier=Tier.SETTING)
    async def valve_window_set(
        device: DeviceRef,
        enabled: Annotated[bool, Field(description="Enable open-window detection")],
        action: Annotated[Literal["notification_only", "notification_shutoff", "shutoff_only"], Field(description="What to do when a window opens")],
        sensors: Annotated[list[str] | None, Field(description="T110 names or ids on this hub. Empty list clears links", max_length=16)] = None,
    ) -> dict[str, Any]:
        """Set KE100 open-window behavior and optional contact sensors on the same hub."""
        await ctx.ready_for([device] + (sensors or []))
        with ctx.translated():
            return await valve_window(ctx.registry, ctx.caller, device, enabled, action, sensors)

    @ctx.tool(title="Calibrate or descale valve", tier=Tier.SETTING)
    async def valve_maintenance_start(
        device: DeviceRef,
        operation: Annotated[Literal["calibrate", "descale"], Field(description="Mechanical calibration or scale-removal cycle")],
    ) -> dict[str, Any]:
        """Start a KE100 mechanical cycle. Poll valve_settings for calibration or descaling status."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await valve_maintenance(ctx.registry, ctx.caller, device, operation)

    @ctx.tool(title="Set automatic valve descaling", tier=Tier.SETTING)
    async def valve_descaling_auto(device: DeviceRef,
                                   enabled: Annotated[bool, Field(description="Enable automatic scale-removal cycles")]) -> dict[str, Any]:
        """Enable or disable KE100 automatic descaling."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await valve_auto_descale(ctx.registry, ctx.caller, device, enabled)

    @ctx.tool(title="Set valve shutdown", tier=Tier.SECURITY)
    async def valve_shutdown_set(
        device: DeviceRef,
        enabled: Annotated[bool, Field(description="Enable or cancel seasonal shutdown")],
        mode: Annotated[Literal["full_closed", "full_open"], Field(description="Position during shutdown. Fully closed can risk frozen pipes")],
        anti_clogging: Annotated[bool | None, Field(description="Optional weekly anti-blocking movement switch")] = None,
        confirm: Confirmation = None,
    ) -> dict[str, Any]:
        """Set KE100 seasonal shutdown. Requires confirm='set_shutdown_info <valve id>'."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await valve_shutdown(ctx.registry, ctx.caller, device, enabled, mode,
                                         anti_clogging=anti_clogging, confirm=confirm)

    @ctx.tool(title="Set valve motor mode", tier=Tier.SETTING)
    async def valve_work_mode_set(device: DeviceRef,
                                  mode: Annotated[ValveMode, Field(description="Gentle, standard or powerful motor behavior")]) -> dict[str, Any]:
        """Set KE100 motor behavior to silent_mode, standard_mode or turbo_mode."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await valve_work_mode(ctx.registry, ctx.caller, device, mode)

    @ctx.tool(title="Set valve night mode", tier=Tier.SETTING)
    async def valve_night_set(
        device: DeviceRef,
        enabled: Annotated[bool, Field(description="Enable reduced motor movement at night")],
        start_min: Annotated[int | None, Field(description="Start minute after midnight in device local time", ge=0, le=1439)] = None,
        end_min: Annotated[int | None, Field(description="End minute after midnight in device local time", ge=0, le=1439)] = None,
    ) -> dict[str, Any]:
        """Set KE100 night mode with optional fixed local hours. Hours can cross midnight."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await valve_night(ctx.registry, ctx.caller, device, enabled, start_min, end_min)

    @ctx.tool(title="Set valve early start", tier=Tier.SETTING)
    async def valve_early_start(device: DeviceRef,
                                enabled: Annotated[bool, Field(description="Heat early to reach scheduled targets on time")]) -> dict[str, Any]:
        """Enable or disable KE100 early heating for schedule targets."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await valve_early(ctx.registry, ctx.caller, device, enabled)

    @ctx.tool(title="Set valve frost minimum", tier=Tier.SECURITY)
    async def valve_frost_set(
        device: DeviceRef,
        minimum: Annotated[int, Field(description="Frost-protection minimum: 5 to 15 C or 41 to 59 F. Lowering it can risk frozen pipes")],
        unit: Annotated[Unit, Field(description="Temperature unit")],
        confirm: Confirmation = None,
    ) -> dict[str, Any]:
        """Set KE100 frost minimum. Requires confirm='set_frost_protection <valve id>'."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await valve_frost(ctx.registry, ctx.caller, device, minimum, unit, confirm=confirm)

    @ctx.tool(title="Set valve schedule mode", tier=Tier.SETTING)
    async def valve_schedule_set(
        device: DeviceRef,
        enabled: Annotated[bool, Field(description="Enable the existing heating schedule")],
        mode: Annotated[ScheduleMode | None, Field(description="Every day or weekday plus separate Saturday and Sunday plans")] = None,
    ) -> dict[str, Any]:
        """Enable or choose the KE100 local heating schedule mode. Does not edit its blocks."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await valve_schedule_mode(ctx.registry, ctx.caller, device, enabled, mode)

    @ctx.tool(title="Read valve schedule blocks")
    async def valve_schedule(
        device: DeviceRef,
        mode: Annotated[ScheduleMode, Field(description="Schedule mode whose local blocks to read")],
    ) -> dict[str, Any]:
        """Read compact local KE100 schedule blocks. Preserve firmware temperature encoding."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await valve_schedule_rules(ctx.registry, ctx.caller, device, mode)

    @ctx.tool(title="Read sensor setting")
    async def sensor_settings(device: DeviceRef,
                              setting: Annotated[SensorSetting, Field(description="The sensor setting to read")]) -> dict[str, Any]:
        """Read one supported sensor setting. Returns only public setting fields."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await sensor_setting(ctx.registry, ctx.caller, device, setting)

    @ctx.tool(title="Set motion sensor tuning", tier=Tier.SETTING)
    async def sensor_sensitivity(
        device: DeviceRef,
        sensitivity: Annotated[Literal["low", "normal", "high"] | None, Field(description="Motion sensitivity")] = None,
        report_interval: Annotated[int | None, Field(description="Minimum seconds between reports", ge=1, le=86400)] = None,
    ) -> dict[str, Any]:
        """Set T100/T150 motion sensitivity or detection interval. Firmware may impose narrower limits."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await set_sensitivity(ctx.registry, ctx.caller, device, sensitivity=sensitivity, report_interval=report_interval)

    @ctx.tool(title="Set leak sensor siren settings", tier=Tier.SECURITY)
    async def sensor_alarm_set(
        device: DeviceRef,
        volume: Annotated[Volume, Field(description="Leak siren volume. Mute silences leak warnings")],
        duration: Annotated[int | None, Field(description="Siren duration in seconds", ge=1, le=600)] = None,
        sound: Annotated[str | None, Field(description="Optional current tone type from sensor_settings")] = None,
        confirm: Confirmation = None,
    ) -> dict[str, Any]:
        """Set T300 leak siren volume and duration. Does not sound the alarm.

        Level 3, because muting or shortening the siren weakens leak warnings.
        Requires confirm='set_alarm_config <sensor id>'.
        """
        await ctx.ready_for([device])
        with ctx.translated():
            return await set_sensor_alarm(ctx.registry, ctx.caller, device, volume, duration=duration, sound=sound,
                                          confirm=confirm)

    @ctx.tool(title="Test leak sensor siren", tier=Tier.SECURITY)
    async def sensor_siren(
        device: DeviceRef,
        volume: Annotated[Literal["low", "normal", "high"], Field(description="Audible test volume")],
        confirm: Confirmation = None,
    ) -> dict[str, Any]:
        """Play the T300 siren test. Requires confirm='play_alarm <sensor id>'."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await test_sensor_alarm(ctx.registry, ctx.caller, device, volume, confirm=confirm)

    @ctx.tool(title="Set sensor temperature comfort zone", tier=Tier.SETTING)
    async def sensor_comfort_temperature(
        device: DeviceRef,
        minimum: Annotated[float, Field(description="Lower comfort temperature")],
        maximum: Annotated[float, Field(description="Upper comfort temperature")],
        unit: Annotated[Unit, Field(description="Temperature unit")],
        notification: Annotated[bool | None, Field(description="Optional local comfort warning flag")] = None,
    ) -> dict[str, Any]:
        """Set T310/T315 temperature comfort thresholds. Cloud push rules are separate."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await set_comfort_temperature(ctx.registry, ctx.caller, device, minimum, maximum, unit,
                                                 notification=notification)

    @ctx.tool(title="Set sensor humidity comfort zone", tier=Tier.SETTING)
    async def sensor_comfort_humidity(
        device: DeviceRef,
        minimum: Annotated[int, Field(description="Lower comfort humidity percent", ge=0, le=100)],
        maximum: Annotated[int, Field(description="Upper comfort humidity percent", ge=0, le=100)],
        notification: Annotated[bool | None, Field(description="Optional local comfort warning flag")] = None,
    ) -> dict[str, Any]:
        """Set T310/T315 humidity comfort thresholds. Cloud push rules are separate."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await set_comfort_humidity(ctx.registry, ctx.caller, device, minimum, maximum, notification=notification)

    @ctx.tool(title="Set climate sensor operation mode", tier=Tier.SETTING)
    async def sensor_mode(
        device: DeviceRef,
        mode: Annotated[Literal["high_sensitivity", "long_battery_life"], Field(description="High sensitivity or balanced reporting")],
    ) -> dict[str, Any]:
        """Set T310/T315 reporting mode for responsiveness or battery life."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await set_sensor_mode(ctx.registry, ctx.caller, device, mode)

    @ctx.tool(title="Set sensor indicator LED", tier=Tier.SETTING)
    async def sensor_led(device: DeviceRef,
                         enabled: Annotated[bool, Field(description="Enable the sensor indicator LED")]) -> dict[str, Any]:
        """Set a child sensor LED with led_onoff. Requires firmware support for ble_sensor_led."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await set_sensor_led(ctx.registry, ctx.caller, device, enabled)

    @ctx.tool(title="Hub alarm settings and tones")
    async def hub_alarm_config(device: DeviceRef) -> dict[str, Any]:
        """Read hub siren settings, state and available tones, including H100 chime tones."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await alarm_config(ctx.registry, ctx.caller, device)

    @ctx.tool(title="Set hub alarm settings", tier=Tier.SETTING)
    async def hub_alarm_set(
        device: DeviceRef,
        sound: Annotated[str | None, Field(description="Exact tone name from hub_alarm_config")] = None,
        volume: Annotated[str | int | None, Field(description="H100: low, normal or high. H200: integer 1 to 10. Mute is refused")] = None,
        duration: Annotated[int | None, Field(description="Seconds. H100: 1 to 600. H200: 1 to 6000")] = None,
    ) -> dict[str, Any]:
        """Set hub siren or H100 chime tone, volume and duration. Does not play the sound. Does not mute the siren."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await set_alarm(ctx.registry, ctx.caller, device, sound=sound, volume=volume, duration=duration)

    @ctx.tool(title="Sound or stop hub siren", tier=Tier.SECURITY)
    async def hub_siren(device: DeviceRef,
                        enabled: Annotated[bool, Field(description="True sounds the configured tone; false stops it")],
                        confirm: Confirmation = None) -> dict[str, Any]:
        """Sound or stop the configured hub siren. Audible alarm. Requires method and device confirmation.

        H100 uses play_alarm or stop_alarm. H200 uses setSirenStatus for both actions.
        """
        await ctx.ready_for([device])
        with ctx.translated():
            return await sound_siren(ctx.registry, ctx.caller, device, enabled, confirm=confirm)

    @ctx.tool(title="Recent hub child events")
    async def hub_events(
        hub: DeviceRef,
        device: Annotated[str | None, Field(description="Optional child device id or name on this hub")] = None,
        limit: Annotated[int, Field(description="Maximum number of events", ge=1, le=100)] = 50,
        after: Annotated[int | None, Field(description="Only events after this Unix timestamp", ge=0)] = None,
    ) -> dict[str, Any]:
        """Read recent door, leak, motion, button and dial events with UTC time. Poll for automation.

        Reads up to 100 recent records per child. Overlap polls and dedupe by device, id and timestamp.
        """
        await ctx.ready_for([hub] + ([device] if device else []))
        with ctx.translated():
            events = await recent_events(ctx.registry, ctx.caller, hub, device=device, limit=limit, after=after)
            return {"hub": clean(ctx.registry.resolve_one(hub).slug), "events": [e.compact() for e in events]}
