"""Hub area tests use simulated devices and the real method policy."""

from dataclasses import is_dataclass

from kasa import DeviceType, Module
from kasa.exceptions import DeviceError
import pytest

from tolpo.areas import hub
from tolpo.calls import NeedsConfirmation
from tolpo.registry import DeviceUnavailable, Forbidden, RegistryError

from ..fakes import FakeDevice

pytestmark = pytest.mark.anyio


def event_house(make_house, **kwargs):
    reg, caller, devices = make_house(**kwargs)
    for child in devices["hub"].children:
        child.modules[Module.TriggerLogs] = object()
    return reg, caller, devices


async def test_events_are_typed_live_sorted_filtered_and_compact(make_house, mcp):
    reg, caller, _ = event_house(make_house, tier=0)
    first = {"id": 1, "eventId": "e1", "timestamp": 1700000000, "event": "open\n now",
             "params": {"password": "SECRET", "rotate_deg": -20}, "mac": "SECRET"}
    caller.respond("get_trigger_logs", {"logs": [first, first]}, slug="main-door")
    caller.respond("get_trigger_logs", {"logs": [dict(first, id=2, timestamp=1700000001, event="rotation")]}, slug="temp-sensor")
    events = await hub.recent_events(reg, caller, "tapo-h200", limit=1)
    assert len(events) == 1 and is_dataclass(events[0]) and events[0].device == "temp-sensor"
    assert events[0].rotate_deg == -20
    async with mcp(reg, caller) as client:
        result = await client.call_tool("hub_events", {"hub": "tapo-h200", "device": "main-door"})
    assert result.structured_content == {"hub": "tapo-h200", "events": [
        {"device": "main-door", "id": 1, "timestamp": 1700000000, "time": "2023-11-14T22:13:20+00:00",
         "event": "open now", "event_id": "e1", "rotate_deg": -20}]}
    assert all(params == {"start_id": 0, "page_size": 100} for _, _, params in caller.sent)
    assert await hub.recent_events(reg, caller, "tapo-h200", after=1700000001) == []


async def test_events_reject_other_hubs_unsupported_children_and_bad_input(make_house):
    reg, caller, _ = event_house(make_house)
    for kwargs in ({"device": "desk-lamp"}, {"limit": 0}, {"limit": True}, {"after": -1}):
        with pytest.raises(RegistryError):
            await hub.recent_events(reg, caller, "tapo-h200", **kwargs)
    with pytest.raises(RegistryError):
        await hub.recent_events(reg, caller, "desk-lamp")
    assert caller.sent == []


async def test_events_skip_children_without_log_and_do_not_swallow_errors(make_house, mcp):
    reg, caller, _ = make_house(tier=0)
    assert await hub.recent_events(reg, caller, "tapo-h200") == []
    with pytest.raises(RegistryError, match="no trigger log"):
        await hub.recent_events(reg, caller, "tapo-h200", device="main-door")
    assert caller.sent == []
    reg.resolve_one("main-door").device.modules[Module.TriggerLogs] = object()
    caller.respond("get_trigger_logs", DeviceError("failed", error_code=None))
    with pytest.raises(DeviceUnavailable, match="main-door reported an error"):
        await hub.recent_events(reg, caller, "tapo-h200")
    async with mcp(reg, caller) as client:
        result = await client.call_tool("hub_events", {"hub": "tapo-h200"})
    assert result.is_error and "main-door" in result.content[0].text


@pytest.mark.parametrize("logs", [None, {}, [None], [{"id": 1, "timestamp": -1, "event": "open"}]])
async def test_malformed_logs_are_errors(make_house, logs):
    reg, caller, _ = event_house(make_house)
    caller.respond("get_trigger_logs", {"logs": logs})
    with pytest.raises(RegistryError, match="invalid"):
        await hub.recent_events(reg, caller, "tapo-h200")


def alarm_house(make_house, family, **kwargs):
    reg, caller, devices = make_house(**kwargs)
    devices["hub"].model = family
    caller.respond("get_alarm_configure", {"type": "Doorbell Ring 1", "volume": "normal", "duration": 10, "password": "SECRET"})
    caller.respond("get_support_alarm_type_list", {"alarm_type_list": ["Doorbell Ring 1", "Alarm 1"], "mac": "SECRET"})
    caller.respond("getSirenConfig", {"siren_type": "Alarm 1", "volume": "5", "duration": 10, "password": "SECRET"})
    caller.respond("getSirenTypeList", {"siren_type_list": ["Alarm 1", "Doorbell Ring 1"], "ssid": "SECRET"})
    caller.respond("getSirenStatus", {"status": "off", "token": "SECRET"})
    return reg, caller, devices


