"""Camera settings tests use the real policy and a canned wire. No hardware."""

from unittest.mock import AsyncMock

import pytest

from tolpo.areas import camera_system as area
from tolpo.registry import BadRequest, DeviceUnavailable, Forbidden

pytestmark = pytest.mark.anyio
COMPONENTS = ["ptz", "patrol", "targetTrack"]


def house(make_house, tier=4, components=COMPONENTS):
    reg, caller, _ = make_house(tier=tier)
    reg.refresh = AsyncMock()
    caller.respond("getAppComponentList", {"app_component": {"app_component_list": [{"name": c, "version": 1} for c in components]}})
    caller.respond("getPresetConfig", {"preset": {"preset": {"id": ["1"], "name": ["Desk\n View"], "password": "SECRET", "ssid": "SECRET"}}})
    return reg, caller


PTZ = [
    ("stop", {}, "stopMove", {"motor": {"stop": "null"}}, 1),
    ("cruise", {"axis": "horizontal"}, "cruiseMove", {"motor": {"cruise": {"coord": "x"}}}, 1),
    ("cruise", {"axis": "stop"}, "cruiseStop", {"motor": {"cruise_stop": "null"}}, 1),
    ("calibrate", {"position": "saved"}, "manualCalibrate", {"motor": {"manual_cali": {"cal_to_resume": "1"}}}, 1),
    ("save", {"name": "Desk"}, "addMotorPostion", {"preset": {"set_preset": {"name": "Desk", "save_ptz": "1"}}}, 2),
    ("remove", {"ids": [1]}, "deletePreset", {"preset": {"remove_preset": {"id": ["1"]}}}, 2),
    ("patrol", {"enabled": True}, "setPatrolStatus", {"patrol": {"set_patrol_status": {"value": "on"}}}, 2),
    ("add", {"preset_id": 1, "name": "Desk", "wait_seconds": 30}, "addPatrol", {"patrol": {"add_patrol": {"id": "1", "name": "Desk", "wait_time": "30"}}}, 2),
    ("edit", {"preset_id": 1, "name": "Desk", "wait_seconds": 30}, "editPatrol", {"patrol": {"edit_patrol": {"id": "1", "name": "Desk", "wait_time": "30"}}}, 2),
    ("delete", {"preset_id": 1}, "deletePatrol", {"patrol": {"delete_patrol": {"id": "1"}}}, 2),
    ("tracking", {"enabled": False}, "setTargetTrackConfig", {"target_track": {"target_track_info": {"enabled": "off"}}}, 2),
]


@pytest.mark.parametrize("action,values,method,params,tier", PTZ)
async def test_ptz_writes(make_house, action, values, method, params, tier):
    reg, caller = house(make_house, tier)
    caller.respond(method, {"password": "SECRET", "empty": []})
    result = await area.ptz_action(reg, caller, "hallway-cam", action, **values)
    assert caller.sent[-1] == ("hallway-cam", method, params)
    assert result["device"] == "hallway-cam" and "changed" in result and "SECRET" not in str(result)
    reg.refresh.assert_awaited_once_with(reg.resolve_one("hallway-cam"), force=True)


@pytest.mark.parametrize("action,values,method,params,tier", PTZ)
async def test_ptz_policy_and_errors(make_house, action, values, method, params, tier):
    reg, caller = house(make_house, tier - 1)
    with pytest.raises(Forbidden, match=f"level {tier}"):
        await area.ptz_action(reg, caller, "hallway-cam", action, **values)
    assert method not in caller.methods_sent()
    reg.refresh.assert_not_awaited()
    reg, caller = house(make_house)
    caller.respond(method, {"error_code": -1, "message": "SECRET"})
    with pytest.raises(DeviceUnavailable, match=method) as error:
        await area.ptz_action(reg, caller, "hallway-cam", action, **values)
    assert "SECRET" not in str(error.value)
    reg.refresh.assert_not_awaited()


async def test_capability_refusal_and_kind(make_house):
    reg, caller = house(make_house, components=[])
    with pytest.raises(BadRequest, match="support for ptz"):
        await area.ptz_action(reg, caller, "hallway-cam", "cruise", axis="horizontal")
    assert caller.methods_sent() == ["getAppComponentList"]
    caller.sent.clear()
    for action, values in [("stop", {}), ("cruise", {"axis": "stop"})]:
        with pytest.raises(BadRequest, match="not a camera"):
            await area.ptz_action(reg, caller, "desk-lamp", action, **values)
    assert caller.sent == []


async def test_stop_is_sent_even_when_capability_reads_fail(make_house):
    """A stop must not depend on the component list: a timeout or an empty list still lets it through."""
    for components in [TimeoutError(), {"app_component": {"app_component_list": []}}, {"error_code": -1}]:
        reg, caller = house(make_house, tier=1)
        caller.respond("getAppComponentList", components)
        result = await area.ptz_stop(reg, caller, "hallway-cam")
        assert caller.methods_sent() == ["stopMove"] and result["changed"] == "movement stopped"
    reg, caller = house(make_house, tier=0)
    with pytest.raises(Forbidden, match="level 1"):
        await area.ptz_stop(reg, caller, "hallway-cam")
    assert caller.sent == []


async def test_compact_presets(make_house):
    reg, caller = house(make_house, tier=0)
    assert await area.read_setting(reg, caller, "hallway-cam", "presets") == {"device": "hallway-cam", "presets": {"id": ["1"], "name": ["Desk View"]}}


@pytest.mark.parametrize("action,values", [("save", {"name": "bad\nname"}), ("save", {"name": "a" * 65}), ("remove", {"ids": [2]}), ("remove", {"ids": []}), ("cruise", {"axis": "diagonal"}), ("calibrate", {"position": "other"}), ("patrol", {"enabled": "off"}), ("add", {"preset_id": 1, "name": "Desk", "wait_seconds": -1}), ("edit", {"preset_id": 1, "name": None, "wait_seconds": 30}), ("add", {"preset_id": 1, "name": "Desk", "wait_seconds": None})])
async def test_ptz_invalid_inputs_never_write(make_house, action, values):
    reg, caller = house(make_house)
    with pytest.raises(BadRequest):
        await area.ptz_action(reg, caller, "hallway-cam", action, **values)
    assert not any(m in area.WRITES for m in caller.methods_sent())
    reg.refresh.assert_not_awaited()

IMAGE_COMPONENTS = [*COMPONENTS, "video", "nightVisionMode", "videoRotation", "imageStyle", "osd", "led", "lensMask", "blockZone"]


def image_house(make_house, tier=4):
    reg, caller = house(make_house, tier, IMAGE_COMPONENTS)
    for method, module, section, value in [
        ("getNightVisionCapability", "image_capability", "supplement_lamp", {"night_vision_mode_range": ["inf_night_vision", "wtl_night_vision"]}),
        ("getImageCapability", "image_capability", "flip", {"flip_type": ["off", "center", "left_and_right", "up_and_down"]}),
        ("getImageStyleCapability", "image_style_capability", "image", {"image_style_range": ["standard", "bright"]}),
        ("getVideoCapability", "video_capability", "main", {"hdrs": ["0", "1"]}),
    ]:
        caller.respond(method, {module: {section: value}})
    caller.respond("getOsdCapability", {"osd_capability": {"date_default": {"x_coor": "10"}, "logo_default": {"x_coor": "20"}, "label_info_default": {"x_coor": "30"}}})
    caller.respond("getOsd", {"OSD": {"date": {"x_coor": "1", "y_coor": "2", "enabled": "off", "is_hour12": "off", "ssid": "SECRET"}, "logo": {"x_coor": "3", "y_coor": "4"}, "label_info": [{"label_info_1": {"x_coor": "5", "y_coor": "6", "text": "Old"}}]}})
    return reg, caller


IMAGE = [
    ("night_vision", "wtl_night_vision", "setNightVisionModeConfig", {"image": {"switch": {"night_vision_mode": "wtl_night_vision"}}}),
    ("flip", "center", "setRotationStatus", {"image": {"switch": {"flip_type": "center"}}}),
    ("style", "bright", "setImageStyleConfig", {"image": {"common": {"style": "bright"}}}),
    ("hdr", True, "setHDR", {"video": {"set_hdr": {"secname": "main", "hdr": 1}}}),
]


@pytest.mark.parametrize("setting,value,method,params", IMAGE)
async def test_image_wire_and_policy(make_house, setting, value, method, params):
    reg, caller = image_house(make_house, 2)
    assert (await area.image_change(reg, caller, "hallway-cam", setting, value))["changed"] == {setting: value}
    assert caller.sent[-1] == ("hallway-cam", method, params)
    reg.refresh.assert_awaited_once()
    reg, caller = image_house(make_house, 1)
    with pytest.raises(Forbidden, match="level 2"):
        await area.image_change(reg, caller, "hallway-cam", setting, value)
    assert method not in caller.methods_sent()
    reg, caller = image_house(make_house)
    caller.respond(method, {"error_code": -1})
    with pytest.raises(DeviceUnavailable, match=method):
        await area.image_change(reg, caller, "hallway-cam", setting, value)
    reg.refresh.assert_not_awaited()


@pytest.mark.parametrize("setting,value", [("night_vision", "unknown"), ("flip", "unknown"), ("style", "unknown"), ("hdr", "on"), ("led", 1)])
async def test_image_range_validation(make_house, setting, value):
    reg, caller = image_house(make_house)
    with pytest.raises(BadRequest):
        await area.image_change(reg, caller, "hallway-cam", setting, value)
    assert not any(m in area.WRITES for m in caller.methods_sent())


