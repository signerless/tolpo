"""Fake-only plug and switch protocol tests. No discovery or device sockets."""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from kasa import DeviceType
from kasa.exceptions import DeviceError

from tolpo.areas import plugs
from tolpo.calls import Caller
from tolpo.policy import Tier
from tolpo.registry import DeviceUnavailable, Forbidden, RegistryError

from .. import fakes

pytestmark = pytest.mark.anyio


def rule_cases():
    for kind, name in plugs.RULE_NAMES.items():
        yield "plug_rules", {"kind": kind}, f"get_{name}_rules", {"start_index": 0}
        tool, args, body = {
            "countdown": ("plug_countdown_set", {"seconds": 60, "on": False},
                          {"delay": 60, "enable": True, "desired_states": {"on": False}}),
            "schedule": ("plug_schedule_set", {"at": "09:15", "on": True, "days": [1, 3]},
                         {"week_day": 10, "mode": "repeat", "s_min": 555, "s_type": "normal",
                          "enable": True, "desired_states": {"on": True}}),
            "away": ("plug_away_set", {"start": "23:00", "end": "02:00", "days": [0, 6]},
                     {"week_day": 65, "mode": "repeat", "start_min": 1380, "end_min": 120,
                      "start_time_type": "normal", "end_time_type": "normal", "start_time_offset": 0,
                      "end_time_offset": 0, "frequency": 5, "enable": True}),
        }[kind]
        yield tool, args, f"add_{name}_rule", body
        yield tool, {**args, "rule_id": "r1"}, f"edit_{name}_rule", {**body, "id": "r1"}
        yield "plug_rule_delete", {"kind": kind, "rule_id": "r1"}, f"remove_{name}_rules", {
            "remove_all": False, "rule_list": [{"id": "r1"}]}
        yield "plug_rule_enable", {"kind": kind, "rule_id": "r1", "enabled": False}, f"edit_{name}_rule", {
            "id": "r1", "enable": False, "delay": 60, "desired_states": {"on": False}}
        if kind != "countdown":
            yield "plug_rule_enable", {"kind": kind, "enabled": True}, f"set_{name}_all_enable", {"enable": True}