@pytest.mark.parametrize("family", ["H100", "H200"])
async def test_alarm_read_settings_write_refresh_and_compact(make_house, mcp, monkeypatch, family):
    reg, caller, _ = alarm_house(make_house, family, tier=2)
    refreshed = []
    async def refresh(entry, *, force=False):
        refreshed.append((entry.slug, force))
    monkeypatch.setattr(reg, "refresh", refresh)
    async with mcp(reg, caller) as client:
        read = await client.call_tool("hub_alarm_config", {"device": "tapo-h200"})
        result = await client.call_tool("hub_alarm_set", {"device": "tapo-h200", "sound": "Alarm 1",
                                                          "volume": 7 if family == "H200" else "high", "duration": 30})
    assert not read.is_error and "SECRET" not in str(read.structured_content)
    expected = {"siren_type": "Alarm 1", "volume": "7", "duration": 30} if family == "H200" else {
        "type": "Alarm 1", "volume": "high", "duration": 30}
    assert result.structured_content == {"device": "tapo-h200", "changed": expected}
    assert caller.sent[-1] == ("tapo-h200", "setSirenConfig" if family == "H200" else "set_alarm_configure",
                              {"siren": expected} if family == "H200" else expected)
    assert refreshed[-1] == ("tapo-h200", True)


@pytest.mark.parametrize("family,method,enabled", [("H100", "play_alarm", True), ("H100", "stop_alarm", False),
                                                  ("H200", "setSirenStatus", True), ("H200", "setSirenStatus", False)])
async def test_siren_levels_confirmation_and_wire(make_house, mcp, family, method, enabled):
    reg, caller, _ = alarm_house(make_house, family, tier=2)
    with pytest.raises(Forbidden, match="level 3"):
        await hub.sound_siren(reg, caller, "tapo-h200", enabled, confirm=f"{method} tapo-h200")
    assert caller.sent == []
    reg, caller, _ = alarm_house(make_house, family, tier=3)
    for phrase in (None, f"{method} other", "play_alarm tapo-h200" if family == "H200" else "setSirenStatus tapo-h200"):
        with pytest.raises(NeedsConfirmation):
            await hub.sound_siren(reg, caller, "tapo-h200", enabled, confirm=phrase)
    assert caller.sent == []
    async with mcp(reg, caller) as client:
        result = await client.call_tool("hub_siren", {"device": "tapo-h200", "enabled": enabled,
                                                      "confirm": f"{method} tapo-h200"})
    assert result.structured_content == {"device": "tapo-h200", "changed": {"siren": enabled}}
    assert caller.sent == [("tapo-h200", method, {"siren": {"status": "on" if enabled else "off"}} if family == "H200" else {})]


@pytest.mark.parametrize("family", ["H100", "H200"])
async def test_alarm_settings_denied_and_device_errors(make_house, family):
    reg, caller, _ = alarm_house(make_house, family, tier=1)
    with pytest.raises(Forbidden, match="level 2"):
        await hub.set_alarm(reg, caller, "tapo-h200", duration=20)
    assert "setSirenConfig" not in caller.methods_sent() and "set_alarm_configure" not in caller.methods_sent()
    reg, caller, _ = alarm_house(make_house, family, tier=3)
    for method, action in (("getSirenConfig" if family == "H200" else "get_alarm_configure",
                            lambda: hub.alarm_config(reg, caller, "tapo-h200")),
                           ("setSirenConfig" if family == "H200" else "set_alarm_configure",
                            lambda: hub.set_alarm(reg, caller, "tapo-h200", duration=20)),
                           ("setSirenStatus" if family == "H200" else "play_alarm",
                            lambda: hub.sound_siren(reg, caller, "tapo-h200", True,
                                                   confirm=f"{'setSirenStatus' if family == 'H200' else 'play_alarm'} tapo-h200"))):
        old = dict(caller.responses)
        caller.respond(method, DeviceError("failed", error_code=None))
        with pytest.raises(DeviceUnavailable, match="tapo-h200 reported an error"):
            await action()
        caller.responses = old


@pytest.mark.parametrize("family,kwargs", [("H100", {"duration": 0}), ("H200", {"duration": 6001}),
                                           ("H100", {"volume": 5}), ("H200", {"volume": "high"}),
                                           ("H100", {"volume": "mute"}), ("H200", {"volume": 0}), ("H200", {"duration": 0}),
                                           ("H200", {"volume": True}), ("H100", {}), ("H200", {"sound": "invented"})])
async def test_invalid_alarm_settings_never_write(make_house, family, kwargs):
    reg, caller, _ = alarm_house(make_house, family, tier=3)
    with pytest.raises(RegistryError):
        await hub.set_alarm(reg, caller, "tapo-h200", **kwargs)
    assert not any(method.startswith("set") for method in caller.methods_sent())


def sensor_house(make_house, **kwargs):
    reg, caller, devices = make_house(**kwargs)
    devices["hub"].children += [FakeDevice("motion", "Motion Sensor", "T100", DeviceType.Sensor),
                                FakeDevice("leak", "Leak Sensor", "T300", DeviceType.Sensor)]
    reg._adopt(devices["hub"])
    return reg, caller, devices