@pytest.mark.parametrize("element,text,section,expected", [
    ("time", None, "date", {"x_coor": "1", "y_coor": "2", "is_hour12": "off", "enabled": "on"}),
    ("name", "Desk", "label_info_1", {"x_coor": "5", "y_coor": "6", "text": "Desk", "enabled": "on"}),
    ("logo", None, "logo", {"x_coor": "3", "y_coor": "4", "enabled": "on"}),
])
async def test_osd_preserves_position_and_format(make_house, element, text, section, expected):
    reg, caller = image_house(make_house, 2)
    await area.osd_change(reg, caller, "hallway-cam", element, True, text)
    assert caller.sent[-1] == ("hallway-cam", "setOsd", {"OSD": {section: expected}})
    reg.refresh.assert_awaited_once()


async def test_osd_errors_policy_and_validation(make_house):
    for tier, response, error in [(1, {}, Forbidden), (2, {"error_code": -1}, DeviceUnavailable)]:
        reg, caller = image_house(make_house, tier)
        caller.respond("setOsd", response)
        with pytest.raises(error):
            await area.osd_change(reg, caller, "hallway-cam", "time", True)
        reg.refresh.assert_not_awaited()
    reg, caller = image_house(make_house)
    for element, text in [("name", "bad\ntext"), ("logo", "name"), ("other", None)]:
        with pytest.raises(BadRequest):
            await area.osd_change(reg, caller, "hallway-cam", element, True, text)
    assert "setOsd" not in caller.methods_sent()


@pytest.mark.parametrize("setting,method,module,section", [("led", "setLedStatus", "led", "config"), ("privacy_zones", "setCoverConfig", "cover", "cover")])
@pytest.mark.parametrize("enabled", [True, False])
async def test_led_and_privacy_zones_are_level_3_with_confirmation(make_house, setting, method, module, section, enabled):
    """A dark LED hides recording and disabled zones show blacked-out areas, so both directions are level 3."""
    from tolpo.calls import NeedsConfirmation
    phrase = f"{method} hallway-cam"
    reg, caller = image_house(make_house, 2)
    with pytest.raises(Forbidden, match="level 3"):
        await area.image_change(reg, caller, "hallway-cam", setting, enabled, phrase)
    assert method not in caller.methods_sent()
    reg, caller = image_house(make_house, 3)
    for invalid in [None, f"{method} other"]:
        with pytest.raises(NeedsConfirmation, match=phrase):
            await area.image_change(reg, caller, "hallway-cam", setting, enabled, invalid)
    assert method not in caller.methods_sent()
    assert (await area.image_change(reg, caller, "hallway-cam", setting, enabled, phrase))["changed"] == {setting: enabled}
    assert caller.sent[-1] == ("hallway-cam", method, {module: {section: {"enabled": "on" if enabled else "off"}}})
    reg.refresh.assert_awaited_once()
    caller.respond(method, {"error_code": -1})
    with pytest.raises(DeviceUnavailable, match=method):
        await area.image_change(reg, caller, "hallway-cam", setting, enabled, phrase)


async def test_privacy_both_directions_require_security_confirmation(make_house):
    from tolpo.calls import NeedsConfirmation
    for enabled in [True, False]:
        reg, caller = image_house(make_house, 2)
        with pytest.raises(Forbidden, match="level 3"):
            await area.image_change(reg, caller, "hallway-cam", "privacy", enabled, "setLensMaskConfig hallway-cam")
        reg, caller = image_house(make_house, 3)
        for phrase in [None, "setLensMaskConfig other"]:
            with pytest.raises(NeedsConfirmation):
                await area.image_change(reg, caller, "hallway-cam", "privacy", enabled, phrase)
        assert "setLensMaskConfig" not in caller.methods_sent()
        await area.image_change(reg, caller, "hallway-cam", "privacy", enabled, "setLensMaskConfig hallway-cam")
        assert caller.sent[-1][2] == {"lens_mask": {"lens_mask_info": {"enabled": "on" if enabled else "off"}}}
        reg.refresh.assert_awaited_once()
        caller.respond("setLensMaskConfig", {"error_code": -1})
        with pytest.raises(DeviceUnavailable):
            await area.image_change(reg, caller, "hallway-cam", "privacy", enabled, "setLensMaskConfig hallway-cam")

BATTERY_COMPONENTS = [*IMAGE_COMPONENTS, "battery", "batCamStatistics", "clips", "wakeUp", "pir"]


def battery_house(make_house, tier=4):
    reg, caller = image_house(make_house, tier)
    caller.respond("getAppComponentList", {"app_component": {"app_component_list": [{"name": c, "version": 3 if c == "battery" else 2 if c in {"clips", "wakeUp", "pir"} else 1} for c in BATTERY_COMPONENTS]}})
    caller.respond("getBatteryCapability", {"battery": {"capability": {"battery_power_mode_range": ["battery", "solar"], "battery_operating_mode_range": ["balanced", "performance"], "charging_privacy_mode_support": "1"}}})
    return reg, caller


BATTERY = [
    ("power_mode", "solar", "setPowerMode", {"battery": {"power": {"mode": "solar"}}}),
    ("working_mode", "balanced", "setBatteryOperatingMode", {"battery": {"operating": {"mode": "balanced", "follow_config": True}}}),
    ("wake_up", "doorbell", "setWakeUpConfig", {"wake_up": {"config": {"wake_up_type": "doorbell"}}}),
]


@pytest.mark.parametrize("setting,value,method,params", BATTERY)
async def test_battery_modes_wire_policy_errors(make_house, setting, value, method, params):
    reg, caller = battery_house(make_house, 2)
    await area.battery_change(reg, caller, "hallway-cam", setting, value)
    assert caller.sent[-1] == ("hallway-cam", method, params)
    reg.refresh.assert_awaited_once()
    reg, caller = battery_house(make_house, 1)
    with pytest.raises(Forbidden):
        await area.battery_change(reg, caller, "hallway-cam", setting, value)
    assert method not in caller.methods_sent()
    reg, caller = battery_house(make_house)
    caller.respond(method, {"error_code": -1})
    with pytest.raises(DeviceUnavailable):
        await area.battery_change(reg, caller, "hallway-cam", setting, value)
    reg.refresh.assert_not_awaited()


async def test_clips_wire_policy_error_validation(make_house):
    reg, caller = battery_house(make_house, 2)
    await area.clips_change(reg, caller, "hallway-cam", 30, 5, 10)
    assert caller.sent[-1] == ("hallway-cam", "setClipsConfig", {"clips": {"config": {"clips_length": 30, "record_buffer": 5, "retrigger_time": 10}}})
    reg.refresh.assert_awaited_once()
    for args in [(0, 5, 10), (30, -1, 10), (30, 5, True), (3601, 5, 10)]:
        with pytest.raises(BadRequest):
            await area.clips_change(reg, caller, "hallway-cam", *args)
    reg, caller = battery_house(make_house, 1)
    with pytest.raises(Forbidden):
        await area.clips_change(reg, caller, "hallway-cam", 30, 5, 10)
    reg, caller = battery_house(make_house)
    caller.respond("setClipsConfig", {"error_code": -1})
    with pytest.raises(DeviceUnavailable):
        await area.clips_change(reg, caller, "hallway-cam", 30, 5, 10)
    reg.refresh.assert_not_awaited()


async def test_battery_range_and_versions(make_house):
    reg, caller = battery_house(make_house)
    for setting, value in [("power_mode", "unknown"), ("working_mode", "unknown"), ("wake_up", "unknown")]:
        with pytest.raises(BadRequest):
            await area.battery_change(reg, caller, "hallway-cam", setting, value)
    assert not any(m in area.WRITES for m in caller.methods_sent())
    reg, caller = house(make_house, components=BATTERY_COMPONENTS)
    for setting in ["battery_status", "battery_capability"]:
        with pytest.raises(BadRequest, match="version"):
            await area.read_setting(reg, caller, "hallway-cam", setting)


async def test_charging_privacy_confirmation_and_support(make_house):
    from tolpo.calls import NeedsConfirmation
    reg, caller = battery_house(make_house, 3)
    for confirm in [None, "setChargingMode other"]:
        with pytest.raises(NeedsConfirmation):
            await area.battery_change(reg, caller, "hallway-cam", "charging_privacy", False, confirm)
    assert "setChargingMode" not in caller.methods_sent()
    await area.battery_change(reg, caller, "hallway-cam", "charging_privacy", False, "setChargingMode hallway-cam")
    assert caller.sent[-1] == ("hallway-cam", "setChargingMode", {"battery": {"charging_mode": {"charging_privacy_mode": "off"}}})
    caller.respond("setChargingMode", {"error_code": -1})
    with pytest.raises(DeviceUnavailable):
        await area.battery_change(reg, caller, "hallway-cam", "charging_privacy", True, "setChargingMode hallway-cam")
    reg, caller = battery_house(make_house, 2)
    with pytest.raises(Forbidden, match="level 3"):
        await area.battery_change(reg, caller, "hallway-cam", "charging_privacy", True, "setChargingMode hallway-cam")
    assert "setChargingMode" not in caller.methods_sent()
    reg, caller = battery_house(make_house)
    caller.respond("getBatteryCapability", {"battery": {"capability": {"charging_privacy_mode_support": "0"}}})
    with pytest.raises(BadRequest, match="charging privacy support"):
        await area.battery_change(reg, caller, "hallway-cam", "charging_privacy", True, "setChargingMode hallway-cam")