RULE_CASES = list(rule_cases())
# Rule envelopes follow the P115 fixtures: only schedule replies carry start_index and sum.
ONE_RULE = [{"id": "r1", "delay": 60, "enable": True, "desired_states": {"on": False}, "password": "secret"}]
RULE_RESPONSES = {
    "get_countdown_rules": {"countdown_rule_max_count": 1, "enable": False, "rule_list": ONE_RULE},
    "get_antitheft_rules": {"antitheft_rule_max_count": 1, "enable": False, "rule_list": ONE_RULE},
    "get_schedule_rules": {"schedule_rule_max_count": 32, "enable": False, "rule_list": ONE_RULE, "start_index": 0, "sum": 1},
}
# The P115 get_device_info reply with its redacted values replaced by fake ones. Private fields must never leak.
P115_DEVICE_INFO = {
    "auto_off_remain_time": 0, "auto_off_status": "off", "avatar": "plug", "charging_status": "normal",
    "default_states": {"type": "last_states"}, "device_id": "fake-device-id", "device_on": False,
    "fw_id": "fake-fw-id", "fw_ver": "1.4.8 Build 260804 Rel.045837", "has_set_location_info": True,
    "hw_id": "fake-hw-id", "hw_ver": "1.0", "ip": "192.0.2.10", "lang": "en_US", "latitude": 123456,
    "longitude": -456789, "mac": "00-00-5E-00-53-01", "model": "P115", "nickname": "UHJpdmF0ZQ==",
    "oem_id": "fake-oem-id", "on_time": 0, "overcurrent_status": "normal", "power_protection_status": "normal",
    "region": "Private/Region", "rssi": -61, "signal_level": 2, "specs": "", "ssid": "UHJpdmF0ZU5ldA==",
    "time_diff": 60, "type": "SMART.TAPOPLUG",
}
P115_PRIVATE = ("fake-", "192.0.2.10", "123456", "456789", "00-00-5E", "UHJpdmF0", "Private", "en_US")
EXTRA_CASES = [
    ("plug_settings", {"section": "default_state"}, "get_device_info", {}),
    ("plug_default_state_set", {"mode": "always_off"}, "set_device_info", {"default_states": {"type": "custom", "state": {"on": False}}}),
    ("plug_energy_usage", {}, "get_energy_usage", {}),
    ("plug_energy_history", {"series": "energy", "start_timestamp": 0, "end_timestamp": 3600, "interval": 60},
     "get_energy_data", {"start_timestamp": 0, "end_timestamp": 3600, "interval": 60}),
    ("plug_energy_history", {"series": "energy", "start_timestamp": 0, "end_timestamp": 86400, "interval": 1440, "precision": "mwh"},
     "get_energy_data_mwh", {"start_timestamp": 0, "end_timestamp": 86400, "interval": 1440}),
    ("plug_energy_history", {"series": "power", "start_timestamp": 0, "end_timestamp": 300, "interval": 5},
     "get_power_data", {"start_timestamp": 0, "end_timestamp": 300, "interval": 5}),
    ("plug_settings", {"section": "price"}, "get_electricity_price_config", {}),
    ("plug_electricity_price_set", {"constant_price": 40}, "set_electricity_price_config", {"type": "constant", "constant_price": 40}),
    ("plug_energy_clear", {"confirm": "clear_energy_data desk-lamp"}, "clear_energy_data", {}),
    ("plug_settings", {"section": "auto_off"}, "get_auto_off_config", {}),
    ("plug_auto_off_cancel", {"confirm": "cancel_auto_off desk-lamp"}, "cancel_auto_off", {}),
    ("plug_settings", {}, "get_device_info", {}),
    ("plug_settings", {"section": "charge_guard"}, "get_charging_protect", {}),
    ("plug_settings", {"section": "power_protection"}, "get_protection_power", {}),
    ("plug_settings", {"section": "max_power"}, "get_max_power", {}),
    ("plug_identity_set", {"name": "New Name", "icon": "plug"}, "set_device_info", {"nickname": "TmV3IE5hbWU=", "avatar": "plug"}),
    ("plug_location_set", {"latitude": 12.3456, "longitude": -45.6789}, "set_device_info", {"latitude": 123456, "longitude": -456789}),
    ("plug_settings", {"section": "runtime"}, "get_device_usage", {}),
    *(("switch_settings", {"section": section}, method, {}) for section, (method, _) in plugs.HARDWARE_READS.items()),
    ("switch_delay_off_set", {"enabled": True, "seconds": 60}, "set_delay_action_info", {"enabled": True, "state": {"on": False}, "time": 60}),
    ("switch_double_click_set", {"enabled": True}, "set_double_click_info", {"enable": True, "interval": 500}),
    ("switch_calibration_set", {"minimum": 10, "maximum": 100, "curve_type": 1}, "set_calibrate_brightness", {"min_threshold": 10, "max_threshold": 100, "curve_type": 1}),
    ("switch_low_power_set", {"enabled": True}, "set_device_info", {"low_power_mode": 1}),
    ("plug_settings", {"section": "led"}, "get_led_info", {}),
    ("plug_led_set", {"mode": "never"}, "set_led_info", {"led_rule": "never", "led_status": False, "night_mode": {
        "start_time": 1130, "end_time": 471, "night_mode_type": "sunrise_sunset", "sunrise_offset": 0, "sunset_offset": 0}}),
    ("plug_led_set", {"mode": "night_mode", "start": "22:00", "end": "07:30"}, "set_led_info",
     {"led_rule": "night_mode", "led_status": False, "night_mode": {
         "start_time": 1320, "end_time": 450, "night_mode_type": "time", "sunrise_offset": 0, "sunset_offset": 0}}),
]
RULE_RESPONSES.update({"get_device_info": {"default_states": {"type": "last_states"}},
                       # P115 shape. The fixture redacts the three charge values; integers stand in here.
                       "get_energy_usage": {"current_power": 0, "electricity_charge": [0, 1, 2], "local_time": "2026-10-11 22:38:00",
                                            "month_energy": 14, "month_energy_mwh": 13791, "month_runtime": 103,
                                            "today_energy": 14, "today_energy_mwh": 13791, "today_runtime": 103},
                       "get_electricity_price_config": {"type": "constant", "constant_price": 40},
                       "get_energy_data": {"data": [0, 3], "start_timestamp": 0, "end_timestamp": 3600, "interval": 60},
                       "get_power_data": {"data": [0, 3], "start_timestamp": 0, "end_timestamp": 300, "interval": 5},
                       "get_energy_data_mwh": {"data": [0, 3000], "interval": 1440}})
RULE_RESPONSES.update({
    "get_auto_off_config": {"delay_min": 120, "enable": False},
    "get_charging_protect": {"charging_enabled": True, "charging_power": 4, "charging_time": 60, "type": 0},
    "get_protection_power": {"enabled": True, "protection_power": 3000},
    "get_max_power": {"max_power": 3500},
    "get_device_usage": {"power_usage": {"past30": 14, "past30_mwh": 13791, "past7": 14, "past7_mwh": 13791, "today": 14, "today_mwh": 13791},
                         "saved_power": {"past30": 89, "past30_mwh": 89209, "past7": 89, "past7_mwh": 89209, "today": 89, "today_mwh": 89209},
                         "time_usage": {"past30": 103, "past7": 103, "today": 103}},
    "get_delay_action_info": {"enabled": True, "state": {"on": False}, "time": 60},
    "get_calibrate_brightness": {"min_threshold": 10, "max_threshold": 100, "curve_type": 0},
    "get_dimmer_custom_action_info": {"short_press": "toggle", "double_click": "gentle_on", "gentle_on": 1, "gentle_off": 1},
    "get_double_click_info": {"enable": True, "interval": 500},
    "get_sleep_mode_interval": {"interval": 10},
    "get_led_info": {"led_rule": "always", "led_status": False, "night_mode": {
        "start_time": 1130, "end_time": 471, "night_mode_type": "sunrise_sunset", "sunrise_offset": 0, "sunset_offset": 0}},
})
RULE_RESPONSES["get_device_info"]["low_power_mode"] = 0