SENSOR_WRITES = [
    ("sensor_sensitivity", "motion-sensor", "set_sensitivity_info", {"sensitivity": "high", "report_interval": 30},
     {"sensitivity": "high", "report_interval": 30}),
    ("sensor_comfort_temperature", "temp-sensor", "set_comfort_temp_config",
     {"minimum": 18, "maximum": 24, "unit": "celsius", "notification": True},
     {"min_temp": 18, "max_temp": 24, "temp_unit": "celsius", "notification": True}),
    ("sensor_comfort_humidity", "temp-sensor", "set_comfort_humidity_config", {"minimum": 30, "maximum": 60},
     {"min_humidity": 30, "max_humidity": 60}),
    ("sensor_mode", "temp-sensor", "set_report_mode", {"mode": "long_battery_life"}, {"report_mode": "long_battery_life"}),
    ("sensor_led", "main-door", "set_led_control", {"enabled": True}, {"led_onoff": 1}),
]


@pytest.mark.parametrize("tool,device,method,args,params", SENSOR_WRITES)
async def test_sensor_writes_happy_refusal_errors_and_refresh(make_house, mcp, monkeypatch, tool, device, method, args, params):
    for tier in (1, 2):
        reg, caller, _ = sensor_house(make_house, tier=tier)
        refreshed = []
        async def refresh(entry, *, force=False):
            refreshed.append((entry.slug, force))
        monkeypatch.setattr(reg, "refresh", refresh)
        caller.respond(method, {"password": "SECRET"})
        async with mcp(reg, caller) as client:
            result = await client.call_tool(tool, dict(device=device, **args))
            if tier == 2:
                caller.respond(method, DeviceError("failed", error_code=None))
                error = await client.call_tool(tool, dict(device=device, **args))
                assert error.is_error and device in error.content[0].text
        if tier == 1:
            assert result.is_error and "level 2" in result.content[0].text
            assert "allow_methods" not in result.content[0].text and not caller.sent and not refreshed
        else:
            assert result.structured_content == {"device": device, "changed": params}
            assert caller.sent[0] == (device, method, params)
            assert refreshed == [(device, True)]


@pytest.mark.parametrize("setting,device", [("sensitivity", "motion-sensor"), ("alarm", "leak-sensor"),
                                           ("comfort_temperature", "temp-sensor"), ("comfort_humidity", "temp-sensor"),
                                           ("report_mode", "temp-sensor"), ("led", "main-door")])
async def test_sensor_setting_reads_only_public_fields_and_reports_errors(make_house, mcp, setting, device):
    reg, caller, _ = sensor_house(make_house, tier=0)
    method, _, _, fields = hub.SENSOR_SETTINGS[setting]
    field = fields[0]
    caller.respond(method, {field: "normal\n value", "password": "SECRET", "ssid": "SECRET", "token": "SECRET"})
    async with mcp(reg, caller) as client:
        result = await client.call_tool("sensor_settings", {"device": device, "setting": setting})
        caller.respond(method, DeviceError("failed", error_code=None))
        error = await client.call_tool("sensor_settings", {"device": device, "setting": setting})
    assert result.structured_content == {"device": device, "setting": setting, "values": {field: "normal value"}}
    assert caller.sent[0] == (device, method, {})
    assert error.is_error and device in error.content[0].text


async def test_sensor_siren_confirmed_and_denied(make_house, mcp):
    reg, caller, _ = sensor_house(make_house, tier=2)
    with pytest.raises(Forbidden, match="level 3"):
        await hub.test_sensor_alarm(reg, caller, "leak-sensor", "high", confirm="play_alarm leak-sensor")
    assert caller.sent == []
    reg, caller, _ = sensor_house(make_house, tier=3)
    for phrase in (None, "play_alarm tapo-h200"):
        with pytest.raises(NeedsConfirmation):
            await hub.test_sensor_alarm(reg, caller, "leak-sensor", "high", confirm=phrase)
    assert caller.sent == []
    async with mcp(reg, caller) as client:
        result = await client.call_tool("sensor_siren", {"device": "leak-sensor", "volume": "high",
                                                         "confirm": "play_alarm leak-sensor"})
        caller.respond("play_alarm", DeviceError("failed", error_code=None))
        error = await client.call_tool("sensor_siren", {"device": "leak-sensor", "volume": "high",
                                                        "confirm": "play_alarm leak-sensor"})
    assert result.structured_content == {"device": "leak-sensor", "changed": {"siren_test": True, "volume": "high"}}
    assert caller.sent[0] == ("leak-sensor", "play_alarm", {"volume": "high"})
    assert error.is_error and "leak-sensor" in error.content[0].text