async def test_statistics_wire_and_hidden_fields(make_house):
    reg, caller = battery_house(make_house, 0)
    caller.respond("getBatteryStatistic", {"battery": {"statistic": {"day_num": 1, "day": [{"time": "Today\n", "bat_per": 75, "solar_charge_cap": 1.2, "password": "SECRET"}], "ssid": "SECRET"}}})
    result = await area.battery_statistics(reg, caller, "hallway-cam", 1)
    assert caller.sent[-1][2] == {"battery": {"statistic": {"days": 1}}}
    assert result == {"device": "hallway-cam", "statistics": {"day_num": 1, "day": [{"time": "Today", "bat_per": 75, "solar_charge_cap": 1.2}]}}
    with pytest.raises(BadRequest):
        await area.battery_statistics(reg, caller, "hallway-cam", 0)
    caller.respond("getBatteryStatistic", TimeoutError())
    with pytest.raises(DeviceUnavailable, match="did not answer"):
        await area.battery_statistics(reg, caller, "hallway-cam", 1)


async def test_statistics_as_a_real_doorbell_answers_without_the_module_around_them(make_house):
    """Checked on a D230: the answer is {"statistic": {...}}, with no "battery" around it."""
    reg, caller = battery_house(make_house, 0)
    caller.respond("getBatteryStatistic", {"statistic": {"day_num": 1, "day": [{"time": "2026-10-11", "bat_per": 71, "bat_usage": 3,
                                                                                "pir_wakeup_with_event": 2, "hour_runtime": [0] * 24, "ssid": "SECRET"}]}})
    result = await area.battery_statistics(reg, caller, "hallway-cam", 7)
    assert result["statistics"]["day"][0]["bat_per"] == 71 and "SECRET" not in str(result)
    caller.respond("getBatteryStatistic", {"something_else": {"day_num": 1}})
    with pytest.raises(DeviceUnavailable, match="unexpected response shape"):
        await area.battery_statistics(reg, caller, "hallway-cam", 7)


async def test_charging_privacy_is_read_from_getChargingMode_as_a_real_battery_camera_answers(make_house):
    """A C465 and a C610 refuse getChargingPrivacyMode and answer getChargingMode with both fields."""
    reg, caller = battery_house(make_house, 0)
    caller.respond("getChargingMode", {"battery": {"charging_mode": {"charging_privacy_mode": "on", "adapter_mode": "mains\nonly"}}})
    result = await area.read_setting(reg, caller, "hallway-cam", "charging_privacy")
    assert result == {"device": "hallway-cam", "charging_privacy": {"charging_privacy_mode": "on"}}
    assert caller.sent[-1][1:] == ("getChargingMode", {"battery": {"name": ["charging_mode"]}})
    assert "getChargingPrivacyMode" not in caller.methods_sent()
    assert (await area.read_setting(reg, caller, "hallway-cam", "charging"))["charging"] == {"adapter_mode": "mains only", "charging_privacy_mode": "on"}


async def test_changing_automatic_updates_does_not_depend_on_a_capability_read_the_cameras_refuse(make_house):
    """A C225, a C465 and a C610 refuse getFirmwareCapability. The configuration read decides instead."""
    from kasa.exceptions import DeviceError, SmartErrorCode

    reg, caller = battery_house(make_house, 4)
    caller.respond("getAppComponentList", {"app_component": {"app_component_list": [{"name": "firmware", "version": 1}]}})
    caller.respond("getFirmwareCapability", DeviceError("x", error_code=SmartErrorCode.PROTOCOL_FORMAT_ERROR))
    caller.respond("getFirmwareAutoUpgradeConfig", {"auto_upgrade": {"common": {"enabled": "off", "time": "03:00", "random_range": "120"}}})
    result = await area.system_change(reg, caller, "hallway-cam", "auto_update", True, "setFirmwareAutoUpgradeConfig hallway-cam")
    assert caller.sent[-1][1] == "setFirmwareAutoUpgradeConfig"
    assert caller.sent[-1][2]["auto_upgrade"]["common"]["enabled"] == "on"
    # a camera that answers the capability read and says no is still refused
    caller.respond("getFirmwareCapability", {"auto_upgrade": {"auto_upgrade_support": "0"}})
    with pytest.raises(BadRequest, match="does not report automatic firmware update support"):
        await area.system_change(reg, caller, "hallway-cam", "auto_update", True, "setFirmwareAutoUpgradeConfig hallway-cam")


def audio_house(make_house, tier=4):
    reg, caller = house(make_house, tier, [*IMAGE_COMPONENTS, "audio", "audioCapability", "voiceEnhancement"])
    caller.respond("getAudioConfig", {"audio_config": {"microphone": {"volume": "50", "encode_type": "g711", "channels": "1", "bitrate": "64", "sampling_rate": "8000", "password": "SECRET"}, "speaker": {"volume": "40"}}})
    return reg, caller


AUDIO = [
    ("microphone", 0, "setMicrophoneVolume", {"audio_config": {"microphone": {"volume": "0", "encode_type": "g711", "channels": "1", "bitrate": "64", "sampling_rate": "8000"}}}),
    ("speaker", 70, "setSpeakerVolume", {"audio_config": {"speaker": {"volume": "70"}}}),
    ("record_audio", False, "setRecordAudio", {"audio_config": {"record_audio": {"enabled": "off"}}}),
    ("voice_enhancement", True, "setVoiceEnhancement", {"audio_config": {"voice_enhancement": {"enabled": "on"}}}),
]


@pytest.mark.parametrize("setting,value,method,params", AUDIO)
async def test_audio_wire_policy_error(make_house, setting, value, method, params):
    reg, caller = audio_house(make_house, 2)
    result = await area.audio_change(reg, caller, "hallway-cam", setting, value)
    assert caller.sent[-1] == ("hallway-cam", method, params)
    assert result == {"device": "hallway-cam", "changed": {setting: value}}
    reg.refresh.assert_awaited_once()
    reg, caller = audio_house(make_house, 1)
    with pytest.raises(Forbidden):
        await area.audio_change(reg, caller, "hallway-cam", setting, value)
    assert method not in caller.methods_sent()
    reg, caller = audio_house(make_house)
    caller.respond(method, {"error_code": -1})
    with pytest.raises(DeviceUnavailable):
        await area.audio_change(reg, caller, "hallway-cam", setting, value)
    reg.refresh.assert_not_awaited()


async def test_audio_validation_and_support(make_house):
    reg, caller = audio_house(make_house)
    for setting, value in [("microphone", -1), ("speaker", 101), ("speaker", True), ("record_audio", "on")]:
        with pytest.raises(BadRequest):
            await area.audio_change(reg, caller, "hallway-cam", setting, value)
    assert not any(m in area.WRITES for m in caller.methods_sent())
    caller.respond("getAudioConfig", {"audio_config": {"microphone": {}}})
    with pytest.raises(BadRequest, match="volume support"):
        await area.audio_change(reg, caller, "hallway-cam", "microphone", 50)

SYSTEM_COMPONENTS = [*IMAGE_COMPONENTS, "system", "timezone", "dateTime", "upnpc", "localCtrl", "account", "reboot", "firmware"]


def system_house(make_house, tier=4, system_version=1):
    reg, caller = house(make_house, tier, SYSTEM_COMPONENTS)
    if system_version != 1:
        caller.respond("getAppComponentList", {"app_component": {"app_component_list": [{"name": c, "version": system_version if c == "system" else 1} for c in SYSTEM_COMPONENTS]}})
    caller.respond("getUpnpInfo", {"upnpc": {"upnpc_info": {"mode": "auto", "enabled": "off"}}})
    caller.respond("getThirdAccount", {"user_management": {"third_account": {"enabled": "on", "username": "SECRET", "passwd": "SECRET"}}})
    caller.respond("getFirmwareCapability", {"auto_upgrade": {"auto_upgrade_support": "1", "upgrade_process_support": "1"}})
    caller.respond("getFirmwareAutoUpgradeConfig", {"auto_upgrade": {"common": {"enabled": "off", "time": "03:00:00", "random_range": 30}}})
    caller.respond("getReboot", {"timing_reboot": {"reboot": {"enabled": "off", "day": "0", "time": "03:00:00", "random_range": 30, "now": "1", "password": "SECRET"}}})
    return reg, caller


SYSTEM = [
    ("timezone", "Etc/UTC", "setTimezone", {"system": {"basic": {"timezone": "UTC+00:00", "zone_id": "Etc/UTC", "timing_mode": "ntp"}}}, 2),
    ("upnp", True, "setUpnpInfo", {"upnpc": {"upnpc_info": {"enabled": "on", "mode": "auto"}}}, 3),
    ("compatibility", False, "setThirdPartyCompatibility", {"local_ctrl": {"third_party_setting": {"enabled": "off"}}}, 4),
    ("account", True, "setAccountEnabled", {"user_management": {"set_account_enabled": {"enabled": "on", "secname": "third_account"}}}, 3),
    ("reboot", None, "rebootDevice", {"system": {"reboot": {}}}, 2),
    ("factory_reset", None, "reset", {"system": {"reset": {}}}, 4),
    ("restore", None, "configRestore", {"system": {"config_restore": {}}}, 4),
    ("firmware_check", None, "checkFirmwareVersionByCloud", {"cloud_config": {"check_fw_version": "null"}}, 1),
    ("firmware_update", None, "startFirmwareUpgrade", {"cloud_config": {"fw_download": "null"}}, 4),
    ("auto_update", True, "setFirmwareAutoUpgradeConfig", {"auto_upgrade": {"common": {"enabled": "on", "time": "03:00:00", "random_range": 30}}}, 4),
]