def wall_switch():
    return fakes.FakeDevice("switch-1", "Wall Switch", "S210", DeviceType.WallSwitch)


@pytest.mark.parametrize("tool,args,method,body", RULE_CASES + EXTRA_CASES)
async def test_rule_tools_happy_path_and_refresh(make_house, mcp, tool, args, method, body):
    device = "wall-switch" if tool.startswith("switch_") else "desk-lamp"
    reg, caller, _ = make_house(tier=3, responses=RULE_RESPONSES, extra=[wall_switch()])
    reg.refresh = AsyncMock()
    async with mcp(reg, caller) as client:
        result = await client.call_tool(tool, {"device": device, **args})
    assert not result.is_error, result
    assert caller.sent[-1] == (device, method, body)
    assert "secret" not in str(result.structured_content)
    if method.startswith("get_"):
        reg.refresh.assert_not_called()
    else:
        reg.refresh.assert_awaited_once_with(reg.resolve_one(device), force=True)


@pytest.mark.parametrize("tool,args,method,body", RULE_CASES + EXTRA_CASES)
async def test_rule_tools_level_and_device_errors(make_house, mcp, tool, args, method, body):
    device = "wall-switch" if tool.startswith("switch_") else "desk-lamp"
    reg, caller, _ = make_house(tier=0, responses=RULE_RESPONSES, extra=[wall_switch()])
    if not method.startswith("get_"):
        async with mcp(reg, caller) as client:
            result = await client.call_tool(tool, {"device": device, **args})
        assert result.is_error and "level " in result.content[0].text
        assert method not in caller.methods_sent()
        assert "allow_methods" not in result.content[0].text
    reg, caller, _ = make_house(tier=3, responses=RULE_RESPONSES, extra=[wall_switch()])
    caller.respond(method, DeviceError("boom", error_code=None))
    async with mcp(reg, caller) as client:
        result = await client.call_tool(tool, {"device": device, **args})
    assert result.is_error and device in result.content[0].text and "reported an error" in result.content[0].text


@pytest.mark.parametrize("fn,args", [
    (plugs.countdown, (0, False)), (plugs.countdown, (86401, False)),
    (plugs.countdown, (True, False)), (plugs.countdown, (60, 1)),
    (plugs.schedule, ("24:00", True, [1])), (plugs.schedule, ("9:00", True, [1])),
    (plugs.schedule, ("12:60", True, [1])), (plugs.schedule, ("12:00", True, [7])),
    (plugs.schedule, ("12:00", True, [1, 1])), (plugs.schedule, ("12:00", True, [True])),
    (plugs.schedule, ("12:00", True, [])), (plugs.schedule, ("12:00", True, [], "2025-02-29")),
    (plugs.schedule, ("12:00", True, [], "20260101")),
    (plugs.schedule, ("12:00", True, [1], (date.today() + timedelta(days=7)).isoformat())),
    (plugs.schedule, ("12:00", True, [], (date.today() - timedelta(days=2)).isoformat())),
    (plugs.schedule, ("12:00", True, [], (date.today() + timedelta(days=367)).isoformat())),
    (plugs.schedule, ("12:00", True, [], "9999-12-31")), (plugs.away, ("01:00", "02:00", [], "0001-01-01")),
    (plugs.away, ("12:00", "12:00", [1])), (plugs.away, ("12:00", "invalid", [1])),
    (plugs.delete_rule, ("countdown", "x\nremove_all")), (plugs.delete_rule, ("unknown", "r1")),
    (plugs.enable_rule, ("countdown", True)), (plugs.enable_rule, ("away", 1)),
])
async def test_bad_rule_inputs_never_reach_wire(make_house, fn, args):
    reg, caller, _ = make_house(tier=4)
    with pytest.raises(ValueError):
        await fn(reg, caller, "desk-lamp", *args)
    assert caller.sent == []


async def test_a_countdown_to_switch_on_says_on_not_the_key_the_device_ignores(make_house):
    """Checked on a P115: it stores "on": false for a rule that says "device_on": true, so a timer to switch ON would
    have switched OFF."""
    reg, caller, _ = make_house(tier=2)
    await plugs.countdown(reg, caller, "desk-lamp", 3600, True)
    assert caller.sent[-1][2]["desired_states"] == {"on": True}
    await plugs.schedule(reg, caller, "desk-lamp", "08:00", True, [1])
    assert caller.sent[-1][2]["desired_states"] == {"on": True}
    assert "device_on" not in str(caller.sent)