async def test_sensor_input_validation_and_wrong_models(make_house):
    reg, caller, _ = sensor_house(make_house, tier=3)
    actions = [lambda: hub.set_sensitivity(reg, caller, "motion-sensor", sensitivity="maximum"),
               lambda: hub.set_sensitivity(reg, caller, "motion-sensor", report_interval=0),
               lambda: hub.set_sensitivity(reg, caller, "motion-sensor"),
               lambda: hub.set_sensitivity(reg, caller, "main-door", sensitivity="low"),
               lambda: hub.set_sensor_alarm(reg, caller, "leak-sensor", "LOUD"),
               lambda: hub.set_sensor_alarm(reg, caller, "leak-sensor", "high", duration=-1),
               lambda: hub.test_sensor_alarm(reg, caller, "leak-sensor", "mute"),
               lambda: hub.set_comfort_temperature(reg, caller, "temp-sensor", 30, 20, "celsius"),
               lambda: hub.set_comfort_temperature(reg, caller, "temp-sensor", float("nan"), 20, "celsius"),
               lambda: hub.set_comfort_temperature(reg, caller, "temp-sensor", 10, 20, "kelvin"),
               lambda: hub.set_comfort_humidity(reg, caller, "temp-sensor", 60, 30),
               lambda: hub.set_comfort_humidity(reg, caller, "temp-sensor", -1, 60),
               lambda: hub.set_sensor_mode(reg, caller, "temp-sensor", "fast"),
               lambda: hub.set_sensor_led(reg, caller, "desk-lamp", True),
               lambda: hub.set_sensor_led(reg, caller, "main-door", 1),
               lambda: hub.sensor_setting(reg, caller, "main-door", "unknown")]
    for action in actions:
        with pytest.raises(RegistryError):
            await action()
    assert caller.sent == []


def valve_house(make_house, **kwargs):
    reg, caller, devices = sensor_house(make_house, **kwargs)
    valve = FakeDevice("valve", "Radiator", "KE100", DeviceType.Thermostat)
    valve.sys_info = {"min_control_temp": 5, "max_control_temp": 30, "temp_unit": "celsius"}
    devices["hub"].children.append(valve)
    reg._adopt(devices["hub"])
    return reg, caller, devices


VALVE_WRITES = [
    ("valve_timed_set", "set_boost_info", {"mode": "boost", "enabled": True, "seconds": 600}, {"enable": True, "boost_time": 600}),
    ("valve_timed_set", "set_holdon_info", {"mode": "hold", "enabled": False}, {"enable": False}),
    ("valve_defaults_set", "set_default_boost_info_v2", {"mode": "boost", "seconds": 600, "temperature": 25},
     {"default_boost_time": 600, "default_boost_temp": 25}),
    ("valve_defaults_set", "set_default_holdon_info", {"mode": "hold", "seconds": 1200}, {"default_hold_on_time": 1200}),
    ("valve_window_set", "set_window_open_detection", {"enabled": True, "action": "shutoff_only", "sensors": ["main-door"]},
     {"enable": True, "detected_action": "shutoff_only", "contact_sensor_list": ["child-door"]}),
    ("valve_maintenance_start", "progress_calibration", {"operation": "calibrate"}, {}),
    ("valve_maintenance_start", "remove_scale", {"operation": "descale"}, {}),
    ("valve_option_set", "set_remove_scale_info", {"option": "auto_descaling", "enabled": True}, {"auto": True}),
    ("valve_work_mode_set", "set_trv_mode", {"mode": "silent_mode"}, {"trv_mode": "silent_mode"}),
    ("valve_night_set", "set_night_mode_info", {"enabled": True, "start_min": 1320, "end_min": 420},
     {"enable": True, "effective_time": {"type": "custom", "start_min": 1320, "end_min": 420}}),
    ("valve_option_set", "set_early_start", {"option": "early_start", "enabled": True}, {"enable": True}),
    ("valve_schedule_set", "set_sc_schedule_info", {"enabled": True, "mode": "everyday"}, {"enable": True, "mode": "everyday"}),
]


@pytest.mark.parametrize("tool,method,args,params", VALVE_WRITES)
async def test_valve_writes_happy_refusal_errors_and_refresh(make_house, mcp, monkeypatch, tool, method, args, params):
    for tier in (1, 2):
        reg, caller, _ = valve_house(make_house, tier=tier)
        refreshed = []
        async def refresh(entry, *, force=False):
            refreshed.append((entry.slug, force))
        monkeypatch.setattr(reg, "refresh", refresh)
        caller.respond(method, {"password": "SECRET"})
        async with mcp(reg, caller) as client:
            result = await client.call_tool(tool, dict(device="radiator", **args))
            if tier == 2:
                caller.respond(method, DeviceError("failed", error_code=None))
                error = await client.call_tool(tool, dict(device="radiator", **args))
                assert error.is_error and "radiator" in error.content[0].text
        if tier == 1:
            assert result.is_error and "level 2" in result.content[0].text
            assert caller.sent == [] and refreshed == []
        else:
            assert not result.is_error and result.structured_content["device"] == "radiator"
            assert "SECRET" not in str(result.structured_content) and "child-door" not in str(result.structured_content)
            assert caller.sent[0] == ("radiator", method, params)
            assert refreshed == [("radiator", True)]