@pytest.mark.parametrize("setting,value,method,params,tier", SYSTEM)
async def test_system_wire_policy_confirmation_and_errors(make_house, setting, value, method, params, tier):
    from tolpo.calls import NeedsConfirmation
    reg, caller = system_house(make_house, tier)
    phrase = f"{method} hallway-cam" if tier >= 3 else None
    if tier >= 3:
        for invalid in [None, f"{method} other"]:
            with pytest.raises(NeedsConfirmation):
                await area.system_change(reg, caller, "hallway-cam", setting, value, invalid)
        assert method not in caller.methods_sent()
    result = await area.system_change(reg, caller, "hallway-cam", setting, value, phrase)
    assert caller.sent[-1] == ("hallway-cam", method, params)
    assert "SECRET" not in str(result) and len(result) == 2
    reg.refresh.assert_awaited_once()
    if tier:
        reg, caller = system_house(make_house, tier - 1)
        with pytest.raises(Forbidden, match=f"level {tier}"):
            await area.system_change(reg, caller, "hallway-cam", setting, value, phrase)
        assert method not in caller.methods_sent()
        reg.refresh.assert_not_awaited()
    reg, caller = system_house(make_house)
    caller.respond(method, {"error_code": -1, "password": "SECRET"})
    with pytest.raises(DeviceUnavailable, match=method) as error:
        await area.system_change(reg, caller, "hallway-cam", setting, value, phrase)
    assert "SECRET" not in str(error.value)
    reg.refresh.assert_not_awaited()


async def test_newer_reset_uses_soft_reset(make_house):
    from tolpo.calls import NeedsConfirmation
    reg, caller = system_house(make_house, system_version=4)
    with pytest.raises(NeedsConfirmation, match="softReset hallway-cam"):
        await area.system_change(reg, caller, "hallway-cam", "factory_reset", confirm="reset hallway-cam")
    await area.system_change(reg, caller, "hallway-cam", "factory_reset", confirm="softReset hallway-cam")
    assert caller.sent[-1] == ("hallway-cam", "softReset", {"system": {"soft_reset": {"type": "factory"}}})
    reg, caller = system_house(make_house, tier=3, system_version=4)
    with pytest.raises(Forbidden):
        await area.system_change(reg, caller, "hallway-cam", "factory_reset", confirm="softReset hallway-cam")
    reg, caller = system_house(make_house, system_version=4)
    caller.respond("softReset", {"error_code": -1})
    with pytest.raises(DeviceUnavailable):
        await area.system_change(reg, caller, "hallway-cam", "factory_reset", confirm="softReset hallway-cam")


async def test_schedule_wire_validation_policy_error(make_house):
    reg, caller = system_house(make_house, 2)
    await area.reboot_schedule(reg, caller, "hallway-cam", True, "04:15")
    assert caller.sent[-1] == ("hallway-cam", "setReboot", {"timing_reboot": {"reboot": {"enabled": "on", "day": "0", "time": "04:15:00", "random_range": 30}}})
    reg.refresh.assert_awaited_once()
    for time in ["24:00", "12:60", "4:15", "04:15\n"]:
        with pytest.raises(BadRequest):
            await area.reboot_schedule(reg, caller, "hallway-cam", True, time)
    reg, caller = system_house(make_house, 1)
    with pytest.raises(Forbidden):
        await area.reboot_schedule(reg, caller, "hallway-cam", True)
    reg, caller = system_house(make_house)
    caller.respond("setReboot", {"error_code": -1})
    with pytest.raises(DeviceUnavailable):
        await area.reboot_schedule(reg, caller, "hallway-cam", True)


async def test_system_validation_and_missing_capabilities(make_house):
    reg, caller = system_house(make_house)
    for zone in ["No/SuchZone", "/etc/passwd", " bad ", "Europe/Berlin\n"]:
        with pytest.raises(BadRequest):
            await area.system_change(reg, caller, "hallway-cam", "timezone", zone)
    caller.respond("getFirmwareCapability", {"auto_upgrade": {"auto_upgrade_support": "0"}})
    with pytest.raises(BadRequest, match="automatic firmware"):
        await area.system_change(reg, caller, "hallway-cam", "auto_update", True, "setFirmwareAutoUpgradeConfig hallway-cam")
    assert "setFirmwareAutoUpgradeConfig" not in caller.methods_sent()


async def test_network_and_account_read_hide_identities(make_house):
    reg, caller = system_house(make_house, 0)
    caller.respond("getConnectionType", {"rssi": 3, "rssiValue": -60, "link_type": "wifi", "channel": 6, "ssid": "SECRET", "mac": "SECRET", "ipaddr": "SECRET"})
    assert await area.read_setting(reg, caller, "hallway-cam", "wifi") == {"device": "hallway-cam", "wifi": {"rssi": 3, "rssiValue": -60, "link_type": "wifi", "channel": 6}}
    assert caller.sent[-1][2] == {"network": {"get_connection_type": []}}
    assert await area.read_setting(reg, caller, "hallway-cam", "account") == {"device": "hallway-cam", "account": {"enabled": "on"}}


@pytest.mark.parametrize("setting", list(area.READS))
async def test_every_read_method_wire_compaction_and_errors(make_house, setting):
    reg, caller = house(make_house, 0, [*SYSTEM_COMPONENTS, *BATTERY_COMPONENTS, "audio", "audioCapability", "voiceEnhancement"])
    caller.respond("getAppComponentList", {"app_component": {"app_component_list": [{"name": c, "version": 3 if c == "battery" else 2 if c in {"clips", "wakeUp", "pir"} else 1} for c in set([*SYSTEM_COMPONENTS, *BATTERY_COMPONENTS, "audio", "audioCapability", "voiceEnhancement"])]}})
    spec = area.READS[setting]
    field = spec.fields[0]
    safe = {field: "line\n\x00 " + "x" * 200, "password": "SECRET", "passwd": "SECRET", "username": "SECRET", "ssid": "SECRET", "mac": "SECRET", "token": "SECRET", "public_key": "SECRET", "matter_setup_code": "SECRET", "empty": []}
    response = safe if spec.selector == "direct_list" else {spec.module: safe if spec.section == "*" else {spec.section: safe}}
    caller.respond(spec.method, response)
    operation = (lambda: area.battery_statistics(reg, caller, "hallway-cam", 2)) if setting == "statistics" else (lambda: area.read_setting(reg, caller, "hallway-cam", setting))
    result = await operation()
    assert result == {"device": "hallway-cam", setting: {field: area.clean(safe[field])}}
    if setting == "statistics":
        expected = {"battery": {"statistic": {"days": 2}}}
    elif spec.method == "getOsd":
        expected = {"OSD": {"name": ["date", "logo"], "table": ["label_info"]}}
    elif spec.method == "getOsdCapability":
        expected = {"osd_capability": {"name": ["date_default", "logo_default", "label_info_default", "capability"], "table": ["capability"]}}
    elif spec.selector == "flat":
        expected = {spec.module: "null"}
    elif spec.selector == "direct_list":
        expected = {spec.module: {spec.section: []}}
    elif spec.selector == "direct":
        expected = {spec.module: {spec.section: {}}}
    else:
        expected = {spec.module: {spec.selector: [spec.section]}}
    assert caller.sent[-1] == ("hallway-cam", spec.method, expected)
    caller.respond(spec.method, {} if spec.selector == "direct_list" else {spec.module: {} if spec.section == "*" else {spec.section: {}}})
    assert await operation() == {"device": "hallway-cam"}
    caller.respond(spec.method, {"error_code": -1, "message": "SECRET"})
    with pytest.raises(DeviceUnavailable, match=spec.method) as error:
        await operation()
    assert "SECRET" not in str(error.value)
    caller.respond(spec.method, {"unexpected": "SECRET"})
    with pytest.raises(DeviceUnavailable, match="unexpected response shape"):
        await operation()


async def test_refresh_failure_reports_accepted_action(make_house):
    reg, caller = house(make_house)
    reg.refresh.side_effect = DeviceUnavailable("camera did not answer")
    with pytest.raises(DeviceUnavailable, match="stopMove was accepted.*could not be refreshed"):
        await area.ptz_action(reg, caller, "hallway-cam", "stop")
    assert caller.sent[-1][1] == "stopMove"


async def test_sleeping_camera_error_mentions_ble_without_retry(make_house):
    reg, caller = house(make_house)
    caller.respond("getAppComponentList", TimeoutError())
    with pytest.raises(DeviceUnavailable, match="sleeping battery camera may need BLE"):
        await area.ptz_action(reg, caller, "hallway-cam", "cruise", axis="stop")
    assert caller.methods_sent() == ["getAppComponentList"]


async def test_nested_rejection_is_not_empty_success(make_house):
    reg, caller = house(make_house)
    caller.respond("stopMove", {"motor": {"stop": {"error_code": "-1", "password": "SECRET"}}})
    with pytest.raises(DeviceUnavailable, match="stopMove"):
        await area.ptz_action(reg, caller, "hallway-cam", "stop")
    reg.refresh.assert_not_awaited()