async def test_a_listed_rule_keeps_the_action_it_will_do(make_house):
    """The real plug lists desired_states as {"on": ...}. Dropping it would hide what the rule does and would send a
    rule without its action when it is re-enabled."""
    raw = {"rule_list": [{"id": "C1", "enable": False, "delay": 3600, "remain": 3600, "desired_states": {"on": True}}],
           "countdown_rule_max_count": 1}
    reg, caller, _ = make_house(tier=2, responses={"get_countdown_rules": raw})
    out = await plugs.read_rules(reg, caller, "desk-lamp", "countdown")
    assert out["rule_list"][0]["desired_states"] == {"on": True}
    await plugs.enable_rule(reg, caller, "desk-lamp", "countdown", False, "C1")
    assert caller.sent[-1][2]["desired_states"] == {"on": True}


async def test_one_off_schedule_and_paged_enable(make_house):
    reg, caller, _ = make_house(tier=2)
    when = date.today() + timedelta(days=30)
    await plugs.schedule(reg, caller, "desk-lamp", "00:00", False, [], when.isoformat())
    assert caller.sent[-1][2] == {"week_day": 0, "mode": "once", "year": when.year, "month": when.month, "day": when.day,
                                 "s_min": 0, "s_type": "normal", "enable": True, "desired_states": {"on": False}}
    caller.respond("get_schedule_rules", lambda e, p: {"schedule_rule_max_count": 32, "enable": True, "sum": 2,
                   "start_index": p["start_index"], "rule_list": [
        {"id": "r0" if p["start_index"] == 0 else "r1", "enable": True, "s_min": 60}]})
    await plugs.enable_rule(reg, caller, "desk-lamp", "schedule", False, "r1")
    assert caller.sent[-2][2] == {"start_index": 1}
    assert caller.sent[-1][2] == {"id": "r1", "enable": False, "s_min": 60}
    with pytest.raises(RegistryError, match="not found"):
        await plugs.enable_rule(reg, caller, "desk-lamp", "schedule", False, "missing")
    # A countdown reply without sum is one page.
    caller.respond("get_countdown_rules", RULE_RESPONSES["get_countdown_rules"])
    with pytest.raises(RegistryError, match="not found"):
        await plugs.enable_rule(reg, caller, "desk-lamp", "countdown", False, "missing")
    assert caller.sent[-1] == ("desk-lamp", "get_countdown_rules", {"start_index": 0})


async def test_rule_conflict_and_refresh_failure_are_reported(make_house):
    reg, caller, _ = make_house(tier=2, responses={"add_countdown_rule": {"conflict_id": "other\nrule"}})
    with pytest.raises(RegistryError, match="desk-lamp: rule conflicts with other rule"):
        await plugs.countdown(reg, caller, "desk-lamp", 60, False)
    caller.respond("add_countdown_rule", {})
    entry = reg.resolve_one("desk-lamp")
    entry.online, entry.error = False, "TimeoutError"
    reg.refresh = AsyncMock()
    with pytest.raises(DeviceUnavailable, match="completed but refresh failed"):
        await plugs.countdown(reg, caller, "desk-lamp", 60, False)


async def test_child_socket_and_switch_targeting(make_house):
    child = fakes.plug("socket-1", "Socket One")
    strip = fakes.FakeDevice("strip-1", "Strip", "P300", DeviceType.Strip, children=[child])
    switch = fakes.FakeDevice("switch-1", "Wall Switch", "S210", DeviceType.WallSwitch)
    reg, caller, _ = make_house(tier=2, extra=[strip, switch])
    await plugs.countdown(reg, caller, "socket-one", 10, False)
    await plugs.countdown(reg, caller, "wall-switch", 10, False)
    assert [slug for slug, _, _ in caller.sent] == ["socket-one", "wall-switch"]
    for wrong in ("strip", "office-1", "hallway-cam"):
        with pytest.raises(Forbidden):
            await plugs.countdown(reg, caller, wrong, 10, False)
    assert len(caller.sent) == 2


@pytest.mark.parametrize("parent_type", [DeviceType.Strip, DeviceType.WallSwitch, DeviceType.Plug])
async def test_parent_with_children_is_never_a_target(make_house, parent_type):
    gangs = [fakes.FakeDevice("gang-1", "Gang One", "S220", DeviceType.WallSwitch),
             fakes.FakeDevice("gang-2", "Gang Two", "S220", DeviceType.WallSwitch)]
    parent = fakes.FakeDevice("parent-1", "Parent", "S220", parent_type, children=gangs)
    reg, caller, _ = make_house(tier=3, extra=[parent])
    for fn, args in ((plugs.countdown, (10, True)), (plugs.delete_rule, ("schedule", "r1")),
                     (plugs.enable_rule, ("schedule", False)), (plugs.set_default_state, ("always_on",)),
                     (plugs.write, ("clear_energy_data",)), (plugs.hardware, ("state",))):
        with pytest.raises(Forbidden):
            await fn(reg, caller, "parent", *args)
    assert caller.sent == []
    await plugs.countdown(reg, caller, "gang-two", 10, False)
    assert [slug for slug, _, _ in caller.sent] == ["gang-two"]