@pytest.mark.parametrize("setting", list(hub.VALVE_SETTINGS))
async def test_valve_setting_reads_errors_and_hidden_fields(make_house, mcp, setting):
    reg, caller, _ = valve_house(make_house, tier=0)
    method, fields = hub.VALVE_SETTINGS[setting]
    field = fields[0]
    data = {field: "public\n value", "mac": "SECRET", "mfi_setup_code": "SECRET", "password": "SECRET"}
    if setting == "window":
        data["contact_sensor_list"] = ["child-door", "SECRET"]
    if setting == "night_mode":
        data["effective_time"] = {"type": "custom", "start_min": 1320, "end_min": 420, "token": "SECRET"}
    caller.respond(method, data)
    async with mcp(reg, caller) as client:
        result = await client.call_tool("valve_settings", {"device": "radiator", "setting": setting})
        caller.respond(method, DeviceError("failed", error_code=None))
        error = await client.call_tool("valve_settings", {"device": "radiator", "setting": setting})
    assert not result.is_error and "SECRET" not in str(result.structured_content)
    assert result.structured_content["values"][field] == "public value"
    assert caller.sent[0] == ("radiator", method, {})
    assert error.is_error and "radiator" in error.content[0].text


@pytest.mark.parametrize("tool,method,args", [
    ("valve_frost_set", "set_frost_protection", {"minimum": 5, "unit": "celsius"}),
    ("valve_shutdown_set", "set_shutdown_info", {"enabled": True, "mode": "full_closed", "anti_clogging": True}),
])
async def test_valve_protections_require_level_and_confirmation(make_house, mcp, monkeypatch, tool, method, args):
    for tier in (2, 3):
        reg, caller, _ = valve_house(make_house, tier=tier)
        async with mcp(reg, caller) as client:
            for phrase in (None, f"{method} other"):
                result = await client.call_tool(tool, dict(device="radiator", confirm=phrase, **args))
                assert result.is_error
            assert caller.sent == []
            result = await client.call_tool(tool, dict(device="radiator", confirm=f"{method} radiator", **args))
            if tier == 2:
                assert result.is_error and "level 3" in result.content[0].text and caller.sent == []
            else:
                assert not result.is_error and result.structured_content["device"] == "radiator"
                caller.respond(method, DeviceError("failed", error_code=None))
                error = await client.call_tool(tool, dict(device="radiator", confirm=f"{method} radiator", **args))
                assert error.is_error and "radiator" in error.content[0].text


async def test_valve_inputs_and_contact_hub_membership(make_house):
    reg, caller, devices = valve_house(make_house, tier=3)
    other_door = FakeDevice("other-child", "Other Door", "T110", DeviceType.Sensor)
    other_hub = FakeDevice("other-hub", "Other Hub", "H100", DeviceType.Hub, children=[other_door])
    reg._adopt(other_hub)
    actions = [lambda: hub.valve_timed(reg, caller, "radiator", "boost", True),
               lambda: hub.valve_timed(reg, caller, "radiator", "hold", True, 0),
               lambda: hub.valve_timed(reg, caller, "radiator", "unknown", True, 10),
               lambda: hub.valve_defaults(reg, caller, "radiator", "boost", 600, 31),
               lambda: hub.valve_defaults(reg, caller, "radiator", "hold", 600, 20),
               lambda: hub.valve_window(reg, caller, "radiator", True, "shutoff_only", ["other-door"]),
               lambda: hub.valve_window(reg, caller, "radiator", True, "shutoff_only", ["temp-sensor"]),
               lambda: hub.valve_window(reg, caller, "radiator", True, "unknown"),
               lambda: hub.valve_night(reg, caller, "radiator", True, 1400, None),
               lambda: hub.valve_night(reg, caller, "radiator", True, 1440, 600),
               lambda: hub.valve_night(reg, caller, "radiator", True, 600, 600),
               lambda: hub.valve_frost(reg, caller, "radiator", -1, "celsius", confirm="set_frost_protection radiator"),
               lambda: hub.valve_frost(reg, caller, "radiator", 4, "celsius", confirm="set_frost_protection radiator"),
               lambda: hub.valve_frost(reg, caller, "radiator", 16, "celsius", confirm="set_frost_protection radiator"),
               lambda: hub.valve_frost(reg, caller, "radiator", 40, "fahrenheit", confirm="set_frost_protection radiator"),
               lambda: hub.valve_frost(reg, caller, "radiator", 60, "fahrenheit", confirm="set_frost_protection radiator"),
               lambda: hub.valve_frost(reg, caller, "radiator", 10, "kelvin", confirm="set_frost_protection radiator"),
               lambda: hub.valve_shutdown(reg, caller, "radiator", True, "unknown"),
               lambda: hub.valve_work_mode(reg, caller, "radiator", "unknown"),
               lambda: hub.valve_maintenance(reg, caller, "radiator", "unknown"),
               lambda: hub.valve_schedule_mode(reg, caller, "radiator", True, "unknown"),
               lambda: hub.valve_early(reg, caller, "main-door", True)]
    for action in actions:
        with pytest.raises(RegistryError):
            await action()
    assert caller.sent == []


