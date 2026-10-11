"""Audit and sanitized camera/doorbell fixture shapes, with a canned wire only."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from kasa import DeviceType
from kasa.exceptions import DeviceError

from tolpo.areas import camera_detection as area
from tolpo.calls import NeedsConfirmation
from tolpo.registry import BadRequest, DeviceUnavailable, Forbidden

pytestmark = pytest.mark.anyio
CAM = "hallway-cam"


def house(make_house, *, tier=3, kind="camera"):
    reg, caller, devices = make_house(tier=tier)
    entry = reg.resolve_one(CAM)
    if kind == "doorbell":
        devices["cam"].device_type = DeviceType.Doorbell
    reg.refresh = AsyncMock()
    components = {v[0] for v in area.DETECTIONS.values()} | {"alert", "manualAlarm", "floodlight", "whiteLamp", "msgPush", "playback", "sdCard"}
    caller.respond("getAppComponentList", {"app_component": {"app_component_list": [{"name": c, "version": 1} for c in components]}})
    for detection, (_, module, section, stem, preferred) in area.DETECTIONS.items():
        body = {section: {"enabled": "on", preferred: "50"}, "capability": {"sensitivity_range": "0-100"}}
        if detection in ("line_crossing", "intrusion"):
            body["arming_schedule"] = {day: '["0000-2400"]' for day in area.DAYS}
        caller.respond(f"get{stem}Config", {module: body})
        if detection in area.ZONES:
            body["region_info"] = [{"region_info_1": {"x_coor": "0", "y_coor": "0", "width": "100", "height": "100"}}]
            caller.respond(area.ZONES[detection][0], {module: body})
    caller.respond("getPersonDetectionCapability", {"people_detection": {"capability": {"sensitivity_range": "0-100"}}})
    caller.respond("getBCDCapability", {"sound_detection": {"capability": {"sensitivity_range": "0-100"}}})
    return reg, caller, devices


@pytest.mark.parametrize("kind", ["camera", "doorbell"])
@pytest.mark.parametrize("detection", area.DETECTIONS)
async def test_detection_read_write_exact_wire_and_refresh(make_house, detection, kind):
    reg, caller, _ = house(make_house, kind=kind)
    _, module, section, stem, preferred = area.DETECTIONS[detection]
    read = await area.detection_get(reg, caller, CAM, detection)
    assert read["settings"]["enabled"] == "on"
    assert read["capability"] == {"sensitivity_range": "0-100"}
    result = await area.detection_set(reg, caller, CAM, detection, False, 75)
    patch = {"enabled": "off", preferred: "75"}
    assert result == {"device": CAM, "detection": detection, "changed": patch}
    assert caller.sent[-1] == (CAM, f"set{stem}Config", {module: {section: patch}})
    reg.refresh.assert_awaited_once_with(reg.resolve_one(CAM), force=True)
    assert caller.sent[0] == (CAM, "getAppComponentList", {"app_component": {"name": "app_component_list"}})


async def test_a_doorbell_behind_a_hub_gets_its_components_from_the_hub(make_house):
    """The D230 is a child of the H200. It does not answer getAppComponentList, the hub lists its components."""
    reg, caller, devices = house(make_house, kind="doorbell")
    entry = reg.resolve_one(CAM)
    entry.root = reg.resolve_one("tapo-h200")
    components = [{"name": "detection", "version": 1}, {"name": "alert", "version": 1}]

    def pages(_, params):
        start = params["childControl"]["start_index"]
        row = {"device_id": "other" if start == 0 else entry.device.device_id, "component_list": components}
        return {"start_index": start, "sum": 2, "child_component_list": [row]}

    caller.respond("getChildDeviceComponentList", pages)
    await area.detection_get(reg, caller, CAM, "motion")
    asked = [(slug, method, params) for slug, method, params in caller.sent if method.endswith("ComponentList")]
    assert asked == [
        ("tapo-h200", "getChildDeviceComponentList", {"childControl": {"start_index": 0}}),
        ("tapo-h200", "getChildDeviceComponentList", {"childControl": {"start_index": 1}}),
    ]
    assert "getAppComponentList" not in caller.methods_sent()


async def test_a_hub_that_does_not_list_the_camera_is_an_error_and_sends_nothing_else(make_house):
    reg, caller, _ = house(make_house, kind="doorbell")
    reg.resolve_one(CAM).root = reg.resolve_one("tapo-h200")
    caller.respond("getChildDeviceComponentList", {"child_component_list": [], "sum": 0})
    with pytest.raises(BadRequest, match="did not report"):
        await area.detection_set(reg, caller, CAM, "motion", False)
    assert caller.methods_sent() == ["getChildDeviceComponentList"]
    caller.respond("getChildDeviceComponentList", {"child_component_list": "nope"})
    with pytest.raises(DeviceUnavailable, match="unexpected component response"):
        await area.detection_get(reg, caller, CAM, "motion")


async def test_a_hub_that_promises_more_children_than_it_lists_is_asked_only_once(make_house):
    reg, caller, _ = house(make_house, kind="doorbell")
    reg.resolve_one(CAM).root = reg.resolve_one("tapo-h200")
    caller.respond("getChildDeviceComponentList", {"child_component_list": [], "sum": 99})
    with pytest.raises(BadRequest, match="did not report"):
        await area.detection_get(reg, caller, CAM, "motion")
    assert caller.methods_sent() == ["getChildDeviceComponentList"]


@pytest.mark.parametrize("detection", area.DETECTIONS)
async def test_every_detection_write_refused_by_level(make_house, detection):
    reg, caller, _ = house(make_house, tier=1)
    with pytest.raises(Forbidden, match="level 2") as error:
        await area.detection_set(reg, caller, CAM, detection, False)
    assert "allow_methods" not in str(error.value)
    assert not any(m.startswith("set") for m in caller.methods_sent())
    reg.refresh.assert_not_awaited()


@pytest.mark.parametrize("detection", area.DETECTIONS)
async def test_every_detection_error_reaches_user(make_house, detection):
    reg, caller, _ = house(make_house)
    method = f"get{area.DETECTIONS[detection][3]}Config"
    caller.respond(method, DeviceError("private raw device error", error_code=None))
    with pytest.raises(DeviceUnavailable, match="hallway-cam.*reported an error") as error:
        await area.detection_get(reg, caller, CAM, detection)
    assert "private raw" not in str(error.value)


async def test_unsupported_components_and_wrong_kind_never_write(make_house):
    reg, caller, _ = house(make_house)
    caller.respond("getAppComponentList", {"app_component": {"app_component_list": []}})
    with pytest.raises(BadRequest, match="does not support person"):
        await area.detection_set(reg, caller, CAM, "person", False)
    assert caller.methods_sent() == ["getAppComponentList"]
    with pytest.raises(BadRequest, match="cannot use camera"):
        await area.detection_get(reg, caller, "desk-lamp", "motion")


@pytest.mark.parametrize("enabled,sensitivity", [(None, None), ("off", None), (None, True), (None, {}), (None, -1), (None, 101), (None, "secret")])
async def test_bad_detection_inputs_never_write(make_house, enabled, sensitivity):
    reg, caller, _ = house(make_house)
    with pytest.raises(BadRequest):
        await area.detection_set(reg, caller, CAM, "motion", enabled, sensitivity)
    assert not any(m.startswith("set") for m in caller.methods_sent())


async def test_missing_range_or_sensitivity_refused_and_choice_range_supported(make_house):
    reg, caller, _ = house(make_house)
    for config in ({"enabled": "on"}, {"enabled": "on", "digital_sensitivity": "50"}):
        caller.respond("getDetectionConfig", {"motion_detection": {"motion_det": config}})
        with pytest.raises(BadRequest):
            await area.detection_set(reg, caller, CAM, "motion", sensitivity=60)
    caller.respond("getDetectionConfig", {"motion_detection": {"motion_det": {"sensitivity": "low"}, "capability": {"sensitivity_range": ["low", "high"]}}})
    assert (await area.detection_set(reg, caller, CAM, "motion", sensitivity="high"))["changed"] == {"sensitivity": "high"}


async def test_detection_output_hides_secrets_and_flattens_text(make_house):
    reg, caller, _ = house(make_house)
    caller.respond("getDetectionConfig", {"motion_detection": {"motion_det": {"enabled": "on", "target": "person\n\x00pet", "password": "SECRET", "ssid": "SECRET", "mac": "SECRET", "sensitivity": ""}, "secret": "SECRET"}})
    result = await area.detection_get(reg, caller, CAM, "motion")
    assert result == {"device": CAM, "detection": "motion", "settings": {"enabled": "on", "target": "person pet"}}
    assert "SECRET" not in str(result)


ZONE_INPUTS = {
    "motion": {"x_coor": 10, "y_coor": 10, "width": 500, "height": 500},
    "line_crossing": {"pt1_x": 0, "pt1_y": 0, "pt2_x": 100, "pt2_y": 100, "direction": "both", "sensitivity": 50},
    "intrusion": {**{f"pt{i}_{a}": i * 100 for i in range(1, 5) for a in ("x", "y")}, "sensitivity": 50, "threshold": 0, "percentage": 0},
    "package": {**{f"pt{i}_{a}": i * 100 for i in range(1, 4) for a in ("x", "y")}, "point_num": 3},
}


@pytest.mark.parametrize("detection", area.ZONES)
async def test_zone_read_and_replacement(make_house, detection):
    reg, caller, _ = house(make_house)
    module = area.DETECTIONS[detection][1]
    getter, setter, section = area.ZONES[detection]
    caller.respond(getter, {module: {"region_info": [{"zone_1": {**ZONE_INPUTS[detection], "token": "SECRET"}}]}})
    read = await area.zones_get(reg, caller, CAM, detection)
    assert read["zone_count"] == 1 and "SECRET" not in str(read)
    result = await area.zones_set(reg, caller, CAM, detection, [ZONE_INPUTS[detection]])
    wire = [{k: str(v) for k, v in ZONE_INPUTS[detection].items()}]
    payload = wire if detection == "line_crossing" else {"region_info": wire}
    assert caller.sent[-1] == (CAM, setter, {module: {section: payload}})
    assert result["changed"] == {"zones": wire}
    reg.refresh.assert_awaited_once()


@pytest.mark.parametrize("detection", area.ZONES)
async def test_zone_permission_and_device_error(make_house, detection):
    reg, caller, _ = house(make_house, tier=1)
    with pytest.raises(Forbidden):
        await area.zones_set(reg, caller, CAM, detection, [])
    reg, caller, _ = house(make_house)
    caller.respond(area.ZONES[detection][1], {"error_code": -1, "message": "SECRET"})
    with pytest.raises(DeviceUnavailable, match="reported an error"):
        await area.zones_set(reg, caller, CAM, detection, [])
    reg.refresh.assert_not_awaited()


@pytest.mark.parametrize("zone", [{}, {**ZONE_INPUTS["motion"], "password": "x"}, {**ZONE_INPUTS["motion"], "width": -1}, {**ZONE_INPUTS["motion"], "x_coor": 10000}, {**ZONE_INPUTS["motion"], "width": True}])
async def test_zone_validation_precedes_wire(make_house, zone):
    reg, caller, _ = house(make_house)
    with pytest.raises(BadRequest):
        await area.zones_set(reg, caller, CAM, "motion", [zone])
    assert caller.sent == []


@pytest.mark.parametrize("detection", ["line_crossing", "intrusion"])
async def test_arming_schedule_merges_untouched_days(make_house, detection):
    reg, caller, _ = house(make_house)
    result = await area.schedule_set(reg, caller, CAM, detection, {"monday": ["0800-1200", "1300-2400"], "sunday": []})
    patch = result["changed"]["schedule"]
    assert patch == {"monday": '["0800-1200","1300-2400"]', "sunday": "[]"}
    module, stem = area.DETECTIONS[detection][1], area.DETECTIONS[detection][3]
    assert caller.sent[-1] == (CAM, f"set{stem}Schedule", {module: {"arming_schedule": {day: patch.get(day, '["0000-2400"]') for day in area.DAYS}}})
    reg.refresh.assert_awaited_once()
    reg, caller, _ = house(make_house, tier=1)
    with pytest.raises(Forbidden):
        await area.schedule_set(reg, caller, CAM, detection, {"monday": []})
    reg, caller, _ = house(make_house)
    caller.respond(f"set{stem}Schedule", TimeoutError())
    with pytest.raises(DeviceUnavailable, match="did not answer"):
        await area.schedule_set(reg, caller, CAM, detection, {"monday": []})


@pytest.mark.parametrize("schedule", [{}, {"monday": ["2560-2600"]}, {"monday": ["1200-0800"]}, {"monday": ["0800-1300", "1200-1400"]}, {"monday": ["0800-1000:secret"]}, {"password": []}])
async def test_invalid_schedule_never_calls_wire(make_house, schedule):
    reg, caller, _ = house(make_house)
    with pytest.raises(BadRequest):
        await area.schedule_set(reg, caller, CAM, "intrusion", schedule)
    assert caller.sent == []


def alarm_responses(caller):
    caller.respond("getAlertConfig", {"msg_alarm": {
        "chn1_msg_alarm_info": {"enabled": "on", "alarm_mode": ["sound", "light"], "alarm_type": "0", "alarm_volume": "5", "alarm_duration": "60", "light_type": "flicker", "light_alarm_enabled": "on", "sound_alarm_enabled": "on", "light_rbl_enabled": "off", "password": "SECRET"},
        "capability": {"alarm_func": ["sound", "light"], "alarm_type": ["0", "1"], "alarm_volume_support": "on", "alarm_duration_support": "on", "red_blue_light_alarm_support": "on", "alarm_volume_range": "0-10", "alert_event_type_support": "on", "key": "SECRET"},
    }})
    caller.respond("getManualAlarmStatus", {"msg_alarm": {"get_manual_alarm_status": {"status": "off", "rest_time": "0", "token": "SECRET"}}})
    for method, module, section in [("getAlertEventType", "msg_alarm", "msg_alarm_type"), ("getMsgPushEventList", "msg_push", "msg_push_event")]:
        caller.respond(method, {module: {section: [{"name": "motion", "enabled": "on", **({"interruption_level": "general"} if module == "msg_push" else {}), "password": "SECRET"}, {"name": "person", "enabled": "off"}]}})
    caller.respond("getWhitelampCapability", {"image_capability": {"white_lamp": {"wtl_intensity_range": "1-100", "time_limit_max_sec": "300", "wtl_intensity_support": "on", "time_limit_support": "on"}}})
    caller.respond("getWhitelampConfig", {"image": {"switch": {"wtl_intensity_level": "50", "wtl_force_time": "60", "rtsp": "SECRET"}}})
    caller.respond("getFloodlightCapability", {"floodlight": {"capability": {"min_intensity": "1", "intensity_level_max": "100", "manual_duration_max": "300", "trigger_duration_max": "300"}}})
    caller.respond("getFloodlightConfig", {"floodlight": {"config": {"intensity_level": "50", "manual_duration": "60", "auto_off_enabled": "on", "trigger_duration": "20", "password": "SECRET"}}})


async def test_alarm_read_and_status_are_compact(make_house):
    reg, caller, _ = house(make_house, tier=0)
    alarm_responses(caller)
    read = await area.alarm_get(reg, caller, CAM)
    assert read["settings"]["alarm_mode"] == ["sound", "light"]
    assert read["capability"]["alarm_duration_support"] == "on"
    assert "SECRET" not in str(read)
    assert await area.manual_alarm(reg, caller, CAM) == {"device": CAM, "status": {"status": "off", "rest_time": "0"}}
    reg.refresh.assert_not_awaited()


@pytest.mark.parametrize("settings,patch", [
    ({"enabled": False}, {"enabled": "off"}),
    ({"alarm_mode": ["sound"]}, {"alarm_mode": ["sound"]}),
    ({"alarm_type": 1}, {"alarm_type": "1"}),
    ({"alarm_volume": 7}, {"alarm_volume": "7"}),
    ({"alarm_duration": 180}, {"alarm_duration": "180"}),
    ({"light_type": "lighting"}, {"light_type": "lighting"}),
    ({"sound_alarm_enabled": True, "light_alarm_enabled": False, "light_rbl_enabled": True}, {"sound_alarm_enabled": "on", "light_alarm_enabled": "off", "light_rbl_enabled": "on"}),
])
async def test_alarm_set_confirmation_and_exact_wire(make_house, settings, patch):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    for phrase in (None, "setAlertConfig another-device", "doManualAlarm hallway-cam"):
        with pytest.raises(NeedsConfirmation):
            await area.alarm_set(reg, caller, CAM, settings, phrase)
    result = await area.alarm_set(reg, caller, CAM, settings, f"setAlertConfig {CAM}")
    assert result == {"device": CAM, "changed": patch}
    assert caller.sent[-1] == (CAM, "setAlertConfig", {"msg_alarm": {"chn1_msg_alarm_info": patch}})
    assert caller.methods_sent().count("setAlertConfig") == 1
    reg.refresh.assert_awaited_once()


@pytest.mark.parametrize("action", ["start", "stop"])
async def test_manual_alarm_confirmation_and_wire(make_house, action):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    with pytest.raises(NeedsConfirmation):
        await area.manual_alarm(reg, caller, CAM, action)
    with pytest.raises(NeedsConfirmation):
        await area.manual_alarm(reg, caller, CAM, action, "doManualAlarm other")
    assert await area.manual_alarm(reg, caller, CAM, action, f"doManualAlarm {CAM}") == {"device": CAM, "changed": {"manual_alarm": action}}
    assert caller.sent[-1] == (CAM, "doManualAlarm", {"msg_alarm": {"manual_alarm": {"action": action}}})
    reg.refresh.assert_awaited_once()


@pytest.mark.parametrize("notifications", [False, True])
async def test_event_choices_preserve_others_and_hide_fields(make_house, notifications):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    result = await area.choices_get(reg, caller, CAM, notifications)
    assert "SECRET" not in str(result) and result["choice_count"] == 2
    method = "setMsgPushEventList" if notifications else "setAlertEventType"
    # Both decide whether the owner is warned, so both need the phrase that names them.
    for phrase in (None, f"setAlertConfig {CAM}", f"{method} other-device"):
        with pytest.raises(NeedsConfirmation):
            await area.choices_set(reg, caller, CAM, "motion", False, notifications=notifications, confirm=phrase)
    assert method not in caller.methods_sent()
    result = await area.choices_set(reg, caller, CAM, "motion", False, "critical" if notifications else None, notifications, f"{method} {CAM}")
    module, section = ("msg_push", "msg_push_event") if notifications else ("msg_alarm", "msg_alarm_type")
    expected = [{"name": "motion", "enabled": "off", **({"interruption_level": "critical"} if notifications else {})}, {"name": "person", "enabled": "off"}]
    assert caller.sent[-1] == (CAM, method, {module: {section: expected}})
    assert "SECRET" not in str(caller.sent[-1])
    reg.refresh.assert_awaited_once()


@pytest.mark.parametrize("notifications", [False, True])
async def test_unknown_choice_never_writes(make_house, notifications):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    with pytest.raises(BadRequest, match="does not report event choice"):
        await area.choices_set(reg, caller, CAM, "not_a_detection", True, notifications=notifications)
    assert not any(m.startswith("set") for m in caller.methods_sent())
    with pytest.raises(BadRequest):
        await area.choices_set(reg, caller, CAM, "motion", True, "secret", notifications)


@pytest.mark.parametrize("light", ["spotlight", "floodlight"])
async def test_light_read_write_and_switch(make_house, light):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    read = await area.lighting_get(reg, caller, CAM, light)
    assert read["settings"] and read["capability"] and "SECRET" not in str(read)
    patch = {"wtl_intensity_level": "70", "wtl_force_time": "120"} if light == "spotlight" else {"intensity_level": "70", "manual_duration": "120"}
    assert (await area.lighting_set(reg, caller, CAM, light, 70, 120))["changed"] == patch
    method, module, section = ("setWhitelampConfig", "image", "switch") if light == "spotlight" else ("setFloodlightConfig", "floodlight", "config")
    assert caller.sent[-1] == (CAM, method, {module: {section: patch}})
    switch_method = "setForceWhitelampState" if light == "spotlight" else "manualFloodlightOp"
    for enabled in (True, False):
        with pytest.raises(NeedsConfirmation):
            await area.lighting_switch(reg, caller, CAM, light, enabled)
        await area.lighting_switch(reg, caller, CAM, light, enabled, f"{switch_method} {CAM}")
        params = {"image": {"switch": {"force_wtl_state": "on" if enabled else "off"}}} if light == "spotlight" else {"floodlight": {"manual_floodlight_op": {"action": "start" if enabled else "stop"}}}
        assert caller.sent[-1] == (CAM, switch_method, params)
    assert reg.refresh.await_count == 3


# Exercise every declared alarm/lighting/choice write through real policy and canned device errors.
WRITE_ACTIONS = [
    ("setAlertConfig", lambda r, c: area.alarm_set(r, c, CAM, {"enabled": True}, f"setAlertConfig {CAM}")),
    ("setAlertEventType", lambda r, c: area.choices_set(r, c, CAM, "motion", False, confirm=f"setAlertEventType {CAM}")),
    ("doManualAlarm", lambda r, c: area.manual_alarm(r, c, CAM, "start", f"doManualAlarm {CAM}")),
    ("setWhitelampConfig", lambda r, c: area.lighting_set(r, c, CAM, "spotlight", 70)),
    ("setFloodlightConfig", lambda r, c: area.lighting_set(r, c, CAM, "floodlight", 70)),
    ("setForceWhitelampState", lambda r, c: area.lighting_switch(r, c, CAM, "spotlight", True, f"setForceWhitelampState {CAM}")),
    ("manualFloodlightOp", lambda r, c: area.lighting_switch(r, c, CAM, "floodlight", True, f"manualFloodlightOp {CAM}")),
    ("setMsgPushEventList", lambda r, c: area.choices_set(r, c, CAM, "motion", True, notifications=True, confirm=f"setMsgPushEventList {CAM}")),
]


@pytest.mark.parametrize("method,action", WRITE_ACTIONS)
async def test_each_action_level_and_device_failure(make_house, method, action):
    reg, caller, _ = house(make_house, tier=1)
    alarm_responses(caller)
    with pytest.raises(Forbidden):
        await action(reg, caller)
    assert method not in caller.methods_sent()
    reg.refresh.assert_not_awaited()
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    caller.respond(method, DeviceError("SECRET", error_code=None))
    with pytest.raises(DeviceUnavailable, match=f"{CAM} reported an error for {method}") as error:
        await action(reg, caller)
    assert "SECRET" not in str(error.value)
    reg.refresh.assert_not_awaited()


@pytest.mark.parametrize("settings", [{}, {"password": "SECRET"}, {"alarm_mode": ["sound", "sound"]}, {"alarm_mode": ["secret"]}, {"enabled": "on"}, {"alarm_type": "unknown"}, {"alarm_duration": 0}, {"alarm_duration": 10000}, {"alarm_volume": 11}, {"light_type": "laser"}])
async def test_bad_alarm_fields_and_values_never_write(make_house, settings):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    with pytest.raises(BadRequest):
        await area.alarm_set(reg, caller, CAM, settings, f"setAlertConfig {CAM}")
    assert "setAlertConfig" not in caller.methods_sent()


@pytest.mark.parametrize("light", ["spotlight", "floodlight"])
@pytest.mark.parametrize("brightness,duration", [(None, None), (101, None), (-1, None), (True, None), (0, None), (None, 301), (None, 0)])
async def test_light_ranges_checked_before_write(make_house, light, brightness, duration):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    with pytest.raises(BadRequest):
        await area.lighting_set(reg, caller, CAM, light, brightness, duration)
    assert not any(m.startswith("set") for m in caller.methods_sent())


async def test_last_alert_poll_without_sd_or_refresh(make_house):
    reg, caller, devices = house(make_house, tier=0)
    caller.respond("getLastAlarmInfo", {"system": {"last_alarm_info": {"last_alarm_time": "1700000000", "last_alarm_type": "motion", "token": "SECRET"}}})
    from ..fakes import FakeFeature, T, C
    devices["cam"].features["last_alert_timestamp"] = FakeFeature("last_alert_timestamp", T.Sensor, C.Info, datetime.fromtimestamp(1699999990, timezone.utc))
    devices["cam"].features["last_alert_type"] = FakeFeature("last_alert_type", T.Sensor, C.Info, "motion")
    assert await area.events_recent(reg, caller, CAM) == {"device": CAM, "last": {"timestamp": 1700000000, "type": "motion"}, "cached_last": {"timestamp": 1699999990, "type": "motion"}}
    assert caller.sent[-1] == (CAM, "getLastAlarmInfo", {"system": {"name": ["last_alarm_info"]}})
    assert "searchDetectionList" not in caller.methods_sent()
    reg.refresh.assert_not_awaited()


@pytest.mark.parametrize("value", ["", "0", None, "garbage", True, 99999999999999999999999999999])
async def test_no_last_alert_omits_empty_fields(make_house, value):
    reg, caller, _ = house(make_house, tier=0)
    caller.respond("getLastAlarmInfo", {"system": {"last_alarm_info": {"last_alarm_time": value, "last_alarm_type": "SECRET"}}})
    assert await area.events_recent(reg, caller, CAM) == {"device": CAM}


@pytest.mark.parametrize("code,label", area.EVENT_TYPES.items())
async def test_sd_event_codes_and_bounded_page(make_house, code, label):
    reg, caller, _ = house(make_house, tier=0)
    caller.respond("getLastAlarmInfo", {"system": {"last_alarm_info": {"last_alarm_time": "1700000000", "last_alarm_type": str(code)}}})
    caller.respond("searchDetectionList", {"playback": {"search_detection_list": [{"start_time": 1700000000, "end_time": 1700000060, "alarm_type": code, "file_id": "SECRET", "thumbnail": "SECRET"}], "to_be_continued": 1}})
    result = await area.events_recent(reg, caller, CAM, 1699999900, 1700000100, limit=5, start_index=10)
    assert result["last"] == {"timestamp": 1700000000, "code": code, "type": label}
    assert result["events"] == [{"timestamp": 1700000000, "end_time": 1700000060, "code": code, "type": label}]
    assert result["next_index"] == 11 and "SECRET" not in str(result)
    assert caller.sent[-1] == (CAM, "searchDetectionList", {"playback": {"search_detection_list": {"start_time": 1699999900, "end_time": 1700000100, "channel": 0, "start_index": 10, "end_index": 14}}})


@pytest.mark.parametrize("kwargs", [{"start_time": 1}, {"start_time": 2, "end_time": 1}, {"start_time": 1, "end_time": 86402}, {"limit": 0}, {"limit": 101}, {"start_index": -1}])
async def test_bad_event_windows_precede_wire(make_house, kwargs):
    reg, caller, _ = house(make_house)
    with pytest.raises(BadRequest):
        await area.events_recent(reg, caller, CAM, **kwargs)
    assert caller.sent == []


async def test_event_unknown_types_page_order_and_empty_output(make_house):
    reg, caller, _ = house(make_house)
    caller.respond("getLastAlarmInfo", {"system": {"last_alarm_info": {"last_alarm_time": "1700000000", "last_alarm_type": "SECRET"}}})
    caller.respond("searchDetectionList", {"playback": {"search_detection_list": [{"start_time": 1700000000, "alarm_type": 999}, {"start_time": 1700000001, "alarm_type": 2}, {"start_time": 1, "alarm_type": 3}], "to_be_continued": 0}})
    result = await area.events_recent(reg, caller, CAM, 1699999900, 1700000100)
    assert result["last"]["type"] == "unknown" and "SECRET" not in str(result)
    assert [e["timestamp"] for e in result["events"]] == [1700000001, 1700000000]
    assert result["events"][-1]["type"] == "unknown" and result["events"][-1]["code"] == 999
    caller.respond("searchDetectionList", {"playback": {"search_detection_list": []}})
    result = await area.events_recent(reg, caller, CAM, 1699999900, 1700000100)
    assert result["event_count"] == 0 and "events" not in result and "next_index" not in result


@pytest.mark.parametrize("method", ["getLastAlarmInfo", "searchDetectionList", "getAlertConfig", "getAlertEventType", "getManualAlarmStatus", "getMsgPushEventList", "getWhitelampCapability", "getWhitelampConfig", "getFloodlightCapability", "getFloodlightConfig", "getPersonDetectionCapability", "getBCDCapability"])
async def test_each_read_device_failure(make_house, method):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    caller.respond("getLastAlarmInfo", {"system": {"last_alarm_info": {}}})
    caller.respond(method, TimeoutError())
    actions = {
        "getLastAlarmInfo": lambda: area.events_recent(reg, caller, CAM),
        "searchDetectionList": lambda: area.events_recent(reg, caller, CAM, 1, 100),
        "getAlertConfig": lambda: area.alarm_get(reg, caller, CAM),
        "getAlertEventType": lambda: area.choices_get(reg, caller, CAM),
        "getManualAlarmStatus": lambda: area.manual_alarm(reg, caller, CAM),
        "getMsgPushEventList": lambda: area.choices_get(reg, caller, CAM, True),
        "getWhitelampCapability": lambda: area.lighting_get(reg, caller, CAM, "spotlight"),
        "getWhitelampConfig": lambda: area.lighting_get(reg, caller, CAM, "spotlight"),
        "getFloodlightCapability": lambda: area.lighting_get(reg, caller, CAM, "floodlight"),
        "getFloodlightConfig": lambda: area.lighting_get(reg, caller, CAM, "floodlight"),
        "getPersonDetectionCapability": lambda: area.detection_get(reg, caller, CAM, "person", True),
        "getBCDCapability": lambda: area.detection_get(reg, caller, CAM, "baby_cry", True),
    }
    with pytest.raises(DeviceUnavailable, match=f"{CAM} did not answer in time for {method}"):
        await actions[method]()


TOOL_CASES = [
    ("camera_detect_get", {"detection": "motion"}, "getDetectionConfig", 0),
    ("camera_detect_set", {"detection": "motion", "enabled": False}, "setDetectionConfig", 2),
    ("camera_detect_zones_get", {"detection": "motion"}, "getDetectionConfig", 0),
    ("camera_detect_zones_set", {"detection": "motion", "zones": []}, "addDetectionRegion", 2),
    ("camera_detect_schedule_set", {"detection": "intrusion", "schedule": {"monday": []}}, "setIntrusionDetectionSchedule", 2),
    ("camera_alarm_get", {}, "getAlertConfig", 0),
    ("camera_alarm_set", {"settings": {"enabled": True}, "confirm": f"setAlertConfig {CAM}"}, "setAlertConfig", 3),
    ("camera_alarm_triggers_get", {}, "getAlertEventType", 0),
    ("camera_alarm_triggers_set", {"event": "motion", "enabled": False, "confirm": f"setAlertEventType {CAM}"}, "setAlertEventType", 3),
    ("camera_alarm_status", {}, "getManualAlarmStatus", 0),
    ("camera_alarm_manual", {"action": "start", "confirm": f"doManualAlarm {CAM}"}, "doManualAlarm", 3),
    ("camera_alarm_light_get", {"light": "spotlight"}, "getWhitelampConfig", 0),
    ("camera_alarm_light_set", {"light": "spotlight", "brightness": 70}, "setWhitelampConfig", 2),
    ("camera_alarm_light_switch", {"light": "spotlight", "enabled": True, "confirm": f"setForceWhitelampState {CAM}"}, "setForceWhitelampState", 3),
    ("camera_events_recent", {}, "getLastAlarmInfo", 0),
    ("camera_detect_notifications_get", {}, "getMsgPushEventList", 0),
    ("camera_detect_notifications_set", {"event": "motion", "enabled": False, "confirm": f"setMsgPushEventList {CAM}"}, "setMsgPushEventList", 3),
]


@pytest.mark.parametrize("name,args,method,tier", TOOL_CASES)
async def test_mcp_tool_success_refusal_failure_and_hints(make_house, mcp, name, args, method, tier):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    caller.respond("getLastAlarmInfo", {"system": {"last_alarm_info": {}}})
    async with mcp(reg, caller) as client:
        listed = {tool.name: tool for tool in (await client.list_tools()).tools}
        tool = listed[name]
        assert tool.annotations.read_only_hint == (tier == 0)
        assert tool.annotations.destructive_hint == (None if tier == 0 else tier >= 3)
        assert tool.description.strip()
        result = await client.call_tool(name, {"device": CAM, **args})
        assert not result.is_error and result.structured_content["device"] == CAM
        assert "SECRET" not in str(result.structured_content)
        caller.respond(method, DeviceError("SECRET", error_code=None))
        failed = await client.call_tool(name, {"device": CAM, **args})
        assert failed.is_error and "reported an error" in failed.content[0].text
        assert "SECRET" not in failed.content[0].text
        if tier >= 3:
            unconfirmed = await client.call_tool(name, {"device": CAM, **{k: v for k, v in args.items() if k != "confirm"}})
            assert unconfirmed.is_error and "confirm=" in unconfirmed.content[0].text
    reg, caller, _ = house(make_house, tier=1)
    alarm_responses(caller)
    caller.respond("getLastAlarmInfo", {"system": {"last_alarm_info": {}}})
    async with mcp(reg, caller) as client:
        failed = await client.call_tool(name, {"device": "desk-lamp" if tier == 0 else CAM, **args})
    assert failed.is_error
    if tier:
        assert "level" in failed.content[0].text and "allow_methods" not in failed.content[0].text
        assert method not in caller.methods_sent()
    else:
        assert caller.sent == []


async def test_camera_components_errors_and_malformed_responses(make_house):
    reg, caller, _ = house(make_house)
    caller.respond("getAppComponentList", DeviceError("SECRET", error_code=None))
    with pytest.raises(DeviceUnavailable, match="reported an error for getAppComponentList"):
        await area.detection_get(reg, caller, CAM, "motion")
    caller.respond("getAppComponentList", {})
    with pytest.raises(DeviceUnavailable, match="did not report app_component"):
        await area.detection_get(reg, caller, CAM, "motion")
    caller.respond("getAppComponentList", {"app_component": {"app_component_list": [{"name": "detection", "version": 0}]}})
    with pytest.raises(BadRequest, match="does not support motion"):
        await area.detection_get(reg, caller, CAM, "motion")


async def test_alarm_missing_capability_flags_and_fields_refused(make_house):
    reg, caller, _ = house(make_house)
    for capability, settings in [
        ({"alarm_func": ["light"]}, {"sound_alarm_enabled": True}),
        ({"alarm_func": ["sound"]}, {"light_type": "flicker"}),
        ({"alarm_volume_support": "off"}, {"alarm_volume": 5}),
        ({"alarm_duration_support": "off"}, {"alarm_duration": 60}),
        ({"red_blue_light_alarm_support": "off"}, {"light_rbl_enabled": True}),
        ({"alarm_volume_support": "on"}, {"alarm_volume": 5}),
    ]:
        alarm_responses(caller)
        caller.responses[(None, "getAlertConfig")]["msg_alarm"]["capability"] = capability
        with pytest.raises(BadRequest):
            await area.alarm_set(reg, caller, CAM, settings, f"setAlertConfig {CAM}")
    assert "setAlertConfig" not in caller.methods_sent()


async def test_refused_light_and_notification_capabilities(make_house):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    caller.respond("getWhitelampCapability", {"image_capability": {"white_lamp": {"wtl_intensity_range": "1-100", "wtl_intensity_support": "off"}}})
    with pytest.raises(BadRequest):
        await area.lighting_set(reg, caller, CAM, "spotlight", 70)
    caller.respond("getFloodlightCapability", {"floodlight": {"capability": {}}})
    with pytest.raises(BadRequest):
        await area.lighting_set(reg, caller, CAM, "floodlight", 70)
    with pytest.raises(BadRequest, match="does not report notification interruption"):
        await area.choices_set(reg, caller, CAM, "person", True, "critical", True)
    assert not any(m.startswith("set") for m in caller.methods_sent())


@pytest.mark.parametrize("name,args,method,tier", TOOL_CASES)
async def test_all_tools_refuse_missing_model_components(make_house, name, args, method, tier):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    caller.respond("getAppComponentList", {"app_component": {"app_component_list": []}})
    # Exercise tool wrappers without opening even an in-process transport.
    from tolpo.areas import AreaContext
    from contextlib import nullcontext
    class Recorder:
        def __init__(self):
            self.functions = {}
        def tool(self, **kwargs):
            def record(fn):
                self.functions[fn.__name__] = fn
                return fn
            return record
    recorder = Recorder()
    ctx = AreaContext(registry=reg, caller=caller, server=recorder, ready_for=AsyncMock(), translated=nullcontext)
    area.tools(ctx)
    with pytest.raises(BadRequest, match="does not support"):
        await recorder.functions[name](device=CAM, **args)
    assert caller.methods_sent() == ["getAppComponentList"]


async def test_all_tools_documented(make_house, mcp):
    from pathlib import Path
    reg, caller, _ = house(make_house)
    async with mcp(reg, caller) as client:
        tools = (await client.list_tools()).tools
    doc = (Path(__file__).parents[2] / "docs/areas/camera_detection.md").read_text()
    ours = [tool.name for tool in tools if tool.name.startswith(("camera_detect_", "camera_alarm_", "camera_events_"))]
    assert set(ours) == {case[0] for case in TOOL_CASES}
    assert all(f"- `{name}`:" in doc for name in ours)


@pytest.mark.parametrize("detection", area.DETECTIONS)
async def test_detection_write_failure_does_not_refresh(make_house, detection):
    reg, caller, _ = house(make_house)
    method = f"set{area.DETECTIONS[detection][3]}Config"
    caller.respond(method, DeviceError("SECRET", error_code=None))
    with pytest.raises(DeviceUnavailable, match=f"{CAM} reported an error for {method}") as error:
        await area.detection_set(reg, caller, CAM, detection, enabled=False)
    assert "SECRET" not in str(error.value)
    reg.refresh.assert_not_awaited()


@pytest.mark.parametrize("detection", area.ZONES)
async def test_zone_read_failure(make_house, detection):
    reg, caller, _ = house(make_house)
    caller.respond(area.ZONES[detection][0], DeviceError("SECRET", error_code=None))
    with pytest.raises(DeviceUnavailable, match="reported an error"):
        await area.zones_get(reg, caller, CAM, detection)


@pytest.mark.parametrize("tool,parameters", [
    ("camera_detect_set", {"detection": "motion", "sensitivity": True}),
    ("camera_detect_set", {"detection": "motion", "enabled": "false"}),
    ("camera_events_recent", {"limit": True}),
    ("camera_events_recent", {"start_time": 1.1, "end_time": 100}),
    ("camera_alarm_light_set", {"light": "spotlight", "brightness": True}),
    ("camera_alarm_manual", {"action": "pause"}),
    ("camera_detect_notifications_set", {"event": "motion", "enabled": "false"}),
])
async def test_mcp_invalid_types_rejected_before_wire(make_house, mcp, tool, parameters):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    async with mcp(reg, caller) as client:
        result = await client.call_tool(tool, {"device": CAM, **parameters})
    assert result.is_error
    assert caller.sent == []


@pytest.mark.parametrize("value", [None, [], "SECRET", 1])
async def test_malformed_module_names_device_without_raw_data(make_house, value):
    reg, caller, _ = house(make_house)
    caller.respond("getDetectionConfig", {"motion_detection": value})
    with pytest.raises(DeviceUnavailable, match=f"{CAM} did not report motion_detection") as error:
        await area.detection_get(reg, caller, CAM, "motion")
    assert "SECRET" not in str(error.value)


async def test_missing_schedule_and_version_boolean_refused(make_house):
    reg, caller, _ = house(make_house)
    caller.respond("getIntrusionDetectionConfig", {"intrusion_detection": {"detection": {"enabled": "on"}}})
    with pytest.raises(BadRequest, match="complete arming schedule"):
        await area.schedule_set(reg, caller, CAM, "intrusion", {"monday": []})
    caller.respond("getAppComponentList", {"app_component": {"app_component_list": [{"name": "detection", "version": True}]}})
    with pytest.raises(BadRequest, match="does not support motion"):
        await area.detection_set(reg, caller, CAM, "motion", False)
    assert not any(method.startswith("set") for method in caller.methods_sent())


# Sanitized fixture shapes selected from C225, C465, C610 and D230. Event timestamps use fake values.
# Capability probes, zones other than the D230 motion rectangle, and write replies remain synthetic.
CAPTURED = json.loads(r'''
{
  "C225": {
    "getAlertConfig": {
      "msg_alarm": {
        "chn1_msg_alarm_info": {
          "alarm_duration": "0",
          "alarm_mode": [
            "light",
            "sound"
          ],
          "alarm_type": "0",
          "alarm_volume": "high",
          "enabled": "off",
          "light_alarm_enabled": "on",
          "light_type": "1",
          "sound_alarm_enabled": "on"
        }
      }
    },
    "getAppComponentList": {
      "app_component": {
        "app_component_list": [
          {
            "name": "sdCard",
            "version": 1
          },
          {
            "name": "timezone",
            "version": 1
          },
          {
            "name": "system",
            "version": 4
          },
          {
            "name": "led",
            "version": 1
          },
          {
            "name": "playback",
            "version": 6
          },
          {
            "name": "detection",
            "version": 3
          },
          {
            "name": "alert",
            "version": 2
          },
          {
            "name": "firmware",
            "version": 2
          },
          {
            "name": "account",
            "version": 2
          },
          {
            "name": "quickSetup",
            "version": 1
          },
          {
            "name": "ptz",
            "version": 1
          },
          {
            "name": "video",
            "version": 3
          },
          {
            "name": "lensMask",
            "version": 2
          },
          {
            "name": "lightFrequency",
            "version": 1
          },
          {
            "name": "dayNightMode",
            "version": 1
          },
          {
            "name": "osd",
            "version": 2
          },
          {
            "name": "record",
            "version": 1
          },
          {
            "name": "videoRotation",
            "version": 1
          },
          {
            "name": "audio",
            "version": 3
          },
          {
            "name": "audioCapability",
            "version": 1
          },
          {
            "name": "audioSourceCapability",
            "version": 1
          },
          {
            "name": "diagnose",
            "version": 2
          },
          {
            "name": "msgPush",
            "version": 3
          },
          {
            "name": "linecrossingDetection",
            "version": 2
          },
          {
            "name": "deviceShare",
            "version": 1
          },
          {
            "name": "tamperDetection",
            "version": 1
          },
          {
            "name": "tapoCare",
            "version": 1
          },
          {
            "name": "targetTrack",
            "version": 1
          },
          {
            "name": "blockZone",
            "version": 1
          },
          {
            "name": "babyCryDetection",
            "version": 1
          },
          {
            "name": "personDetection",
            "version": 2
          },
          {
            "name": "needSubscriptionServiceList",
            "version": 1
          },
          {
            "name": "patrol",
            "version": 1
          },
          {
            "name": "panoramicView",
            "version": 1
          },
          {
            "name": "vehicleDetection",
            "version": 1
          },
          {
            "name": "petDetection",
            "version": 1
          },
          {
            "name": "meowDetection",
            "version": 1
          },
          {
            "name": "barkDetection",
            "version": 1
          },
          {
            "name": "glassDetection",
            "version": 1
          },
          {
            "name": "alarmDetection",
            "version": 1
          },
          {
            "name": "infLamp",
            "version": 1
          },
          {
            "name": "markerBox",
            "version": 1
          },
          {
            "name": "nvmp",
            "version": 1
          },
          {
            "name": "iotCloud",
            "version": 1
          },
          {
            "name": "hdr",
            "version": 1
          },
          {
            "name": "homekit",
            "version": 1
          },
          {
            "name": "upnpc",
            "version": 2
          },
          {
            "name": "bleOnboarding",
            "version": 2
          },
          {
            "name": "recordDownload",
            "version": 2
          },
          {
            "name": "recordDownloadOccupy",
            "version": 1
          },
          {
            "name": "smartTrack",
            "version": 1
          },
          {
            "name": "encryption",
            "version": 3
          },
          {
            "name": "staticIp",
            "version": 2
          },
          {
            "name": "detectionRegion",
            "version": 2
          },
          {
            "name": "timeFormat",
            "version": 1
          },
          {
            "name": "snapshot",
            "version": 2
          },
          {
            "name": "relayPreConnection",
            "version": 1
          },
          {
            "name": "hubManage",
            "version": 1
          },
          {
            "name": "cipcV2Relay",
            "version": 1
          },
          {
            "name": "streamCapability",
            "version": 1
          },
          {
            "name": "playbackDelete",
            "version": 1
          }
        ]
      }
    },
    "getBCDConfig": {
      "sound_detection": {
        "bcd": {
          "digital_sensitivity": "50",
          "enabled": "off",
          "sensitivity": "medium"
        }
      }
    },
    "getBarkDetectionConfig": {
      "bark_detection": {
        "detection": {
          "enabled": "off",
          "sensitivity": "50"
        }
      }
    },
    "getDetectionConfig": {
      "motion_detection": {
        "motion_det": {
          "digital_sensitivity": "60",
          "enabled": "on",
          "non_vehicle_enabled": "off",
          "people_enabled": "off",
          "sensitivity": "medium",
          "vehicle_enabled": "off"
        }
      }
    },
    "getGlassDetectionConfig": {
      "glass_detection": {
        "detection": {
          "enabled": "off",
          "sensitivity": "50"
        }
      }
    },
    "getLastAlarmInfo": {
      "system": {
        "last_alarm_info": {
          "last_alarm_time": "1700000000",
          "last_alarm_type": "motion"
        }
      }
    },
    "getLinecrossingDetectionConfig": {
      "linecrossing_detection": {
        "detection": {
          "enabled": "off"
        }
      }
    },
    "getMeowDetectionConfig": {
      "meow_detection": {
        "detection": {
          "enabled": "off",
          "sensitivity": "50"
        }
      }
    },
    "getPersonDetectionConfig": {
      "people_detection": {
        "detection": {
          "enabled": "on",
          "sensitivity": "60"
        }
      }
    },
    "getPetDetectionConfig": {
      "pet_detection": {
        "detection": {
          "enabled": "off",
          "sensitivity": "60"
        }
      }
    },
    "getTamperDetectionConfig": {
      "tamper_detection": {
        "tamper_det": {
          "digital_sensitivity": "50",
          "enabled": "off",
          "sensitivity": "medium"
        }
      }
    },
    "getVehicleDetectionConfig": {
      "vehicle_detection": {
        "detection": {
          "enabled": "off",
          "sensitivity": "60"
        }
      }
    }
  },
  "C465": {
    "getAlertConfig": {
      "msg_alarm": {
        "chn1_msg_alarm_info": {
          "alarm_duration": "0",
          "alarm_mode": [
            "sound",
            "light"
          ],
          "alarm_type": "0",
          "alarm_volume": "high",
          "enabled": "off",
          "light_alarm_enabled": "on",
          "light_type": "1",
          "sound_alarm_enabled": "on"
        }
      }
    },
    "getAppComponentList": {
      "app_component": {
        "app_component_list": [
          {
            "name": "sdCard",
            "version": 1
          },
          {
            "name": "timezone",
            "version": 1
          },
          {
            "name": "system",
            "version": 4
          },
          {
            "name": "led",
            "version": 1
          },
          {
            "name": "playback",
            "version": 6
          },
          {
            "name": "detection",
            "version": 3
          },
          {
            "name": "alert",
            "version": 2
          },
          {
            "name": "firmware",
            "version": 2
          },
          {
            "name": "quickSetup",
            "version": 1
          },
          {
            "name": "video",
            "version": 3
          },
          {
            "name": "imageStyle",
            "version": 3
          },
          {
            "name": "lensMask",
            "version": 2
          },
          {
            "name": "lightFrequency",
            "version": 3
          },
          {
            "name": "dayNightMode",
            "version": 2
          },
          {
            "name": "osd",
            "version": 2
          },
          {
            "name": "record",
            "version": 1
          },
          {
            "name": "audio",
            "version": 3
          },
          {
            "name": "audioCapability",
            "version": 1
          },
          {
            "name": "diagnose",
            "version": 1
          },
          {
            "name": "msgPush",
            "version": 3
          },
          {
            "name": "deviceShare",
            "version": 1
          },
          {
            "name": "tapoCare",
            "version": 1
          },
          {
            "name": "nightVisionMode",
            "version": 3
          },
          {
            "name": "iotCloud",
            "version": 1
          },
          {
            "name": "needSubscriptionServiceList",
            "version": 1
          },
          {
            "name": "blockZone",
            "version": 1
          },
          {
            "name": "personDetection",
            "version": 2
          },
          {
            "name": "vehicleDetection",
            "version": 1
          },
          {
            "name": "petDetection",
            "version": 1
          },
          {
            "name": "infLamp",
            "version": 1
          },
          {
            "name": "whiteLamp",
            "version": 1
          },
          {
            "name": "pir",
            "version": 3
          },
          {
            "name": "battery",
            "version": 3
          },
          {
            "name": "clips",
            "version": 2
          },
          {
            "name": "markerBox",
            "version": 1
          },
          {
            "name": "streamGrab",
            "version": 1
          },
          {
            "name": "antiTheft",
            "version": 3
          },
          {
            "name": "nvmp",
            "version": 1
          },
          {
            "name": "detectionRegion",
            "version": 2
          },
          {
            "name": "ldc",
            "version": 1
          },
          {
            "name": "noHubBatteryCam",
            "version": 1
          },
          {
            "name": "recordDownload",
            "version": 2
          },
          {
            "name": "recordDownloadOccupy",
            "version": 1
          },
          {
            "name": "smartAutoExposure",
            "version": 1
          },
          {
            "name": "snapshot",
            "version": 2
          },
          {
            "name": "timeFormat",
            "version": 1
          },
          {
            "name": "aov",
            "version": 1
          },
          {
            "name": "hubManage",
            "version": 1
          },
          {
            "name": "wifiSwitch",
            "version": 1
          },
          {
            "name": "wifiDualBand",
            "version": 1
          },
          {
            "name": "bleOnboarding",
            "version": 2
          },
          {
            "name": "encryption",
            "version": 3
          },
          {
            "name": "aovSupportHub",
            "version": 1
          }
        ]
      }
    },
    "getDetectionConfig": {
      "motion_detection": {
        "motion_det": {
          "det_sensitivity": "70",
          "digital_sensitivity": "60",
          "enabled": "on",
          "non_vehicle_enabled": "off",
          "people_enabled": "off",
          "sensitivity": "medium",
          "vehicle_enabled": "off"
        }
      }
    },
    "getLastAlarmInfo": {
      "system": {
        "last_alarm_info": {
          "last_alarm_time": "1700000000",
          "last_alarm_type": "motion"
        }
      }
    },
    "getPersonDetectionConfig": {
      "people_detection": {
        "detection": {
          "enabled": "on",
          "sensitivity": "60"
        }
      }
    },
    "getPetDetectionConfig": {
      "pet_detection": {
        "detection": {
          "enabled": "off",
          "sensitivity": "60"
        }
      }
    },
    "getVehicleDetectionConfig": {
      "vehicle_detection": {
        "detection": {
          "enabled": "on",
          "sensitivity": "60"
        }
      }
    }
  },
  "C610": {
    "getAlertConfig": {
      "msg_alarm": {
        "chn1_msg_alarm_info": {
          "alarm_duration": "0",
          "alarm_mode": [
            "sound",
            "light"
          ],
          "alarm_type": "0",
          "alarm_volume": "high",
          "enabled": "off",
          "light_alarm_enabled": "on",
          "light_type": "1",
          "sound_alarm_enabled": "on"
        }
      }
    },
    "getAppComponentList": {
      "app_component": {
        "app_component_list": [
          {
            "name": "sdCard",
            "version": 1
          },
          {
            "name": "timezone",
            "version": 1
          },
          {
            "name": "system",
            "version": 4
          },
          {
            "name": "led",
            "version": 1
          },
          {
            "name": "playback",
            "version": 6
          },
          {
            "name": "detection",
            "version": 3
          },
          {
            "name": "alert",
            "version": 2
          },
          {
            "name": "firmware",
            "version": 2
          },
          {
            "name": "quickSetup",
            "version": 3
          },
          {
            "name": "video",
            "version": 3
          },
          {
            "name": "lensMask",
            "version": 2
          },
          {
            "name": "lightFrequency",
            "version": 3
          },
          {
            "name": "dayNightMode",
            "version": 2
          },
          {
            "name": "osd",
            "version": 2
          },
          {
            "name": "record",
            "version": 1
          },
          {
            "name": "audio",
            "version": 3
          },
          {
            "name": "audioCapability",
            "version": 1
          },
          {
            "name": "diagnose",
            "version": 1
          },
          {
            "name": "msgPush",
            "version": 3
          },
          {
            "name": "deviceShare",
            "version": 1
          },
          {
            "name": "tapoCare",
            "version": 1
          },
          {
            "name": "nightVisionMode",
            "version": 3
          },
          {
            "name": "iotCloud",
            "version": 1
          },
          {
            "name": "needSubscriptionServiceList",
            "version": 1
          },
          {
            "name": "blockZone",
            "version": 1
          },
          {
            "name": "personDetection",
            "version": 2
          },
          {
            "name": "infLamp",
            "version": 1
          },
          {
            "name": "whiteLamp",
            "version": 1
          },
          {
            "name": "pir",
            "version": 3
          },
          {
            "name": "battery",
            "version": 3
          },
          {
            "name": "clips",
            "version": 2
          },
          {
            "name": "streamGrab",
            "version": 1
          },
          {
            "name": "nvmp",
            "version": 1
          },
          {
            "name": "detectionRegion",
            "version": 2
          },
          {
            "name": "noHubBatteryCam",
            "version": 1
          },
          {
            "name": "recordDownload",
            "version": 2
          },
          {
            "name": "recordDownloadOccupy",
            "version": 1
          },
          {
            "name": "snapshot",
            "version": 2
          },
          {
            "name": "ptz",
            "version": 1
          },
          {
            "name": "patrol",
            "version": 1
          },
          {
            "name": "targetTrack",
            "version": 1
          },
          {
            "name": "bleOnboarding",
            "version": 2
          },
          {
            "name": "encryption",
            "version": 3
          },
          {
            "name": "hubManage",
            "version": 1
          },
          {
            "name": "panoramicVideo",
            "version": 1
          },
          {
            "name": "imageStyle",
            "version": 3
          },
          {
            "name": "audioSourceCapability",
            "version": 1
          },
          {
            "name": "cipcV2Relay",
            "version": 1
          },
          {
            "name": "facTest",
            "version": 1
          },
          {
            "name": "fileRelay",
            "version": 1
          },
          {
            "name": "srtpWebrtc",
            "version": 1
          }
        ]
      }
    },
    "getDetectionConfig": {
      "motion_detection": {
        "motion_det": {
          "det_sensitivity": "70",
          "digital_sensitivity": "60",
          "enabled": "on",
          "non_vehicle_enabled": "off",
          "people_enabled": "off",
          "sensitivity": "medium",
          "vehicle_enabled": "off"
        }
      }
    },
    "getLastAlarmInfo": {
      "system": {
        "last_alarm_info": {
          "last_alarm_time": "0",
          "last_alarm_type": ""
        }
      }
    },
    "getPersonDetectionConfig": {
      "people_detection": {
        "detection": {
          "enabled": "on",
          "sensitivity": "60"
        }
      }
    }
  },
  "D230": {
    "getDetectionConfig": {
      "motion_detection": {
        "motion_det": {
          "digital_sensitivity": "60",
          "enabled": "on",
          "sensitivity": "medium"
        },
        "region_info": [
          {
            "region_info_1": {
              "height": "10000",
              "width": "10000",
              "x_coor": "0",
              "y_coor": "0"
            }
          }
        ]
      }
    },
    "getPersonDetectionConfig": {
      "people_detection": {
        "detection": {
          "enabled": "on",
          "sensitivity": "60"
        }
      }
    },
    "getPetDetectionConfig": {
      "pet_detection": {
        "detection": {
          "enabled": "off",
          "sensitivity": "60"
        }
      }
    },
    "getVehicleDetectionConfig": {
      "vehicle_detection": {
        "detection": {
          "enabled": "off",
          "sensitivity": "60"
        }
      }
    }
  }
}
''')


@pytest.mark.parametrize("model", ["C225", "C465", "C610"])
async def test_captured_camera_shapes_do_not_require_unverified_probes(make_house, model):
    reg, caller, _ = house(make_house)
    captured = CAPTURED[model]
    for method, response in captured.items():
        caller.respond(method, response)
    caller.respond("getPersonDetectionCapability", DeviceError("not implemented", error_code=None))
    caller.respond("getBCDCapability", DeviceError("not implemented", error_code=None))
    for detection, (_, module, section, stem, _) in area.DETECTIONS.items():
        if f"get{stem}Config" not in captured:
            continue
        result = await area.detection_get(reg, caller, CAM, detection)
        expected = captured[f"get{stem}Config"][module][section]
        assert result["settings"] == expected
        assert "capability" not in result
        assert (await area.detection_set(reg, caller, CAM, detection, enabled=False))["changed"] == {"enabled": "off"}
    assert not {"getPersonDetectionCapability", "getBCDCapability"} & set(caller.methods_sent())
    alarm = await area.alarm_get(reg, caller, CAM)
    assert alarm["settings"] == captured["getAlertConfig"]["msg_alarm"]["chn1_msg_alarm_info"]
    assert alarm["settings"]["alarm_volume"] == "high"
    assert alarm["settings"]["light_type"] == "1"
    assert "capability" not in alarm
    with pytest.raises(BadRequest):
        await area.alarm_set(reg, caller, CAM, {"alarm_volume": "low"}, f"setAlertConfig {CAM}")
    with pytest.raises(BadRequest):
        await area.detection_set(reg, caller, CAM, "motion", sensitivity=75)
    assert "setDetectionConfig" in caller.methods_sent()
    last = await area.events_recent(reg, caller, CAM)
    if model == "C610":
        assert last == {"device": CAM}
    else:
        assert last == {"device": CAM, "last": {"timestamp": 1700000000, "type": "motion"}}
    with pytest.raises(BadRequest, match="does not support manual alarm"):
        await area.manual_alarm(reg, caller, CAM, "start", f"doManualAlarm {CAM}")


async def test_captured_doorbell_config_shapes_and_motion_table(make_house):
    reg, caller, _ = house(make_house, kind="doorbell")
    # D230 fixture has no component read, so the component response here remains synthetic.
    for method, response in CAPTURED["D230"].items():
        caller.respond(method, response)
    caller.respond("getPersonDetectionCapability", DeviceError("not implemented", error_code=None))
    for detection in ("motion", "person", "pet", "vehicle"):
        read = await area.detection_get(reg, caller, CAM, detection)
        assert read["settings"]["enabled"] in ("on", "off")
        assert "capability" not in read
        await area.detection_set(reg, caller, CAM, detection, enabled=False)
    assert await area.zones_get(reg, caller, CAM, "motion") == {
        "device": CAM, "detection": "motion", "zone_count": 1,
        "zones": [{"height": "10000", "width": "10000", "x_coor": "0", "y_coor": "0"}],
    }


async def test_captured_baby_cry_numeric_sensitivity_uses_digital_field(make_house):
    reg, caller, _ = house(make_house)
    caller.respond("getBCDConfig", CAPTURED["C225"]["getBCDConfig"])
    # The capability probe is synthetic. No captured camera advertised a range in its config.
    await area.detection_set(reg, caller, CAM, "baby_cry", sensitivity=75)
    assert caller.sent[-1] == (CAM, "setBCDConfig", {"sound_detection": {"bcd": {"digital_sensitivity": "75"}}})


async def test_numeric_legacy_alarm_light_type_does_not_get_label_guessed(make_house):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    caller.responses[(None, "getAlertConfig")]["msg_alarm"]["chn1_msg_alarm_info"]["light_type"] = "1"
    with pytest.raises(BadRequest, match="reported range"):
        await area.alarm_set(reg, caller, CAM, {"light_type": "lighting"}, f"setAlertConfig {CAM}")
    assert "setAlertConfig" not in caller.methods_sent()


async def test_cached_python_kasa_strenum_alert_type_is_preserved(make_house):
    from kasa.smartcam.modules.lastalertdetection import LastAlertType
    from ..fakes import FakeFeature, T, C
    reg, caller, devices = house(make_house)
    caller.respond("getLastAlarmInfo", {"system": {"last_alarm_info": {}}})
    devices["cam"].features["last_alert_timestamp"] = FakeFeature("last_alert_timestamp", T.Sensor, C.Info, datetime.fromtimestamp(1700000000, timezone.utc))
    devices["cam"].features["last_alert_type"] = FakeFeature("last_alert_type", T.Sensor, C.Info, LastAlertType.Motion)
    assert (await area.events_recent(reg, caller, CAM))["cached_last"] == {"timestamp": 1700000000, "type": "motion"}


@pytest.mark.parametrize("reported", [[0, 50, 100], {"min": 0, "max": 100}])
async def test_reported_range_shapes_are_visible_without_secret_fields(make_house, reported):
    reg, caller, _ = house(make_house)
    incoming = {**reported, "password": "SECRET"} if isinstance(reported, dict) else reported
    caller.respond("getDetectionConfig", {"motion_detection": {"motion_det": {"enabled": "on", "digital_sensitivity": "50"}, "capability": {"sensitivity_range": incoming}}})
    result = await area.detection_get(reg, caller, CAM, "motion")
    assert result["capability"] == {"sensitivity_range": reported}
    assert "SECRET" not in str(result)
    await area.detection_set(reg, caller, CAM, "motion", sensitivity=100)
    assert caller.sent[-1][2] == {"motion_detection": {"motion_det": {"digital_sensitivity": "100"}}}


LEVELS = {
    0: {"getDetectionConfig", "getPersonDetectionConfig", "getLinecrossingDetectionRegion", "getAlertConfig", "getAlertEventType",
        "getManualAlarmStatus", "getWhitelampConfig", "getFloodlightConfig", "getLastAlarmInfo", "searchDetectionList", "getMsgPushEventList"},
    2: {"setDetectionConfig", "setTamperDetectionConfig", "setBCDConfig", "addDetectionRegion", "addIntrusionDetectionRegion",
        "setIntrusionDetectionSchedule", "setLinecrossingDetectionSchedule", "setWhitelampConfig", "setFloodlightConfig"},
    3: {"setAlertConfig", "setAlertEventType", "setMsgPushEventList", "doManualAlarm", "manualFloodlightOp", "setForceWhitelampState"},
}


def test_declared_levels_match_real_effect():
    declared = {rule.method: int(rule.tier) for rule in area.METHODS}
    for level, methods in LEVELS.items():
        for method in methods:
            assert declared[method] == level, method
    # Anything that sounds, flashes or decides whether the owner is warned is level 3.
    assert {m for m, t in declared.items() if t >= 3} == LEVELS[3]
    assert all(rule.kinds == frozenset(area.KINDS) for rule in area.METHODS)


SECURITY_ACTIONS = [
    ("setAlertConfig", lambda r, c, p: area.alarm_set(r, c, CAM, {"enabled": True}, p)),
    ("setAlertEventType", lambda r, c, p: area.choices_set(r, c, CAM, "motion", False, confirm=p)),
    ("setMsgPushEventList", lambda r, c, p: area.choices_set(r, c, CAM, "motion", False, notifications=True, confirm=p)),
    ("doManualAlarm", lambda r, c, p: area.manual_alarm(r, c, CAM, "stop", p)),
    ("setForceWhitelampState", lambda r, c, p: area.lighting_switch(r, c, CAM, "spotlight", False, p)),
    ("manualFloodlightOp", lambda r, c, p: area.lighting_switch(r, c, CAM, "floodlight", False, p)),
]


@pytest.mark.parametrize("method,action", SECURITY_ACTIONS)
async def test_level_two_owner_cannot_run_security_actions_even_with_phrase(make_house, method, action):
    reg, caller, _ = house(make_house, tier=2)
    alarm_responses(caller)
    with pytest.raises(Forbidden, match="level 3") as error:
        await action(reg, caller, f"{method} {CAM}")
    assert "allow_methods" not in str(error.value) and "tier" not in str(error.value)
    assert method not in caller.methods_sent()
    reg.refresh.assert_not_awaited()


@pytest.mark.parametrize("raw", [10 ** 40, -2, "9" * 60, " 7_0 ", "1e3"])
async def test_event_codes_from_device_stay_small(make_house, raw):
    reg, caller, _ = house(make_house, tier=0)
    caller.respond("getLastAlarmInfo", {"system": {"last_alarm_info": {"last_alarm_time": "1700000000", "last_alarm_type": raw}}})
    result = await area.events_recent(reg, caller, CAM)
    assert result["last"]["timestamp"] == 1700000000
    assert len(str(result)) < 120
    assert result["last"].get("code", 0) <= 9999 and result["last"]["type"] in set(area.EVENT_TYPES.values()) | {"unknown"}


@pytest.mark.parametrize("flag,settings", [
    ("alarm_volume_support", {"alarm_volume": 5}),
    ("alarm_duration_support", {"alarm_duration": 60}),
    ("red_blue_light_alarm_support", {"light_rbl_enabled": True}),
])
async def test_alarm_support_flag_off_refuses_even_with_a_range(make_house, flag, settings):
    reg, caller, _ = house(make_house)
    alarm_responses(caller)
    # Everything else is reported, including alarm_volume_range, so only the flag can refuse.
    caller.responses[(None, "getAlertConfig")]["msg_alarm"]["capability"][flag] = "off"
    with pytest.raises(BadRequest, match="does not support"):
        await area.alarm_set(reg, caller, CAM, settings, f"setAlertConfig {CAM}")
    assert "setAlertConfig" not in caller.methods_sent()