async def test_rule_output_hides_unknown_nested_values_and_flattens_text(make_house):
    raw = {"rule_list": [{"id": "r1\n\t", "enable": False, "remain": 0, "nickname": "secret",
                     "desired_states": {"on": False, "ssid": "secret", "token": "secret"}}],
           "countdown_rule_max_count": 0, "password": "secret", "mac": "secret", "conflict_id": "", "empty": None}
    reg, caller, _ = make_house(tier=0, responses={"get_countdown_rules": raw})
    assert await plugs.read_rules(reg, caller, "desk-lamp", "countdown") == {
        "device": "desk-lamp", "kind": "countdown", "rule_list": [{"id": "r1", "enable": False,
        "remain": 0, "desired_states": {"on": False}}], "countdown_rule_max_count": 0}


async def test_default_formats_and_safety_refusals(make_house):
    reg, caller, _ = make_house(tier=2, responses=RULE_RESPONSES)
    for mode in ("last_states", "always_on", "always_off"):
        out = await plugs.set_default_state(reg, caller, "desk-lamp", mode)
        assert out["default"] == mode
    for mode in ("toggle_states", "bad"):
        with pytest.raises(ValueError):
            await plugs.set_default_state(reg, caller, "desk-lamp", mode)
    caller.respond("get_device_info", {"default_states": {"type": "smart_status", "re_power_type_capability": ["toggle_states", "always_off"]}})
    await plugs.set_default_state(reg, caller, "desk-lamp", "toggle_states")
    assert caller.sent[-1][2] == {"default_states": {"type": "smart_status", "re_power_type": "toggle_states"}}
    with pytest.raises(ValueError, match="not supported"):
        await plugs.set_default_state(reg, caller, "desk-lamp", "always_on")
    caller.respond("get_device_info", {})
    with pytest.raises(RegistryError, match="did not report"):
        await plugs.set_default_state(reg, caller, "desk-lamp", "last_states")


async def test_default_and_energy_outputs_are_compact_and_allowlisted(make_house):
    raw = {"default_states": {"type": "custom", "state": {"on": False, "password": "secret"}},
           "ssid": "secret", "mac": "secret", "homekit_setup_code": "secret"}
    reg, caller, _ = make_house(tier=0, responses={"get_device_info": raw})
    assert await plugs.default_state(reg, caller, "desk-lamp") == {"device": "desk-lamp", "default_states": {"type": "custom", "state": {"on": False}}}
    raw = {"today_energy": 0, "local_time": "2026\n 10\t11", "electricity_charge": [0, 40, 50],
           "saved_electricity_charge": [0, 1, 2], "past7d_mwh": [[0, 1], [2]], "today_charge_energy": 3,
           "password": "secret", "unknown": "secret", "past1y_mwh": [], "month_energy": None}
    caller.respond("get_energy_usage", raw)
    out = await plugs.read(reg, caller, "desk-lamp", "get_energy_usage", plugs.ENERGY_FIELDS)
    assert out == {"device": "desk-lamp", "today_energy": 0, "local_time": "2026 10 11",
                   "electricity_charge": [0, 40, 50], "saved_electricity_charge": [0, 1, 2],
                   "past7d_mwh": [[0, 1], [2]], "today_charge_energy": 3}


@pytest.mark.parametrize("args", [
    ("energy", -1, 3600, 60), ("energy", 0, 0, 60), ("energy", 3600, 0, 60),
    ("energy", 0, 3600, 5), ("power", 0, 3600, 1440), ("energy", True, 3600, 60),
    ("energy", 0, 513 * 3600, 60), ("power", 0, 300, 5, "mwh"), ("bad", 0, 3600, 60),
])
async def test_invalid_history_never_reaches_wire(make_house, args):
    reg, caller, _ = make_house()
    with pytest.raises(ValueError):
        await plugs.energy_history(reg, caller, "desk-lamp", *args)
    assert not caller.sent


@pytest.mark.parametrize("price", [-1, True, 1.5, 2147483648])
async def test_invalid_price_never_reaches_wire(make_house, price):
    reg, caller, _ = make_house(tier=2)
    with pytest.raises(ValueError):
        await plugs.set_price(reg, caller, "desk-lamp", price)
    assert not caller.sent