async def test_valve_schedule_read_is_compact_and_preserves_encoding(make_house, mcp):
    reg, caller, _ = valve_house(make_house, tier=0)
    caller.respond("get_sc_schedule_rules", {"schedule_mode": "everyday", "day_segment": 4, "password": "SECRET",
        "schedule_list": [{"id": "day\n 1", "token": "SECRET", "rule_list": [
            {"start_min": 0, "end_min": 1440, "desired_states": {"target_temp": 20, "target_temp_float": 5,
              "temp_unit": "celsius", "frost_protection_on": False, "mac": "SECRET"}}]}]})
    async with mcp(reg, caller) as client:
        result = await client.call_tool("valve_schedule", {"device": "radiator", "mode": "everyday"})
        caller.respond("get_sc_schedule_rules", DeviceError("failed", error_code=None))
        error = await client.call_tool("valve_schedule", {"device": "radiator", "mode": "everyday"})
    assert not result.is_error and "SECRET" not in str(result.structured_content)
    assert result.structured_content["schedules"][0]["id"] == "day 1"
    assert result.structured_content["schedules"][0]["rules"][0]["desired_states"]["target_temp_float"] == 5
    assert caller.sent[0] == ("radiator", "get_sc_schedule_rules", {"schedule_mode": "everyday", "start_index": 0})
    assert error.is_error and "radiator" in error.content[0].text


@pytest.mark.parametrize("tool,device,method,array,meta", [
    ("hub_child_history", "temp-sensor", "get_temp_humidity_records", "past24h_temp", {"temp_unit": "celsius", "local_time": 1700000000}),
    ("hub_child_history", "radiator", "get_temperature_records", "today", {"current_temp": 20.5, "local_time": 1700000000}),
])
async def test_local_history_numeric_compact_and_errors(make_house, mcp, tool, device, method, array, meta):
    reg, caller, _ = valve_house(make_house, tier=0)
    caller.respond(method, {array: [10, 20, None, 30], **meta, "password": "SECRET", "mac": "SECRET"})
    async with mcp(reg, caller) as client:
        result = await client.call_tool(tool, {"device": device, "max_points": 3})
        assert result.structured_content == {"device": device, **meta, array: [10, 20, None], "omitted": {array: 1}}
        caller.respond(method, {array: ["SECRET"]})
        error = await client.call_tool(tool, {"device": device})
        assert error.is_error and "invalid history values" in error.content[0].text
        caller.respond(method, DeviceError("failed", error_code=None))
        error = await client.call_tool(tool, {"device": device})
        assert error.is_error and device in error.content[0].text
    assert caller.sent[0] == (device, method, {})


async def test_history_input_validation_and_missing_arrays(make_house):
    reg, caller, _ = valve_house(make_house)
    with pytest.raises(RegistryError):
        await hub.local_history(reg, caller, "main-door")
    with pytest.raises(RegistryError):
        await hub.local_history(reg, caller, "temp-sensor", max_points=0)
    assert caller.sent == []
    with pytest.raises(RegistryError, match="invalid local history"):
        await hub.local_history(reg, caller, "temp-sensor")


async def test_child_rename_base64_refreshed_alias_and_errors(make_house, mcp, monkeypatch):
    import base64
    reg, caller, _ = make_house(tier=2)
    entry = reg.resolve_one("main-door")
    refreshes = []
    async def refresh(entry, *, force=False):
        refreshes.append(force)
        entry.device.alias = "New Door"
    monkeypatch.setattr(reg, "refresh", refresh)
    async with mcp(reg, caller) as client:
        result = await client.call_tool("hub_child_rename", {"device": "main-door", "name": "New Door"})
        caller.respond("set_device_info", DeviceError("failed", error_code=None))
        error = await client.call_tool("hub_child_rename", {"device": "main-door", "name": "Other Door"})
    assert result.structured_content == {"device": "main-door", "changed": {"name": "New Door"}}
    assert caller.sent[0] == ("main-door", "set_device_info", {"nickname": base64.b64encode(b"New Door").decode()})
    assert refreshes == [True] and reg.resolve_one("New Door") is entry and entry.slug == "main-door"
    assert error.is_error and "main-door" in error.content[0].text
    reg, caller, _ = make_house(tier=1)
    with pytest.raises(Forbidden, match="level 2"):
        await hub.rename_child(reg, caller, "main-door", "New Door")
    assert caller.sent == []
    reg, caller, _ = make_house(tier=2)
    for name in ("", "a" * 65, "name\n two", " name "):
        with pytest.raises(RegistryError):
            await hub.rename_child(reg, caller, "main-door", name)
    with pytest.raises(RegistryError):
        await hub.rename_child(reg, caller, "desk-lamp", "New Lamp")
    assert caller.sent == []