TOOLS = [
    ("camera_ptz_status", {"setting": "presets"}, "getPresetConfig"),
    ("camera_ptz_stop", {}, "stopMove"),
    ("camera_ptz_cruise", {"axis": "horizontal"}, "cruiseMove"),
    ("camera_ptz_calibrate", {}, "manualCalibrate"),
    ("camera_ptz_save_preset", {"name": "Desk"}, "addMotorPostion"),
    ("camera_ptz_delete_presets", {"ids": [1]}, "deletePreset"),
    ("camera_ptz_patrol", {"enabled": True}, "setPatrolStatus"),
    ("camera_ptz_patrol_point", {"action": "add", "preset_id": 1, "name": "Desk", "wait_seconds": 30}, "addPatrol"),
    ("camera_ptz_auto_tracking", {"enabled": True}, "setTargetTrackConfig"),
    ("camera_image_read", {"setting": "osd"}, "getOsd"),
    ("camera_image_mode", {"setting": "style", "value": "bright"}, "setImageStyleConfig"),
    ("camera_image_mode", {"setting": "hdr", "value": "on"}, "setHDR"),
    ("camera_image_osd", {"element": "time", "enabled": True}, "setOsd"),
    ("camera_image_privacy_zones", {"enabled": False, "confirm": "setCoverConfig hallway-cam"}, "setCoverConfig"),
    ("camera_image_led", {"enabled": False, "confirm": "setLedStatus hallway-cam"}, "setLedStatus"),
    ("camera_system_read", {"setting": "privacy"}, "getLensMaskConfig"),
    ("camera_system_privacy", {"enabled": False, "confirm": "setLensMaskConfig hallway-cam"}, "setLensMaskConfig"),
    ("camera_battery_read", {"setting": "battery_capability"}, "getBatteryCapability"),
    ("camera_battery_mode", {"setting": "power_mode", "value": "solar"}, "setPowerMode"),
    ("camera_battery_clips", {"length_seconds": 30, "buffer_seconds": 5, "retrigger_seconds": 10}, "setClipsConfig"),
    ("camera_battery_mode", {"setting": "wake_up", "value": "doorbell"}, "setWakeUpConfig"),
    ("camera_battery_charging_privacy", {"enabled": True, "confirm": "setChargingMode hallway-cam"}, "setChargingMode"),
    ("camera_battery_statistics", {"days": 2}, "getBatteryStatistic"),
    ("camera_audio_read", {"setting": "microphone"}, "getAudioConfig"),
    ("camera_audio_set", {"setting": "microphone", "volume": 0}, "setMicrophoneVolume"),
    ("camera_audio_set", {"setting": "speaker", "volume": 70}, "setSpeakerVolume"),
    ("camera_audio_set", {"setting": "record_audio", "enabled": False}, "setRecordAudio"),
    ("camera_audio_set", {"setting": "voice_enhancement", "enabled": True}, "setVoiceEnhancement"),
    ("camera_system_read", {"setting": "account"}, "getThirdAccount"),
    ("camera_system_timezone", {"zone_id": "Etc/UTC"}, "setTimezone"),
    ("camera_system_upnp", {"enabled": True, "confirm": "setUpnpInfo hallway-cam"}, "setUpnpInfo"),
    ("camera_system_compatibility", {"enabled": True, "confirm": "setThirdPartyCompatibility hallway-cam"}, "setThirdPartyCompatibility"),
    ("camera_system_account_enabled", {"enabled": True, "confirm": "setAccountEnabled hallway-cam"}, "setAccountEnabled"),
    ("camera_system_reboot", {}, "rebootDevice"),
    ("camera_system_reboot_schedule", {"enabled": True, "time": "04:15"}, "setReboot"),
    ("camera_system_firmware_check", {}, "checkFirmwareVersionByCloud"),
    ("camera_system_firmware_update", {"confirm": "startFirmwareUpgrade hallway-cam"}, "startFirmwareUpgrade"),
    ("camera_system_auto_update", {"enabled": True, "confirm": "setFirmwareAutoUpgradeConfig hallway-cam"}, "setFirmwareAutoUpgradeConfig"),
    ("camera_system_reset", {"action": "factory_reset", "confirm": "softReset hallway-cam"}, "softReset"),
]


def full_house(make_house, tier):
    reg, caller = battery_house(make_house, tier)
    for factory in [audio_house, system_house]:
        _, other = factory(make_house, tier)
        caller.responses.update({k: v for k, v in other.responses.items() if k[1] != "getAppComponentList"})
    components = [*SYSTEM_COMPONENTS, *BATTERY_COMPONENTS, "audio", "audioCapability", "voiceEnhancement"]
    caller.respond("getAppComponentList", {"app_component": {"app_component_list": [{"name": c, "version": 3 if c == "battery" else 2 if c in {"clips", "wakeUp", "pir"} else 1} for c in set(components)]}})
    caller.respond("getLensMaskConfig", {"lens_mask": {"lens_mask_info": {"enabled": "on"}}})
    caller.respond("getBatteryStatistic", {"battery": {"statistic": {"day_num": 2}}})
    return reg, caller


@pytest.mark.parametrize("name,arguments,method", TOOLS)
async def test_every_tool_through_mcp(make_house, mcp, name, arguments, method):
    reg, caller = full_house(make_house, 4)
    async with mcp(reg, caller) as client:
        result = await client.call_tool(name, {"device": "hallway-cam", **arguments})
        assert not result.is_error, result
        assert result.structured_content["device"] == "hallway-cam" and "SECRET" not in str(result)
        assert caller.sent[-1][1] == method
        caller.respond(method, {"error_code": -1, "message": "SECRET"})
        failed = await client.call_tool(name, {"device": "hallway-cam", **arguments})
        assert failed.is_error and method in failed.content[0].text and "SECRET" not in failed.content[0].text


async def test_tools_are_typed_documented_and_policy_annotated(make_house, mcp):
    from pathlib import Path
    reg, caller = full_house(make_house, 0)
    async with mcp(reg, caller) as client:
        found = {t.name: t for t in (await client.list_tools()).tools if t.name.startswith(("camera_ptz_", "camera_image_", "camera_battery_", "camera_audio_", "camera_system_"))}
    assert set(found) == {name for name, _, _ in TOOLS}
    doc = Path("docs/areas/camera_system.md").read_text()
    for name, _, method in TOOLS:
        tool = found[name]
        assert f"- `{name}`:" in doc
        assert tool.description and tool.output_schema
        for field in tool.input_schema["properties"].values():
            assert field.get("description"), (name, field)
        tier = area.WRITES.get(method, (0,))[0]
        assert tool.annotations.read_only_hint == (tier == 0)
        assert tool.annotations.destructive_hint == (tier >= 3) if tier else True


@pytest.mark.parametrize("name,arguments,method", [case for case in TOOLS if area.WRITES.get(case[2], (0,))[0] > 0])
async def test_every_write_tool_refused_through_mcp(make_house, mcp, name, arguments, method):
    reg, caller = full_house(make_house, 0)
    async with mcp(reg, caller) as client:
        result = await client.call_tool(name, {"device": "hallway-cam", **arguments})
    assert result.is_error and "level" in result.content[0].text
    assert "allow_methods" not in result.content[0].text
    assert method not in caller.methods_sent()
    reg.refresh.assert_not_awaited()


@pytest.mark.parametrize("name,arguments,method", [case for case in TOOLS if area.WRITES.get(case[2], (0,))[0] >= 3])
async def test_every_risky_tool_confirmation_through_mcp(make_house, mcp, name, arguments, method):
    reg, caller = full_house(make_house, 4)
    async with mcp(reg, caller) as client:
        for invalid in [None, f"{method} other"]:
            result = await client.call_tool(name, {"device": "hallway-cam", **arguments, "confirm": invalid})
            assert result.is_error and f"{method} hallway-cam" in result.content[0].text
    assert method not in caller.methods_sent()
    reg.refresh.assert_not_awaited()


async def test_device_errors_never_echo_raw_private_data(make_house):
    from kasa.exceptions import AuthenticationError, DeviceError
    for error, safe in [(DeviceError("SECRET ssid passwd rtsp://private", error_code=None), "reported a device error"), (AuthenticationError("SECRET"), "rejected the account credentials")]:
        reg, caller = house(make_house)
        caller.respond("stopMove", error)
        with pytest.raises(DeviceUnavailable) as caught:
            await area.ptz_action(reg, caller, "hallway-cam", "stop")
        assert "hallway-cam" in str(caught.value) and "stopMove" in str(caught.value)
        assert safe in str(caught.value) and "SECRET" not in str(caught.value) and "rtsp" not in str(caught.value)


@pytest.mark.parametrize("name,arguments,method", [case for case in TOOLS if area.WRITES.get(case[2], (0,))[0] > 0 and case[0] != "camera_ptz_stop"])
async def test_every_write_tool_refuses_missing_features(make_house, mcp, name, arguments, method):
    reg, caller = full_house(make_house, 4)
    caller.respond("getAppComponentList", {"app_component": {"app_component_list": []}})
    async with mcp(reg, caller) as client:
        result = await client.call_tool(name, {"device": "hallway-cam", **arguments})
    assert result.is_error and "support" in result.content[0].text
    assert caller.methods_sent() == ["getAppComponentList"]
    reg.refresh.assert_not_awaited()


@pytest.mark.parametrize("kind", ["camera", "doorbell"])
async def test_declared_methods_and_component_gate_for_both_kinds(make_house, kind):
    from tolpo import policy
    reg, caller = image_house(make_house, 2)
    from kasa import DeviceType
    reg.resolve_one("hallway-cam").device.device_type = DeviceType.Camera if kind == "camera" else DeviceType.Doorbell
    for rule in area.METHODS:
        if kind in rule.kinds:
            assert policy.rule_for(kind, rule.method).tier == rule.tier
    await area.image_change(reg, caller, "hallway-cam", "hdr", True)
    assert caller.sent[-1][1] == "setHDR"
    caller.respond("getAppComponentList", {"app_component": {"app_component_list": []}})
    with pytest.raises(BadRequest, match="support for video"):
        await area.image_change(reg, caller, "hallway-cam", "hdr", True)