async def test_energy_clear_needs_exact_phrase_even_above_level(make_house, mcp):
    reg, caller, _ = make_house(tier=4)
    async with mcp(reg, caller) as client:
        for confirm in (None, "clear_energy_data other-device", "set_device_info desk-lamp"):
            result = await client.call_tool("plug_energy_clear", {"device": "desk-lamp", "confirm": confirm})
            assert result.is_error and "confirm='clear_energy_data desk-lamp'" in result.content[0].text
    assert not caller.sent
    reg, caller, _ = make_house(tier=2)
    with pytest.raises(Forbidden):
        await plugs.write(reg, caller, "desk-lamp", "clear_energy_data", confirm="clear_energy_data desk-lamp")
    assert not caller.sent


async def test_history_oversized_or_invalid_response_is_reported(make_house):
    reg, caller, _ = make_house(responses={"get_power_data": {"data": [0] * 514}})
    with pytest.raises(DeviceUnavailable, match="too many samples"):
        await plugs.energy_history(reg, caller, "desk-lamp", "power", 0, 300, 5)
    caller.respond("get_power_data", "bad")
    with pytest.raises(DeviceUnavailable, match="invalid response"):
        await plugs.energy_history(reg, caller, "desk-lamp", "power", 0, 300, 5)
    caller.respond("get_power_data", {"data": 5})
    with pytest.raises(DeviceUnavailable, match="invalid sample list"):
        await plugs.energy_history(reg, caller, "desk-lamp", "power", 0, 300, 5)


@pytest.mark.parametrize("fn,args", [
    (plugs.identity, ()), (plugs.identity, ("bad\nname",)), (plugs.identity, ("x" * 65,)),
    (plugs.identity, (" ok ",)), (plugs.identity, (None, "bad/icon")),
    (plugs.location, (91, 0)), (plugs.location, (0, -181)),
    (plugs.location, (float("nan"), 0)), (plugs.location, (0, float("inf"))),
    (plugs.location, (True, 0)),
    (plugs.delay_off, (True, 0)), (plugs.delay_off, (False, 86401)), (plugs.delay_off, (1, 30)),
    (plugs.double_click, (True, 99)), (plugs.double_click, (True, 2001)), (plugs.double_click, (1, 500)),
    (plugs.calibration, (0, 100)), (plugs.calibration, (100, 1)), (plugs.calibration, (1, 101)),
    (plugs.calibration, (1, 100, 256)), (plugs.calibration, (True, 100)),
    (plugs.low_power, (1,)),
    (plugs.led_set, ("bad",)), (plugs.led_set, ("never", "10:00", "11:00")),
    (plugs.led_set, ("night_mode",)), (plugs.led_set, ("night_mode", "10:00", "10:00")),
    (plugs.hardware, ("bad",)), (plugs.settings, ("bad",)),
])
async def test_bad_setting_inputs_never_reach_wire(make_house, fn, args):
    reg, caller, _ = make_house(tier=4, extra=[wall_switch()])
    with pytest.raises(ValueError):
        await fn(reg, caller, "wall-switch", *args)
    assert not caller.sent


@pytest.mark.parametrize("tool,args", [
    ("plug_countdown_set", {"seconds": True, "on": False}),
    ("plug_countdown_set", {"seconds": "60", "on": False}),
    ("plug_countdown_set", {"seconds": 60, "on": 1}),
    ("plug_schedule_set", {"at": "12:00", "on": False, "days": [True]}),
    ("plug_schedule_set", {"at": "12:00", "on": False, "days": ["1"]}),
    ("plug_location_set", {"latitude": True, "longitude": 0}),
])
async def test_mcp_does_not_coerce_safety_inputs(make_house, mcp, tool, args):
    reg, caller, _ = make_house(tier=4)
    async with mcp(reg, caller) as client:
        out = await client.call_tool(tool, {"device": "desk-lamp", **args})
    assert out.is_error
    assert not caller.sent


async def test_auto_off_cancel_confirmation_and_tool_hints(make_house, mcp):
    reg, caller, _ = make_house(tier=4)
    async with mcp(reg, caller) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for tool, _, method, _ in RULE_CASES + EXTRA_CASES:
            hints = tools[tool].annotations
            assert hints.read_only_hint is method.startswith("get_")
            assert hints.destructive_hint is (None if method.startswith("get_") else method in ("clear_energy_data", "cancel_auto_off"))
        for confirm in (None, "cancel_auto_off other-device", "clear_energy_data desk-lamp"):
            result = await client.call_tool("plug_auto_off_cancel", {"device": "desk-lamp", "confirm": confirm})
            assert result.is_error and "confirm='cancel_auto_off desk-lamp'" in result.content[0].text
    assert not caller.sent