@pytest.mark.parametrize("family", ["H100", "H200"])
async def test_hub_led_read_write_refusal_errors_and_preserved_fields(make_house, mcp, monkeypatch, family):
    reg, caller, _ = alarm_house(make_house, family, tier=2)
    current = {"led_rule": "night_mode", "start_time": 1300, "end_time": 400, "night_mode_type": "custom",
               "sunrise_offset": 0, "sunset_offset": 0}
    read_method = "getLedStatus" if family == "H200" else "get_led_info"
    write_method = "setLedStatus" if family == "H200" else "set_led_info"
    caller.respond(read_method, {"led": {"config": {"enabled": "on", "password": "SECRET"}}} if family == "H200" else
                   dict(current, password="SECRET"))
    refreshed = []
    async def refresh(entry, *, force=False):
        refreshed.append(force)
    monkeypatch.setattr(reg, "refresh", refresh)
    async with mcp(reg, caller) as client:
        read = await client.call_tool("hub_led", {"device": "tapo-h200"})
        result = await client.call_tool("hub_led_set", {"device": "tapo-h200", "enabled": False})
        caller.respond(write_method, DeviceError("failed", error_code=None))
        error = await client.call_tool("hub_led_set", {"device": "tapo-h200", "enabled": True})
        assert error.is_error and "tapo-h200" in error.content[0].text
        caller.respond(read_method, DeviceError("failed", error_code=None))
        error = await client.call_tool("hub_led", {"device": "tapo-h200"})
        assert error.is_error and "tapo-h200" in error.content[0].text
    assert not read.is_error and "SECRET" not in str(read.structured_content)
    assert result.structured_content == {"device": "tapo-h200", "changed": {"led": False}}
    writes = [(slug, method, params) for slug, method, params in caller.sent if method == write_method]
    assert writes[0] == ("tapo-h200", write_method, {"led": {"config": {"enabled": "off"}}} if family == "H200" else
                         dict(current, led_rule="never"))
    assert refreshed[-1] is True
    reg, caller, _ = alarm_house(make_house, family, tier=1)
    caller.respond("get_led_info", current)
    with pytest.raises(Forbidden, match="level 2"):
        await hub.hub_led_setting(reg, caller, "tapo-h200", True)
    assert write_method not in caller.methods_sent()


@pytest.mark.parametrize("family", ["H100", "H200"])
async def test_hub_diagnostics_compact_load_only_and_errors(make_house, mcp, family):
    reg, caller, _ = alarm_house(make_house, family, tier=0)
    method = "getDeviceLoadInfo" if family == "H200" else "get_device_load_info"
    data = {"cur_load_num": 2, "max_load_num": 64, "load_level": "light\n load", "used_memory": 1024,
            "password": "SECRET", "mac": "SECRET", "ssid": "SECRET", "homekit_setup_code": "SECRET"}
    caller.respond(method, {"deviceLoad": data} if family == "H200" else data)
    async with mcp(reg, caller) as client:
        result = await client.call_tool("hub_diagnostics_read", {"device": "tapo-h200"})
        caller.respond(method, DeviceError("failed", error_code=None))
        error = await client.call_tool("hub_diagnostics_read", {"device": "tapo-h200"})
    assert result.structured_content == {"device": "tapo-h200", "cur_load_num": 2, "max_load_num": 64,
                                         "load_level": "light load", "used_memory": 1024}
    assert caller.sent[0] == ("tapo-h200", method, {"deviceLoad": {}} if family == "H200" else {})
    assert error.is_error and "tapo-h200" in error.content[0].text


async def test_successful_write_with_failed_state_refresh_reports_stale_state(make_house, monkeypatch):
    reg, caller, _ = sensor_house(make_house, tier=2)
    async def refresh(entry, *, force=False):
        entry.online, entry.error = False, "TimeoutError"
    monkeypatch.setattr(reg, "refresh", refresh)
    assert await hub.set_sensor_led(reg, caller, "main-door", True) == {
        "device": "main-door", "changed": {"led_onoff": 1}, "state_refreshed": False, "reason": "TimeoutError"}


async def test_sanitized_h200_and_child_fixture_shapes(make_house, mcp):
    """Public response bodies from fixtures/hub/H200 and sensor/{T110,T300,T310}.

    Keep only method bodies without identities. Nonempty event bodies are audit-based synthetic records.
    """
    reg, caller, devices = sensor_house(make_house, tier=0)
    for child in devices["hub"].children:
        child.modules[Module.TriggerLogs] = object()
    tones = [f"Doorbell Ring {n}" for n in range(1, 11)] + ["Phone Ring", "Alarm 1", "Alarm 2", "Alarm 3", "Alarm 4",
                                                                "Dripping Tap", "Alarm 5", "Connection 1", "Connection 2"]
    caller.respond("getSirenConfig", {"duration": 300, "siren_type": "Doorbell Ring 10", "volume": "6"})
    caller.respond("getSirenStatus", {"status": "off", "time_left": 0})
    caller.respond("getSirenTypeList", {"siren_type_list": tones})
    caller.respond("getLedStatus", {"led": {"config": {".name": "config", ".type": "led", "enabled": "on"}}})
    caller.respond("get_trigger_logs", {"logs": [], "start_id": 0, "sum": 0})
    async with mcp(reg, caller) as client:
        config = await client.call_tool("hub_alarm_config", {"device": "tapo-h200"})
        led = await client.call_tool("hub_led", {"device": "tapo-h200"})
        events = await client.call_tool("hub_events", {"hub": "tapo-h200"})
    assert config.structured_content == {"device": "tapo-h200", "settings": {"siren_type": "Doorbell Ring 10", "volume": "6",
                                                                            "duration": 300}, "tones": tones, "status": "off"}
    assert led.structured_content == {"device": "tapo-h200", "settings": {"enabled": "on"}}
    assert events.structured_content == {"hub": "tapo-h200", "events": []}