# Sanitized fixture responses, 2026-10-11. Kept inline so tests need no private fixture path.
CAPTURED = {'C225': {'getAppComponentList': {'app_component': {'app_component_list': [{'name': 'sdCard', 'version': 1},
                                                                           {'name': 'timezone', 'version': 1},
                                                                           {'name': 'system', 'version': 4},
                                                                           {'name': 'led', 'version': 1},
                                                                           {'name': 'playback', 'version': 6},
                                                                           {'name': 'detection',
                                                                            'version': 3},
                                                                           {'name': 'alert', 'version': 2},
                                                                           {'name': 'firmware', 'version': 2},
                                                                           {'name': 'account', 'version': 2},
                                                                           {'name': 'quickSetup',
                                                                            'version': 1},
                                                                           {'name': 'ptz', 'version': 1},
                                                                           {'name': 'video', 'version': 3},
                                                                           {'name': 'lensMask', 'version': 2},
                                                                           {'name': 'lightFrequency',
                                                                            'version': 1},
                                                                           {'name': 'dayNightMode',
                                                                            'version': 1},
                                                                           {'name': 'osd', 'version': 2},
                                                                           {'name': 'record', 'version': 1},
                                                                           {'name': 'videoRotation',
                                                                            'version': 1},
                                                                           {'name': 'audio', 'version': 3},
                                                                           {'name': 'audioCapability',
                                                                            'version': 1},
                                                                           {'name': 'audioSourceCapability',
                                                                            'version': 1},
                                                                           {'name': 'diagnose', 'version': 2},
                                                                           {'name': 'msgPush', 'version': 3},
                                                                           {'name': 'linecrossingDetection',
                                                                            'version': 2},
                                                                           {'name': 'deviceShare',
                                                                            'version': 1},
                                                                           {'name': 'tamperDetection',
                                                                            'version': 1},
                                                                           {'name': 'tapoCare', 'version': 1},
                                                                           {'name': 'targetTrack',
                                                                            'version': 1},
                                                                           {'name': 'blockZone',
                                                                            'version': 1},
                                                                           {'name': 'babyCryDetection',
                                                                            'version': 1},
                                                                           {'name': 'personDetection',
                                                                            'version': 2},
                                                                           {'name': 'needSubscriptionServiceList',
                                                                            'version': 1},
                                                                           {'name': 'patrol', 'version': 1},
                                                                           {'name': 'panoramicView',
                                                                            'version': 1},
                                                                           {'name': 'vehicleDetection',
                                                                            'version': 1},
                                                                           {'name': 'petDetection',
                                                                            'version': 1},
                                                                           {'name': 'meowDetection',
                                                                            'version': 1},
                                                                           {'name': 'barkDetection',
                                                                            'version': 1},
                                                                           {'name': 'glassDetection',
                                                                            'version': 1},
                                                                           {'name': 'alarmDetection',
                                                                            'version': 1},
                                                                           {'name': 'infLamp', 'version': 1},
                                                                           {'name': 'markerBox',
                                                                            'version': 1},
                                                                           {'name': 'nvmp', 'version': 1},
                                                                           {'name': 'iotCloud', 'version': 1},
                                                                           {'name': 'hdr', 'version': 1},
                                                                           {'name': 'homekit', 'version': 1},
                                                                           {'name': 'upnpc', 'version': 2},
                                                                           {'name': 'bleOnboarding',
                                                                            'version': 2},
                                                                           {'name': 'recordDownload',
                                                                            'version': 2},
                                                                           {'name': 'recordDownloadOccupy',
                                                                            'version': 1},
                                                                           {'name': 'smartTrack',
                                                                            'version': 1},
                                                                           {'name': 'encryption',
                                                                            'version': 3},
                                                                           {'name': 'staticIp', 'version': 2},
                                                                           {'name': 'detectionRegion',
                                                                            'version': 2},
                                                                           {'name': 'timeFormat',
                                                                            'version': 1},
                                                                           {'name': 'snapshot', 'version': 2},
                                                                           {'name': 'relayPreConnection',
                                                                            'version': 1},
                                                                           {'name': 'hubManage',
                                                                            'version': 1},
                                                                           {'name': 'cipcV2Relay',
                                                                            'version': 1},
                                                                           {'name': 'streamCapability',
                                                                            'version': 1},
                                                                           {'name': 'playbackDelete',
                                                                            'version': 1}]}},
          'getClockStatus': {'system': {'clock_status': {'local_time': '2026-10-11 22:37:39',
                                                         'seconds_from_1970': 1791751059}}},
          'getConnectionType': {'link_type': 'wifi', 'rssi': '2', 'rssiValue': -72, 'ssid': 'REDACTED'},
          'getLedStatus': {'led': {'config': {'enabled': 'off'}}},
          'getLensMaskConfig': {'lens_mask': {'lens_mask_info': {'enabled': 'off'}}},
          'getTimezone': {'system': {'basic': {'timezone': 'REDACTED',
                                               'timing_mode': 'ntp',
                                               'zone_id': 'REDACTED'}}},
          'getPresetConfig': {'preset': {'preset': {'id': [],
                                                    'name': [],
                                                    'position_pan': [],
                                                    'position_tilt': [],
                                                    'position_zoom': [],
                                                    'read_only': []}}},
          'getAudioConfig': {'audio_config': {'microphone': {'bitrate': '64',
                                                             'channels': '1',
                                                             'echo_cancelling': 'off',
                                                             'encode_type': 'G711ulaw',
                                                             'factory_noise_cancelling': 'off',
                                                             'input_device_type': 'MicIn',
                                                             'mute': 'off',
                                                             'noise_cancelling': 'on',
                                                             'sampling_rate': '16',
                                                             'volume': '80'},
                                              'speaker': {'mute': 'off',
                                                          'output_device_type': 'SpeakerOut',
                                                          'volume': '80'}}}},
 'C465': {'getAppComponentList': {'app_component': {'app_component_list': [{'name': 'sdCard', 'version': 1},
                                                                           {'name': 'timezone', 'version': 1},
                                                                           {'name': 'system', 'version': 4},
                                                                           {'name': 'led', 'version': 1},
                                                                           {'name': 'playback', 'version': 6},
                                                                           {'name': 'detection',
                                                                            'version': 3},
                                                                           {'name': 'alert', 'version': 2},
                                                                           {'name': 'firmware', 'version': 2},
                                                                           {'name': 'quickSetup',
                                                                            'version': 1},
                                                                           {'name': 'video', 'version': 3},
                                                                           {'name': 'imageStyle',
                                                                            'version': 3},
                                                                           {'name': 'lensMask', 'version': 2},
                                                                           {'name': 'lightFrequency',
                                                                            'version': 3},
                                                                           {'name': 'dayNightMode',
                                                                            'version': 2},
                                                                           {'name': 'osd', 'version': 2},
                                                                           {'name': 'record', 'version': 1},
                                                                           {'name': 'audio', 'version': 3},
                                                                           {'name': 'audioCapability',
                                                                            'version': 1},
                                                                           {'name': 'diagnose', 'version': 1},
                                                                           {'name': 'msgPush', 'version': 3},
                                                                           {'name': 'deviceShare',
                                                                            'version': 1},
                                                                           {'name': 'tapoCare', 'version': 1},
                                                                           {'name': 'nightVisionMode',
                                                                            'version': 3},
                                                                           {'name': 'iotCloud', 'version': 1},
                                                                           {'name': 'needSubscriptionServiceList',
                                                                            'version': 1},
                                                                           {'name': 'blockZone',
                                                                            'version': 1},
                                                                           {'name': 'personDetection',
                                                                            'version': 2},
                                                                           {'name': 'vehicleDetection',
                                                                            'version': 1},
                                                                           {'name': 'petDetection',
                                                                            'version': 1},
                                                                           {'name': 'infLamp', 'version': 1},
                                                                           {'name': 'whiteLamp',
                                                                            'version': 1},
                                                                           {'name': 'pir', 'version': 3},
                                                                           {'name': 'battery', 'version': 3},
                                                                           {'name': 'clips', 'version': 2},
                                                                           {'name': 'markerBox',
                                                                            'version': 1},
                                                                           {'name': 'streamGrab',
                                                                            'version': 1},
                                                                           {'name': 'antiTheft',
                                                                            'version': 3},
                                                                           {'name': 'nvmp', 'version': 1},
                                                                           {'name': 'detectionRegion',
                                                                            'version': 2},
                                                                           {'name': 'ldc', 'version': 1},
                                                                           {'name': 'noHubBatteryCam',
                                                                            'version': 1},
                                                                           {'name': 'recordDownload',
                                                                            'version': 2},
                                                                           {'name': 'recordDownloadOccupy',
                                                                            'version': 1},
                                                                           {'name': 'smartAutoExposure',
                                                                            'version': 1},
                                                                           {'name': 'snapshot', 'version': 2},
                                                                           {'name': 'timeFormat',
                                                                            'version': 1},
                                                                           {'name': 'aov', 'version': 1},
                                                                           {'name': 'hubManage',
                                                                            'version': 1},
                                                                           {'name': 'wifiSwitch',
                                                                            'version': 1},
                                                                           {'name': 'wifiDualBand',
                                                                            'version': 1},
                                                                           {'name': 'bleOnboarding',
                                                                            'version': 2},
                                                                           {'name': 'encryption',
                                                                            'version': 3},
                                                                           {'name': 'aovSupportHub',
                                                                            'version': 1}]}},
          'getClockStatus': {'system': {'clock_status': {'local_time': '2026-10-11 22:37:41',
                                                         'seconds_from_1970': 1791751061}}},
          'getConnectionType': {'channel': 120,
                                'link_type': 'wifi',
                                'rssi': '4',
                                'rssiValue': -49,
                                'ssid': 'REDACTED'},
          'getLedStatus': {'led': {'config': {'enabled': 'on'}}},
          'getLensMaskConfig': {'lens_mask': {'lens_mask_info': {'enabled': 'off'}}},
          'getTimezone': {'system': {'basic': {'timezone': 'REDACTED',
                                               'timing_mode': 'ntp',
                                               'zone_id': 'REDACTED'}}},
          'getAudioConfig': {'audio_config': {'microphone': {'bitrate': '64',
                                                             'channels': '1',
                                                             'echo_cancelling': 'off',
                                                             'encode_type': 'G711ulaw',
                                                             'input_device_type': 'MicIn',
                                                             'mute': 'off',
                                                             'noise_cancelling': 'on',
                                                             'sampling_rate': '16',
                                                             'volume': '98'},
                                              'speaker': {'mute': 'off',
                                                          'output_device_type': 'SpeakerOut',
                                                          'volume': '100'}}}},
 'C610': {'getAppComponentList': {'app_component': {'app_component_list': [{'name': 'sdCard', 'version': 1},
                                                                           {'name': 'timezone', 'version': 1},
                                                                           {'name': 'system', 'version': 4},
                                                                           {'name': 'led', 'version': 1},
                                                                           {'name': 'playback', 'version': 6},
                                                                           {'name': 'detection',
                                                                            'version': 3},
                                                                           {'name': 'alert', 'version': 2},
                                                                           {'name': 'firmware', 'version': 2},
                                                                           {'name': 'quickSetup',
                                                                            'version': 3},
                                                                           {'name': 'video', 'version': 3},
                                                                           {'name': 'lensMask', 'version': 2},
                                                                           {'name': 'lightFrequency',
                                                                            'version': 3},
                                                                           {'name': 'dayNightMode',
                                                                            'version': 2},
                                                                           {'name': 'osd', 'version': 2},
                                                                           {'name': 'record', 'version': 1},
                                                                           {'name': 'audio', 'version': 3},
                                                                           {'name': 'audioCapability',
                                                                            'version': 1},
                                                                           {'name': 'diagnose', 'version': 1},
                                                                           {'name': 'msgPush', 'version': 3},
                                                                           {'name': 'deviceShare',
                                                                            'version': 1},
                                                                           {'name': 'tapoCare', 'version': 1},
                                                                           {'name': 'nightVisionMode',
                                                                            'version': 3},
                                                                           {'name': 'iotCloud', 'version': 1},
                                                                           {'name': 'needSubscriptionServiceList',
                                                                            'version': 1},
                                                                           {'name': 'blockZone',
                                                                            'version': 1},
                                                                           {'name': 'personDetection',
                                                                            'version': 2},
                                                                           {'name': 'infLamp', 'version': 1},
                                                                           {'name': 'whiteLamp',
                                                                            'version': 1},
                                                                           {'name': 'pir', 'version': 3},
                                                                           {'name': 'battery', 'version': 3},
                                                                           {'name': 'clips', 'version': 2},
                                                                           {'name': 'streamGrab',
                                                                            'version': 1},
                                                                           {'name': 'nvmp', 'version': 1},
                                                                           {'name': 'detectionRegion',
                                                                            'version': 2},
                                                                           {'name': 'noHubBatteryCam',
                                                                            'version': 1},
                                                                           {'name': 'recordDownload',
                                                                            'version': 2},
                                                                           {'name': 'recordDownloadOccupy',
                                                                            'version': 1},
                                                                           {'name': 'snapshot', 'version': 2},
                                                                           {'name': 'ptz', 'version': 1},
                                                                           {'name': 'patrol', 'version': 1},
                                                                           {'name': 'targetTrack',
                                                                            'version': 1},
                                                                           {'name': 'bleOnboarding',
                                                                            'version': 2},
                                                                           {'name': 'encryption',
                                                                            'version': 3},
                                                                           {'name': 'hubManage',
                                                                            'version': 1},
                                                                           {'name': 'panoramicVideo',
                                                                            'version': 1},
                                                                           {'name': 'imageStyle',
                                                                            'version': 3},
                                                                           {'name': 'audioSourceCapability',
                                                                            'version': 1},
                                                                           {'name': 'cipcV2Relay',
                                                                            'version': 1},
                                                                           {'name': 'facTest', 'version': 1},
                                                                           {'name': 'fileRelay',
                                                                            'version': 1},
                                                                           {'name': 'srtpWebrtc',
                                                                            'version': 1}]}},
          'getClockStatus': {'system': {'clock_status': {'local_time': '2026-10-11 22:37:43',
                                                         'seconds_from_1970': 1791751063}}},
          'getConnectionType': {'channel': 11,
                                'link_type': 'wifi',
                                'rssi': '2',
                                'rssiValue': -71,
                                'ssid': 'REDACTED'},
          'getLedStatus': {'led': {'config': {'enabled': 'off'}}},
          'getLensMaskConfig': {'lens_mask': {'lens_mask_info': {'enabled': 'off'}}},
          'getTimezone': {'system': {'basic': {'timezone': 'REDACTED',
                                               'timing_mode': 'ntp',
                                               'zone_id': 'REDACTED'}}},
          'getPresetConfig': {'preset': {'preset': {'id': ['1', '2', '3', '4'],
                                                    'name': ['Viewpoint 1',
                                                             'Viewpoint 2',
                                                             'Viewpoint 3',
                                                             'Viewpoint 4'],
                                                    'position_pan': ['-1.000000',
                                                                     '-0.403727',
                                                                     '0.113872',
                                                                     '0.461698'],
                                                    'position_tilt': ['-0.312500',
                                                                      '-0.531250',
                                                                      '-0.531250',
                                                                      '-0.531250'],
                                                    'position_zoom': [],
                                                    'read_only': ['0', '0', '0', '0']}}},
          'getAudioConfig': {'audio_config': {'microphone': {'bitrate': '64',
                                                             'channels': '1',
                                                             'echo_cancelling': 'off',
                                                             'encode_type': 'G711ulaw',
                                                             'input_device_type': 'MicIn',
                                                             'mute': 'off',
                                                             'noise_cancelling': 'on',
                                                             'sampling_rate': '16',
                                                             'volume': '100'},
                                              'speaker': {'mute': 'off',
                                                          'output_device_type': 'SpeakerOut',
                                                          'volume': '100'}}}},
 'D230': {'getClockStatus': {'system': {'clock_status': {'local_time': '2026-10-11 22:37:44',
                                                         'seconds_from_1970': 1791751064}}},
          'getLedStatus': {'led': {'config': {'enabled': 'on'}}},
          'getLensMaskConfig': {'lens_mask': {'lens_mask_info': {'enabled': 'off'}}},
          'getTimezone': {'system': {'basic': {'timezone': 'REDACTED',
                                               'timing_mode': 'ntp',
                                               'zone_id': 'REDACTED'}}},
          'getChildDeviceComponentList': {'child_component_list': [{'device_id': 'cam-1',
                                                                    'component_list': [{'name': 'sdCard',
                                                                                        'version': 2},
                                                                                       {'name': 'timezone',
                                                                                        'version': 1},
                                                                                       {'name': 'batCamSystem',
                                                                                        'version': 1},
                                                                                       {'name': 'led',
                                                                                        'version': 1},
                                                                                       {'name': 'playback',
                                                                                        'version': 3},
                                                                                       {'name': 'detection',
                                                                                        'version': 3},
                                                                                       {'name': 'firmware',
                                                                                        'version': 1},
                                                                                       {'name': 'video',
                                                                                        'version': 3},
                                                                                       {'name': 'lensMask',
                                                                                        'version': 2},
                                                                                       {'name': 'lightFrequency',
                                                                                        'version': 2},
                                                                                       {'name': 'dayNightMode',
                                                                                        'version': 2},
                                                                                       {'name': 'nightVisionMode',
                                                                                        'version': 3},
                                                                                       {'name': 'batCamOsd',
                                                                                        'version': 1},
                                                                                       {'name': 'record',
                                                                                        'version': 1},
                                                                                       {'name': 'audio',
                                                                                        'version': 3},
                                                                                       {'name': 'personDetection',
                                                                                        'version': 2},
                                                                                       {'name': 'vehicleDetection',
                                                                                        'version': 1},
                                                                                       {'name': 'petDetection',
                                                                                        'version': 1},
                                                                                       {'name': 'msgPush',
                                                                                        'version': 3},
                                                                                       {'name': 'deviceShare',
                                                                                        'version': 1},
                                                                                       {'name': 'tapoCare',
                                                                                        'version': 1},
                                                                                       {'name': 'pir',
                                                                                        'version': 1},
                                                                                       {'name': 'battery',
                                                                                        'version': 3},
                                                                                       {'name': 'clips',
                                                                                        'version': 1},
                                                                                       {'name': 'batCamRelay',
                                                                                        'version': 1},
                                                                                       {'name': 'batCamP2p',
                                                                                        'version': 1},
                                                                                       {'name': 'needSubscriptionServiceList',
                                                                                        'version': 1},
                                                                                       {'name': 'iotCloud',
                                                                                        'version': 1},
                                                                                       {'name': 'blockZone',
                                                                                        'version': 1},
                                                                                       {'name': 'whiteLamp',
                                                                                        'version': 1},
                                                                                       {'name': 'infLamp',
                                                                                        'version': 1},
                                                                                       {'name': 'packageDetection',
                                                                                        'version': 3},
                                                                                       {'name': 'wakeUp',
                                                                                        'version': 1},
                                                                                       {'name': 'ring',
                                                                                        'version': 1},
                                                                                       {'name': 'antiTheft',
                                                                                        'version': 3},
                                                                                       {'name': 'quickResponse',
                                                                                        'version': 2},
                                                                                       {'name': 'doorbellNightVision',
                                                                                        'version': 1},
                                                                                       {'name': 'dataDownload',
                                                                                        'version': 1},
                                                                                       {'name': 'chimeCtrl',
                                                                                        'version': 2},
                                                                                       {'name': 'detectionRegion',
                                                                                        'version': 2},
                                                                                       {'name': 'child_migrate',
                                                                                        'version': 1},
                                                                                       {'name': 'streamGrab',
                                                                                        'version': 1},
                                                                                       {'name': 'recordDownload',
                                                                                        'version': 1},
                                                                                       {'name': 'batCamPreRelay',
                                                                                        'version': 1},
                                                                                       {'name': 'batCamStatistics',
                                                                                        'version': 1},
                                                                                       {'name': 'batCamNodeRelay',
                                                                                        'version': 1},
                                                                                       {'name': 'batCamRtsp',
                                                                                        'version': 2}]}],
                                          'start_index': 0,
                                          'sum': 1}}}