async def test_hardware_scope_and_low_power_support(make_house):
    dimmer = fakes.FakeDevice("dimmer-1", "Dimmer Plug", "D100", DeviceType.Dimmer)
    reg, caller, _ = make_house(tier=2, extra=[dimmer, wall_switch()])
    await plugs.calibration(reg, caller, "dimmer-plug", 10, 90)
    assert caller.sent[-1] == ("dimmer-plug", "set_calibrate_brightness", {"min_threshold": 10, "max_threshold": 90})
    await plugs.led_set(reg, caller, "dimmer-plug", "always")
    assert caller.sent[-2:] == [("dimmer-plug", "get_led_info", {}), ("dimmer-plug", "set_led_info", {"led_rule": "always"})]
    with pytest.raises(Forbidden):
        await plugs.led_set(reg, caller, "office-1", "never")
    assert caller.sent[-1][2] == {"led_rule": "always"}
    for device in ("desk-lamp", "office-1", "hallway-cam"):
        with pytest.raises(Forbidden):
            await plugs.hardware(reg, caller, device, "state")
    with pytest.raises(RegistryError, match="did not report low_power_mode"):
        await plugs.low_power(reg, caller, "wall-switch", True)


async def test_runtime_protection_hardware_and_price_allowlists(make_house):
    secret = {"ssid": "secret", "password": "secret", "token": "secret", "mac": "secret",
              "homekit_setup_code": "secret", "matter_setup_code": "secret"}
    reg, caller, _ = make_house(tier=0, extra=[wall_switch()])
    for method, fields in [*plugs.HARDWARE_READS.values(), ("get_device_usage", plugs.RUNTIME_FIELDS),
                           ("get_device_info", plugs.STATUS_FIELDS), ("get_electricity_price_config", plugs.PRICE_FIELDS),
                           ("get_charging_protect", plugs.CHARGE_FIELDS), ("get_protection_power", plugs.POWER_FIELDS),
                           ("get_max_power", plugs.POWER_FIELDS), ("get_auto_off_config", plugs.AUTO_FIELDS),
                           ("get_energy_data", plugs.HISTORY_FIELDS), ("get_energy_data_mwh", plugs.HISTORY_FIELDS),
                           ("get_power_data", plugs.HISTORY_FIELDS)]:
        raw = {**RULE_RESPONSES.get(method, {}), **secret}
        raw["state"] = {"on": False, **secret}
        caller.respond(method, raw)
        out = await plugs.read(reg, caller, "wall-switch", method, fields)
        assert "secret" not in str(out)
        assert all(v is not None and v != "" and v != [] and v != {} for v in out.values())
    caller.respond("get_device_info", {"overcurrent_status": "overcurrent\n", "charging_status": "normal", **secret})
    assert await plugs.settings(reg, caller, "desk-lamp") == {"device": "desk-lamp", "overcurrent_status": "overcurrent", "charging_status": "normal"}


async def test_p115_read_fakes_match_captured_envelopes(make_house):
    # Sanitized P115 fixtures define these shapes. Values here belong to a fake house.
    responses = {
        "get_countdown_rules": {"countdown_rule_max_count": 1, "enable": False, "rule_list": []},
        "get_antitheft_rules": {"antitheft_rule_max_count": 1, "enable": False, "rule_list": []},
        "get_schedule_rules": {"schedule_rule_max_count": 32, "enable": False, "rule_list": [], "start_index": 0, "sum": 0},
    }
    reg, caller, _ = make_house(tier=0, responses=responses)
    for kind, name in plugs.RULE_NAMES.items():
        out = await plugs.read_rules(reg, caller, "desk-lamp", kind)
        assert out == {"device": "desk-lamp", "kind": kind, **{k: v for k, v in responses[f"get_{name}_rules"].items() if k != "rule_list"}}
    season = {"midpeak": 0, "offpeak": 0, "onpeak": 0, "period": [0] * 4,
              "weekday_config": [1] * 24, "weekend_config": [1] * 24}
    price = {"type": "constant", "constant_price": 0, "time_of_use_config": {"summer": season, "winter": season}}
    caller.respond("get_electricity_price_config", price)
    assert await plugs.read(reg, caller, "desk-lamp", "get_electricity_price_config", plugs.PRICE_FIELDS) == {"device": "desk-lamp", **price}
    caller.respond("get_led_info", RULE_RESPONSES["get_led_info"])
    assert await plugs.read(reg, caller, "desk-lamp", "get_led_info", plugs.LED_FIELDS) == {"device": "desk-lamp", **RULE_RESPONSES["get_led_info"]}
    usage = {"power_usage": {"today": 1, "past7": 2, "past30": 3, "today_mwh": 1000, "past7_mwh": 2000, "past30_mwh": 3000},
             "saved_power": {"today": 2, "past7": 3, "past30": 4, "today_mwh": 2000, "past7_mwh": 3000, "past30_mwh": 4000},
             "time_usage": {"today": 3, "past7": 4, "past30": 5}}
    caller.respond("get_device_usage", usage)
    assert await plugs.read(reg, caller, "desk-lamp", "get_device_usage", plugs.RUNTIME_FIELDS) == {"device": "desk-lamp", "time_usage": usage["time_usage"]}