@pytest.mark.parametrize("family,method", [("H100", "get_support_alarm_type_list"),
                                          ("H200", "getSirenTypeList"), ("H200", "getSirenStatus"),
                                          ("H100", "stop_alarm")])
async def test_remaining_alarm_methods_report_device_errors(make_house, family, method):
    reg, caller, _ = alarm_house(make_house, family, tier=3)
    caller.respond(method, DeviceError("failed", error_code=None))
    with pytest.raises(DeviceUnavailable, match="tapo-h200 reported an error"):
        if method == "stop_alarm":
            await hub.sound_siren(reg, caller, "tapo-h200", False, confirm="stop_alarm tapo-h200")
        else:
            await hub.alarm_config(reg, caller, "tapo-h200")


async def test_events_never_poll_children_on_another_hub(make_house):
    reg, caller, _ = event_house(make_house)
    child = FakeDevice("other-child", "Other Sensor", "T110", DeviceType.Sensor)
    child.modules[Module.TriggerLogs] = object()
    parent = FakeDevice("other-hub", "Other Hub", "H100", DeviceType.Hub, children=[child])
    reg._adopt(parent)
    caller.respond("get_trigger_logs", {"logs": [], "start_id": 0, "sum": 0})
    assert await hub.recent_events(reg, caller, "tapo-h200") == []
    assert {slug for slug, _, _ in caller.sent} == {"main-door", "temp-sensor"}
    caller.sent.clear()
    with pytest.raises(RegistryError, match="no trigger log on"):
        await hub.recent_events(reg, caller, "tapo-h200", device="other-sensor")
    assert caller.sent == []


@pytest.mark.parametrize("minimum,unit", [(5, "celsius"), (15, "celsius"), (41, "fahrenheit"), (59, "fahrenheit")])
async def test_frost_minimum_accepts_the_app_range(make_house, minimum, unit):
    reg, caller, _ = valve_house(make_house, tier=3)
    result = await hub.valve_frost(reg, caller, "radiator", minimum, unit, confirm="set_frost_protection radiator")
    assert result["changed"] == {"min_temp": minimum, "temp_unit": unit}
    assert caller.sent == [("radiator", "set_frost_protection", {"min_temp": minimum, "temp_unit": unit})]


async def test_leak_siren_settings_are_level_3_with_confirmation(make_house, mcp, monkeypatch):
    args = {"device": "leak-sensor", "volume": "mute", "duration": 20}
    reg, caller, _ = sensor_house(make_house, tier=2)
    async with mcp(reg, caller) as client:
        result = await client.call_tool("sensor_alarm_set", dict(args, confirm="set_alarm_config leak-sensor"))
    assert result.is_error and "level 3" in result.content[0].text and caller.sent == []
    reg, caller, _ = sensor_house(make_house, tier=3)
    refreshed = []
    async def refresh(entry, *, force=False):
        refreshed.append((entry.slug, force))
    monkeypatch.setattr(reg, "refresh", refresh)
    caller.respond("set_alarm_config", {"password": "SECRET"})
    async with mcp(reg, caller) as client:
        for phrase in (None, "set_alarm_config tapo-h200", "play_alarm leak-sensor"):
            result = await client.call_tool("sensor_alarm_set", dict(args, confirm=phrase))
            assert result.is_error and "set_alarm_config leak-sensor" in result.content[0].text
        assert caller.sent == []
        result = await client.call_tool("sensor_alarm_set", dict(args, confirm="set_alarm_config leak-sensor"))
        caller.respond("set_alarm_config", DeviceError("failed", error_code=None))
        error = await client.call_tool("sensor_alarm_set", dict(args, confirm="set_alarm_config leak-sensor"))
    assert result.structured_content == {"device": "leak-sensor", "changed": {"volume": "mute", "duration": 20}}
    assert caller.sent[0] == ("leak-sensor", "set_alarm_config", {"volume": "mute", "duration": 20})
    assert refreshed == [("leak-sensor", True)]
    assert error.is_error and "leak-sensor" in error.content[0].text


async def test_events_read_only_sensor_and_switch_children(make_house):
    reg, caller, devices = valve_house(make_house, tier=0)
    for child in devices["hub"].children:
        child.modules[Module.TriggerLogs] = object()
    caller.respond("get_trigger_logs", {"logs": [], "start_id": 0, "sum": 0})
    assert await hub.recent_events(reg, caller, "tapo-h200") == []
    assert "radiator" not in {slug for slug, _, _ in caller.sent}
    assert {method for _, method, _ in caller.sent} == {"get_trigger_logs"}


async def test_history_tool_dispatches_by_child_kind(make_house):
    reg, caller, _ = valve_house(make_house, tier=0)
    caller.respond("get_temp_humidity_records", {"past24h_temp": [1]})
    caller.respond("get_temperature_records", {"today": [2]})
    assert (await hub.child_history(reg, caller, "temp-sensor"))["past24h_temp"] == [1]
    assert (await hub.child_history(reg, caller, "radiator"))["today"] == [2]
    with pytest.raises(RegistryError):
        await hub.child_history(reg, caller, "desk-lamp")
    assert caller.methods_sent() == ["get_temp_humidity_records", "get_temperature_records"]