@pytest.mark.parametrize("model", list(CAPTURED))
async def test_captured_response_shapes(make_house, model):
    from kasa import DeviceType
    reg, caller, _ = make_house(tier=4, responses=CAPTURED[model])
    reg.refresh = AsyncMock()
    entry = reg.resolve_one("hallway-cam")
    if model == "D230":
        entry.device.device_type = DeviceType.Doorbell
        entry.root = reg.resolve_one("tapo-h200")
    for setting in ["led", "privacy", "timezone", "clock"]:
        spec = area.READS[setting]
        data = await area.read_setting(reg, caller, "hallway-cam", setting)
        assert data == {"device": "hallway-cam", setting: area._pick(CAPTURED[model][spec.method][spec.module][spec.section], spec.fields)}
    if model != "D230":
        network = await area.read_setting(reg, caller, "hallway-cam", "wifi")
        assert network == {"device": "hallway-cam", "wifi": area._pick(CAPTURED[model]["getConnectionType"], area.READS["wifi"].fields)}
        for setting in ["microphone", "speaker"]:
            result = await area.read_setting(reg, caller, "hallway-cam", setting)
            assert result[setting]["volume"] in {"80", "98", "100"}
        await area.audio_change(reg, caller, "hallway-cam", "microphone", 0)
        expected = area._pick(CAPTURED[model]["getAudioConfig"]["audio_config"]["microphone"], ("volume", "encode_type", "sampling_rate", "bitrate", "channels"))
        expected["volume"] = "0"
        assert caller.sent[-1][2] == {"audio_config": {"microphone": expected}}
    if model in {"C225", "C610"}:
        result = await area.read_setting(reg, caller, "hallway-cam", "presets")
        expected = area._pick(CAPTURED[model]["getPresetConfig"]["preset"]["preset"], ("id", "name"))
        assert result == {"device": "hallway-cam", **({"presets": expected} if expected else {})}
    if model == "C465":
        with pytest.raises(BadRequest, match="support for ptz"):
            await area.ptz_action(reg, caller, "hallway-cam", "cruise", axis="horizontal")
    if model == "D230":
        assert "getAppComponentList" not in caller.methods_sent()
        assert "getChildDeviceComponentList" in caller.methods_sent()
        await area.system_change(reg, caller, "hallway-cam", "factory_reset", confirm="softReset hallway-cam")
        assert caller.sent[-1][1] == "softReset"
        await area.clips_change(reg, caller, "hallway-cam", 30, 5, 10)
        assert caller.sent[-1][2] == {"clips": {"clips_length": 30, "record_buffer": 5, "retrigger_time": 10}}