async def test_real_child_wrapper_uses_only_selected_socket(make_house):
    from kasa.protocols.smartprotocol import _ChildProtocolWrapper

    children = [fakes.plug("socket-1", "Socket One"), fakes.plug("socket-2", "Socket Two")]
    strip = fakes.FakeDevice("strip-1", "Strip", "P300", DeviceType.Strip, children=children)
    # Only the parent wire is fake. The caller and python-kasa child envelope run for real.
    parent = SimpleNamespace(_transport=object(), query=AsyncMock(return_value={
        "control_child": {"responseData": {"error_code": 0, "result": {"id": "r1"}}}}))
    for child in children:
        child.protocol = _ChildProtocolWrapper(child.device_id, parent)
    reg, _, _ = make_house(tier=2, extra=[strip])
    await plugs.countdown(reg, Caller(reg), "socket-two", 60, False)
    parent.query.assert_awaited_once_with({"control_child": {"device_id": "socket-2", "requestData": {
        "method": "add_countdown_rule", "params": {"delay": 60, "enable": True, "desired_states": {"on": False}}}}}, 3)


async def test_catalog_covers_all_sent_methods_at_exact_levels(make_house):
    _, _, _ = make_house()
    expected = {method for _, _, method, _ in RULE_CASES + EXTRA_CASES}
    declared = {rule.method for rule in plugs.METHODS}
    shared = {"get_device_info", "set_device_info", "get_device_usage"}
    assert declared == expected - shared
    assert declared.isdisjoint(shared)
    for rule in plugs.METHODS:
        expected_tier = Tier.READ if rule.method.startswith("get_") else Tier.SECURITY if rule.method in ("clear_energy_data", "cancel_auto_off") else Tier.SETTING
        assert rule.tier == expected_tier


@pytest.mark.parametrize("raw", [None, "bad", [], {"rule_list": "bad"}, {"rule_list": [1]}])
async def test_invalid_rule_responses_name_device_and_method(make_house, raw):
    reg, caller, _ = make_house(tier=2, responses={"get_countdown_rules": raw})
    for fn, args in ((plugs.read_rules, ("countdown",)), (plugs.enable_rule, ("countdown", True, "r1"))):
        with pytest.raises(DeviceUnavailable, match="desk-lamp: get_countdown_rules returned an invalid"):
            await fn(reg, caller, "desk-lamp", *args)
    assert "edit_countdown_rule" not in caller.methods_sent()


async def test_identity_and_location_writes_do_not_echo_private_fields(make_house):
    response = {"nickname": "private-name", "latitude": 123456, "longitude": -456789,
                "ssid": "private-network", "mac": "private-address", "token": "private-token",
                "password": "private-password", "matter_setup_code": "private-code"}
    reg, caller, _ = make_house(tier=2, responses={"set_device_info": response})
    assert await plugs.identity(reg, caller, "desk-lamp", "Private Name", "plug") == {
        "device": "desk-lamp", "changed": "set_device_info", "fields": ["name", "icon"]}
    assert await plugs.location(reg, caller, "desk-lamp", 12.3456, -45.6789) == {
        "device": "desk-lamp", "changed": "set_device_info", "fields": ["location"]}


async def test_paging_stops_on_a_malformed_sum(make_house):
    reg, caller, _ = make_house(tier=2, responses={"get_schedule_rules": {"sum": "many", "rule_list": [{"id": "r0"}]}})
    with pytest.raises(RegistryError, match="not found"):
        await plugs.enable_rule(reg, caller, "desk-lamp", "schedule", True, "r9")
    assert caller.methods_sent() == ["get_schedule_rules"]


async def test_p115_device_info_reads_leak_nothing_private(make_house):
    reg, caller, _ = make_house(tier=0, responses={"get_device_info": P115_DEVICE_INFO})
    assert await plugs.settings(reg, caller, "desk-lamp") == {
        "device": "desk-lamp", "auto_off_remain_time": 0, "auto_off_status": "off", "charging_status": "normal",
        "overcurrent_status": "normal", "power_protection_status": "normal"}
    assert await plugs.settings(reg, caller, "desk-lamp", "default_state") == {"device": "desk-lamp", "default_states": {"type": "last_states"}}
    for out in (await plugs.settings(reg, caller, "desk-lamp"), await plugs.settings(reg, caller, "desk-lamp", "default_state")):
        assert not any(private in str(out) for private in P115_PRIVATE)


async def test_plug_settings_sections_cover_every_read(make_house, mcp):
    reg, caller, _ = make_house(tier=0, responses=RULE_RESPONSES)
    async with mcp(reg, caller) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"plug_default_state", "plug_auto_off", "plug_protection", "plug_runtime", "plug_led",
                "plug_electricity_price"}.isdisjoint(names)
        assert len([n for n in names if n.startswith(("plug_", "switch_"))]) == 21
        bad = await client.call_tool("plug_settings", {"device": "desk-lamp", "section": "state"})
    assert bad.is_error and caller.sent == []
    assert set(plugs.PLUG_READS) | {"default_state"} == set(plugs.PlugSection.__args__)