async def test_child_component_pagination_and_errors(make_house):
    reg, caller = house(make_house)
    entry = reg.resolve_one("hallway-cam")
    entry.root = reg.resolve_one("tapo-h200")
    def pages(_, params):
        start = params["childControl"]["start_index"]
        return {"start_index": start, "sum": 2, "child_component_list": [{"device_id": "other" if start == 0 else "cam-1", "component_list": [{"name": "ptz", "version": 1}]}]}
    caller.respond("getChildDeviceComponentList", pages)
    await area.ptz_action(reg, caller, "hallway-cam", "cruise", axis="stop")
    assert caller.sent[:2] == [("tapo-h200", "getChildDeviceComponentList", {"childControl": {"start_index": 0}}), ("tapo-h200", "getChildDeviceComponentList", {"childControl": {"start_index": 1}})]
    caller.respond("getChildDeviceComponentList", {"child_component_list": [], "sum": 0})
    with pytest.raises(BadRequest, match="did not report"):
        await area.ptz_action(reg, caller, "hallway-cam", "cruise", axis="stop")
    caller.respond("getChildDeviceComponentList", {"error_code": -1})
    with pytest.raises(DeviceUnavailable, match="getChildDeviceComponentList"):
        await area.ptz_action(reg, caller, "hallway-cam", "cruise", axis="stop")


@pytest.mark.parametrize("setting,method,module,value", [
    ("clips", "getClipsConfig", "clips", {"clips_length": 30, "record_buffer": 5, "retrigger_time": 10}),
    ("wake_up", "getWakeUpConfig", "wake_up", {"wake_up_type": "doorbell"}),
    ("pir", "getPirSensitivity", "pir_verify", {"sensitivity": "high"}),
])
async def test_legacy_battery_setting_dialects(make_house, setting, method, module, value):
    reg, caller = house(make_house, 2, components=["clips", "wakeUp", "pir"])
    caller.respond(method, {module: value})
    assert await area.read_setting(reg, caller, "hallway-cam", setting) == {"device": "hallway-cam", setting: value}
    assert caller.sent[-1][2] == {module: {}}
    if setting == "clips":
        await area.clips_change(reg, caller, "hallway-cam", 30, 5, 10)
        assert caller.sent[-1][2] == {"clips": value}
    if setting == "wake_up":
        await area.battery_change(reg, caller, "hallway-cam", "wake_up", "doorbell")
        assert caller.sent[-1][2] == {"wake_up": {"config": value}}


async def test_account_enable_requires_existing_login(make_house):
    reg, caller = system_house(make_house)
    caller.respond("getThirdAccount", {"user_management": {"third_account": {"enabled": "off", "username": ""}}})
    with pytest.raises(BadRequest, match="no existing stream login"):
        await area.system_change(reg, caller, "hallway-cam", "account", True, "setAccountEnabled hallway-cam")
    assert "setAccountEnabled" not in caller.methods_sent()


async def test_exposure_tools_warn_the_agent(make_house, mcp):
    """The agent reads the docstring: it must learn that UPnP can expose the camera and that the
    compatibility switch can cut tolpo off before it asks the owner to confirm."""
    reg, caller = full_house(make_house, 0)
    async with mcp(reg, caller) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert "internet" in tools["camera_system_upnp"].description
    assert "tolpo itself" in tools["camera_system_compatibility"].description
    assert "level 4" in tools["camera_system_compatibility"].description
    assert area.WRITES["setThirdPartyCompatibility"][0] == area.Tier.LIFECYCLE
    assert area.WRITES["setUpnpInfo"][0] >= area.Tier.SECURITY


@pytest.mark.parametrize("presets", [{"preset": {"preset": ["1"]}}, {"preset": {"preset": {"id": "1"}}}, {"preset": {}}])
async def test_preset_delete_rejects_odd_preset_shapes(make_house, presets):
    reg, caller = house(make_house)
    caller.respond("getPresetConfig", presets)
    with pytest.raises(DeviceUnavailable, match="getPresetConfig returned an unexpected response shape"):
        await area.ptz_action(reg, caller, "hallway-cam", "remove", ids=[1])
    assert "deletePreset" not in caller.methods_sent()


@pytest.mark.parametrize("setting,volume,enabled", [("microphone", None, None), ("microphone", 50, True), ("speaker", None, False), ("record_audio", None, None), ("record_audio", 10, True), ("voice_enhancement", 5, None), ("other", 5, None)])
async def test_audio_set_takes_exactly_the_matching_argument(make_house, setting, volume, enabled):
    reg, caller = audio_house(make_house)
    with pytest.raises(BadRequest):
        await area.audio_set(reg, caller, "hallway-cam", setting, volume, enabled)
    assert not any(m in area.WRITES for m in caller.methods_sent())


@pytest.mark.parametrize("value,sent", [("on", 1), ("off", 0)])
async def test_image_mode_hdr_takes_on_or_off(make_house, value, sent):
    reg, caller = image_house(make_house, 2)
    await area.image_mode(reg, caller, "hallway-cam", "hdr", value)
    assert caller.sent[-1] == ("hallway-cam", "setHDR", {"video": {"set_hdr": {"secname": "main", "hdr": sent}}})
    for bad_setting, bad_value in [("hdr", "true"), ("hdr", "1"), ("led", "off"), ("privacy", "off")]:
        with pytest.raises(BadRequest):
            await area.image_mode(reg, caller, "hallway-cam", bad_setting, bad_value)
    assert caller.methods_sent().count("setHDR") == 1


async def test_merged_tools_keep_their_levels(make_house, mcp):
    """Merging tools must not move any method to another level or drop a confirmation."""
    reg, caller = full_house(make_house, 1)
    async with mcp(reg, caller) as client:
        for name, arguments, method in [case for case in TOOLS if case[0] in {"camera_audio_set", "camera_battery_mode", "camera_image_mode"}]:
            result = await client.call_tool(name, {"device": "hallway-cam", **arguments})
            assert result.is_error and "level 2" in result.content[0].text, (name, arguments)
            assert method not in caller.methods_sent()
