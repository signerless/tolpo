"""Lighting area tests. All devices and wire responses are simulated."""

from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from kasa import Feature, Module
from kasa.exceptions import DeviceError, SmartErrorCode

from tolpo.areas import lights
from tolpo.registry import BadRequest, DeviceUnavailable, Forbidden, NotFound, Unsupported
from .. import fakes

pytestmark = pytest.mark.anyio

DEFAULT = {"type": "custom", "state": {"brightness": 50, "color_temp": 3000},
           "re_power_type": "last_states", "re_power_type_capability": ["always_on", "always_off", "last_states"]}


async def test_defaults_read_compact_and_clean(make_house):
    reg, caller, _ = make_house(tier=0, responses={"get_device_info": {
        "ssid": "PRIVATE", "default_states": {**DEFAULT, "password": "PRIVATE", "empty": "",
        "state": {"brightness": 50, "token": "PRIVATE"}}}})
    result = await lights.get_defaults(reg, caller, "office-1")
    assert result == {"device": "office-1", "default_states": {**DEFAULT, "state": {"brightness": 50}}}
    assert "PRIVATE" not in str(result)


async def test_defaults_patch_preserves_other_behavior_and_refreshes(make_house, monkeypatch):
    reg, caller, _ = make_house(tier=2, responses={"get_device_info": {"default_states": DEFAULT}})
    refresh = AsyncMock()
    monkeypatch.setattr(reg, "refresh", refresh)
    result = await lights.set_defaults(reg, caller, "office-1", {"re_power_type": "always_off"})
    expected = {k: v for k, v in DEFAULT.items() if k != "re_power_type_capability"}
    expected["re_power_type"] = "always_off"
    assert caller.sent[-1] == ("office-1", "set_device_info", {"default_states": expected})
    assert result["changed"] == {"default_states": expected}
    refresh.assert_awaited_once_with(reg.resolve_one("office-1"), force=True)
    await lights.set_defaults(reg, caller, "office-1", {"type": "last_states"})
    assert "state" not in caller.sent[-1][2]["default_states"]


async def test_defaults_per_child(make_house):
    parent = fakes.bulb("ceiling", "Ceiling")
    parent.children = [fakes.bulb("down", "Downlight"), fakes.bulb("ring", "Ring")]
    reg, caller, _ = make_house(tier=2, extra=[parent], responses={"get_device_info": {"default_states": DEFAULT}})
    await lights.set_defaults(reg, caller, "ring", {"type": "last_states"})
    assert caller.sent[-1][0] == "ring"
    assert reg.resolve_one("ring").root.slug == "ceiling"


async def test_defaults_policy_kind_and_errors(make_house):
    reg, caller, _ = make_house(responses={"get_device_info": {"default_states": DEFAULT}})
    with pytest.raises(Forbidden, match="level 2"):
        await lights.set_defaults(reg, caller, "office-1", {"type": "last_states"})
    assert "set_device_info" not in caller.methods_sent()
    with pytest.raises(BadRequest, match="not a light"):
        await lights.get_defaults(reg, caller, "desk-lamp")
    caller.respond("get_device_info", TimeoutError())
    with pytest.raises(DeviceUnavailable, match="office-1.*did not answer"):
        await lights.get_defaults(reg, caller, "office-1")
    caller.respond("get_device_info", {})
    with pytest.raises(Unsupported):
        await lights.get_defaults(reg, caller, "office-1")


@pytest.mark.parametrize("patch", [
    {"type": "bad"}, {"password": "x"}, {"state": {"brightness": True}}, {"on_off": 1},
    {"state": {"hue": 20}}, {"state": {"hue": 20, "saturation": 30, "color_temp": 1000}},
    {"re_power_type": "toggle_states"}, {"re_power_type_capability": []},
    {"brightness": {"type": "custom", "value": 101}}, {"type": "last_states", "state": {"brightness": 50}},
    {"brightness": {"type": "custom", "value": 20}, "state": {"brightness": 50}},
])
async def test_defaults_invalid_never_writes(make_house, patch):
    reg, caller, _ = make_house(tier=2, responses={"get_device_info": {"default_states": DEFAULT}})
    with pytest.raises(BadRequest):
        await lights.set_defaults(reg, caller, "office-1", patch)
    assert "set_device_info" not in caller.methods_sent()


async def test_defaults_white_brightness_shape(make_house):
    reg, caller, _ = make_house(tier=2, responses={"get_device_info": {
        "default_states": {"brightness": {"type": "last_states"}, "re_power_type": "always_on"}}})
    await lights.set_defaults(reg, caller, "office-1", {"brightness": {"type": "custom", "value": 30}})
    assert caller.sent[-1][2] == {"default_states": {
        "brightness": {"type": "custom", "value": 30}, "re_power_type": "always_on"}}


SCHEDULE = {"enable": True, "mode": "REPEAT", "week_day": 62, "s_type": "NORMAL",
            "s_min": 480, "e_type": "NONE", "e_min": 0, "desired_states": {"on": True, "brightness": 40}}
COUNTDOWN = {"enable": True, "delay": 300, "desired_states": {"on": False}}
AWAY = {"enable": True, "mode": "REPEAT", "week_day": 127, "start_time_type": "NORMAL",
        "end_time_type": "NORMAL", "start_min": 1200, "end_min": 60, "frequency": 10}
RULES = {"schedule": SCHEDULE, "countdown": COUNTDOWN, "antitheft": AWAY}


@pytest.mark.parametrize("kind", RULES)
async def test_rules_crud_enable_and_read(make_house, monkeypatch, kind):
    row = {**RULES[kind], "id": "r1"}
    reg, caller, _ = make_house(tier=2, responses={
        f"get_{kind}_rules": {"rule_list": [row], "sum": 1, "token": "PRIVATE"},
        f"add_{kind}_rule": {"id": "r2", "password": "PRIVATE"}})
    refresh = AsyncMock()
    monkeypatch.setattr(reg, "refresh", refresh)
    listed = await lights.list_rules(reg, caller, "office-1", kind)
    assert listed["rules"] == {"rule_list": [row], "sum": 1}
    added = await lights.change_rule(reg, caller, "office-1", kind, RULES[kind])
    assert added == {"device": "office-1", "kind": kind, "changed": RULES[kind], "id": "r2"}
    await lights.change_rule(reg, caller, "office-1", kind, row, editing=True)
    await lights.enable_rule(reg, caller, "office-1", kind, "r1", False)
    assert caller.sent[-1][2] == {**row, "enable": False}
    assert await lights.delete_rule(reg, caller, "office-1", kind, "r1") == {
        "device": "office-1", "kind": kind, "deleted": "r1"}
    assert caller.sent[-1][2] == {"remove_all": False, "rule_list": [{"id": "r1"}]}
    assert refresh.await_count == 4
    assert "PRIVATE" not in str((listed, added))


@pytest.mark.parametrize("kind", RULES)
@pytest.mark.parametrize("operation", ["add", "edit", "delete", "enable"])
async def test_every_rule_write_is_level_two(make_house, kind, operation):
    row = {**RULES[kind], "id": "r1"}
    reg, caller, _ = make_house(tier=1, responses={f"get_{kind}_rules": {"rule_list": [row]}})
    with pytest.raises(Forbidden, match="level 2") as failure:
        if operation == "add":
            await lights.change_rule(reg, caller, "office-1", kind, RULES[kind])
        elif operation == "edit":
            await lights.change_rule(reg, caller, "office-1", kind, row, editing=True)
        elif operation == "delete":
            await lights.delete_rule(reg, caller, "office-1", kind, "r1")
        else:
            await lights.enable_rule(reg, caller, "office-1", kind, "r1", True)
    assert "allow_methods" not in str(failure.value)
    assert all(method.startswith("get_") for method in caller.methods_sent())


@pytest.mark.parametrize("kind", RULES)
@pytest.mark.parametrize("operation", ["get", "add", "edit", "remove"])
async def test_every_rule_method_reports_device_errors(make_house, kind, operation):
    method = f"{operation}_{kind}_{'rules' if operation in ('get', 'remove') else 'rule'}"
    reg, caller, _ = make_house(tier=2, responses={method: TimeoutError()})
    with pytest.raises(DeviceUnavailable, match="office-1.*did not answer"):
        if operation == "get":
            await lights.list_rules(reg, caller, "office-1", kind)
        elif operation == "remove":
            await lights.delete_rule(reg, caller, "office-1", kind, "r1")
        else:
            rule = {**RULES[kind], **({"id": "r1"} if operation == "edit" else {})}
            await lights.change_rule(reg, caller, "office-1", kind, rule, editing=operation == "edit")


@pytest.mark.parametrize("kind,patch", [
    ("schedule", {"s_min": -1}), ("schedule", {"s_min": 1440}), ("schedule", {"s_min": 4.5}),
    ("schedule", {"week_day": True}), ("schedule", {"week_day": 128}), ("schedule", {"week_day": 0}),
    ("schedule", {"mode": "ONCE", "week_day": 0, "year": 2025, "month": 2, "day": 29}),
    ("schedule", {"mode": "ONCE", "week_day": 0, "year": 2024, "month": 4, "day": 31}),
    ("schedule", {"mode": "ONCE", "week_day": 0}), ("schedule", {"month": 1}),
    ("schedule", {"s_type": "SUNRISE", "time_offset": 721}),
    ("schedule", {"time_offset": -1}), ("schedule", {"s_type": "NONE"}),
    ("schedule", {"e_type": "NONE", "e_min": 20}), ("schedule", {"id": "unwanted"}),
    ("countdown", {"delay": 0}), ("countdown", {"delay": 86401}), ("countdown", {"delay": "30"}),
    ("countdown", {"enable": 1}), ("countdown", {"desired_states": {"brightness": 30}}),
    ("antitheft", {"frequency": 0}), ("antitheft", {"end_min": 1200}),
    ("antitheft", {"end_min": 1440}), ("antitheft", {"start_time_offset": True}),
    ("antitheft", {"end_time_type": "UNKNOWN"}), ("antitheft", {"password": "x"}),
])
async def test_rule_validation_prevents_any_wire_call(make_house, kind, patch):
    reg, caller, _ = make_house(tier=2)
    with pytest.raises(BadRequest):
        await lights.change_rule(reg, caller, "office-1", kind, {**RULES[kind], **patch})
    assert caller.sent == []


async def test_rule_dates_solar_offsets_and_enable_pagination(make_house):
    reg, caller, _ = make_house(tier=2)
    once = {**SCHEDULE, "mode": "ONCE", "week_day": 0, "year": 2024, "month": 2, "day": 29,
            "s_type": "SUNSET", "time_offset": -30}
    await lights.change_rule(reg, caller, "office-1", "schedule", once)
    assert caller.sent[-1][2] == once
    caller.respond("get_schedule_rules", lambda e, p: {"sum": 2, "rule_list": [
        {**SCHEDULE, "id": "first" if p["start_index"] == 0 else "r1"}]})
    await lights.enable_rule(reg, caller, "office-1", "schedule", "r1", False)
    assert [p for _, m, p in caller.sent if m == "get_schedule_rules"] == [{"start_index": 0}, {"start_index": 1}]
    assert caller.sent[-1][2] == {**SCHEDULE, "id": "r1", "enable": False}


async def test_countdown_enable_omits_read_only_remaining(make_house):
    reg, caller, _ = make_house(tier=2, responses={"get_countdown_rules": {
        "rule_list": [{**COUNTDOWN, "id": "r1", "remain": 120}]}})
    await lights.enable_rule(reg, caller, "office-1", "countdown", "r1", False)
    assert caller.sent[-1][2] == {**COUNTDOWN, "id": "r1", "enable": False}


async def test_next_event_safe_read_and_failure(make_house):
    reg, caller, _ = make_house(tier=0, responses={"get_next_event": {
        "next_event": {"id": "line\nbreak", "type": 1, "s_time": 100, "desired_states": {
        "on": True, "token": "PRIVATE"}}, "mac": "PRIVATE", "empty": {}}})
    assert await lights.next_event(reg, caller, "office-1") == {"device": "office-1", "next_event": {
        "next_event": {"id": "line break", "type": 1, "s_time": 100, "desired_states": {"on": True}}}}
    caller.respond("get_next_event", TimeoutError())
    with pytest.raises(DeviceUnavailable, match="office-1"):
        await lights.next_event(reg, caller, "office-1")


async def test_mcp_lighting_tools(make_house, mcp):
    reg, caller, _ = make_house(tier=2, responses={"get_device_info": {"default_states": DEFAULT},
        "get_schedule_rules": {"rule_list": [{**SCHEDULE, "id": "r1"}]}})
    cases = [("light_get_defaults", {}), ("light_set_defaults", {"settings": {"type": "last_states"}}),
        ("light_list_rules", {"kind": "schedule"}), ("light_add_rule", {"kind": "schedule", "rule": SCHEDULE}),
        ("light_edit_rule", {"kind": "schedule", "rule": {**SCHEDULE, "id": "r1"}}),
        ("light_delete_rule", {"kind": "schedule", "rule_id": "r1"}),
        ("light_enable_rule", {"kind": "schedule", "rule_id": "r1", "enable": False}), ("light_next_event", {})]
    async with mcp(reg, caller) as client:
        catalog = {t.name: t for t in (await client.list_tools()).tools}
        for name, arguments in cases:
            result = await client.call_tool(name, {"device": "office-1", **arguments})
            assert not result.is_error, result
            assert catalog[name].annotations.read_only_hint == (name in {
                "light_get_defaults", "light_list_rules", "light_next_event"})


OPTIONS = {"smooth_transitions": (Feature.Type.Switch, False),
           "smooth_transition_on": (Feature.Type.Number, 10), "smooth_transition_off": (Feature.Type.Number, 10),
           "smooth_transition_change": (Feature.Type.Switch, False), "strip_segments": (Feature.Type.Number, 25),
           "led": (Feature.Type.Switch, False)}


def add_lighting_features(device, *, custom=True):
    for name, (kind, value) in OPTIONS.items():
        device.features[name] = fakes.FakeFeature(name, kind, value=value, hi=50)
    device.features["light_effect"] = fakes.FakeFeature("light_effect", Feature.Type.Choice,
        value="Off", choices=["Off", "Party", "Relax", "Aurora"])
    module = SimpleNamespace(effect_list=["Off", "Party", "Relax", "Aurora"], LIGHT_EFFECTS_OFF="Off",
        has_custom_effects=custom, set_effect=AsyncMock(return_value={}), set_custom_effect=AsyncMock(return_value={}))
    device.modules[Module.LightEffect] = module
    return module


@pytest.mark.parametrize("name", OPTIONS)
async def test_feature_options_keep_existing_allow_write_and_bounds(make_house, name):
    reg, caller, devices = make_house()
    add_lighting_features(devices["office1"])
    value = OPTIONS[name][1]
    with pytest.raises(Forbidden, match="policy blocks"):
        await lights.set_option(reg, "office-1", name, value)
    assert devices["office1"].features[name].calls == []
    reg, caller, devices = make_house(allow_write=frozenset({f"light.{name}"}))
    add_lighting_features(devices["office1"])
    assert await lights.set_option(reg, "office-1", name, value) == {
        "device": "office-1", "changed": {name: value}}
    assert devices["office1"].features[name].calls == [value]
    invalid = 51 if type(value) is int else 1
    with pytest.raises(BadRequest):
        await lights.set_option(reg, "office-1", name, invalid)
    assert devices["office1"].features[name].calls == [value]


@pytest.mark.parametrize("name", OPTIONS)
async def test_feature_errors_name_the_device(make_house, name):
    reg, caller, devices = make_house(allow_write=frozenset({name}))
    add_lighting_features(devices["office1"])
    devices["office1"].features[name].set_value = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(DeviceUnavailable, match="office-1.*did not answer"):
        await lights.set_option(reg, "office-1", name, OPTIONS[name][1])


async def test_feature_reads_only_known_light_settings(make_house):
    reg, caller, devices = make_house(tier=0)
    add_lighting_features(devices["office1"])
    result = await lights.get_features(reg, "office-1")
    assert set(result["features"]) == set(OPTIONS) | {"light_effect"}
    assert result["features"]["light_effect"]["choices"] == ["Off", "Party", "Relax", "Aurora"]
    assert result["features"]["led"] == {"value": False, "writable": False}
    assert "MyHomeWifi" not in str(result)
    assert caller.sent == []


@pytest.mark.parametrize("name", ["Party", "Relax", "Aurora", "Off"])
async def test_builtin_effects_use_kasa_module_and_opt_in(make_house, name):
    reg, caller, devices = make_house()
    module = add_lighting_features(devices["office1"])
    with pytest.raises(Forbidden):
        await lights.set_effect(reg, "office-1", name)
    module.set_effect.assert_not_awaited()
    reg, caller, devices = make_house(allow_write=frozenset({"light_effect"}))
    module = add_lighting_features(devices["office1"])
    result = await lights.set_effect(reg, "office-1", name)
    module.set_effect.assert_awaited_once_with(name, brightness=None, transition=None)
    assert result["changed"] == {"light_effect": name}


async def test_builtin_effect_keeps_brightness_unless_asked(make_house):
    reg, caller, devices = make_house(allow_write=frozenset({"light_effect"}))
    module = add_lighting_features(devices["office1"])
    result = await lights.set_effect(reg, "office-1", "Aurora", 30, 500)
    module.set_effect.assert_awaited_once_with("Aurora", brightness=30, transition=500)
    assert result["changed"] == {"light_effect": "Aurora", "brightness": 30, "transition": 500}
    for brightness, transition in ((0, None), (101, None), (True, None), (None, -1), (None, 60001), (None, 1.5)):
        with pytest.raises(BadRequest):
            await lights.set_effect(reg, "office-1", "Aurora", brightness, transition)
    with pytest.raises(BadRequest):
        await lights.set_effect(reg, "office-1", "Not an effect")
    assert module.set_effect.await_count == 1


PAINT = {"id": "my-paint", "name": "My paint", "type": "static", "segments": [0, 1],
         "sequence": [[0, 100, 40], [240, 100, 40]]}


async def test_custom_painting_is_validated_and_permission_checked(make_house):
    reg, caller, devices = make_house()
    module = add_lighting_features(devices["office1"])
    with pytest.raises(Forbidden):
        await lights.custom_effect(reg, "office-1", PAINT)
    module.set_custom_effect.assert_not_awaited()
    reg, caller, devices = make_house(allow_write=frozenset({"light_effect"}))
    module = add_lighting_features(devices["office1"])
    result = await lights.custom_effect(reg, "office-1", PAINT)
    module.set_custom_effect.assert_awaited_once_with({**PAINT, "custom": 1, "enable": 1, "brightness": 50})
    assert result == {"device": "office-1", "changed": {"id": "my-paint", "name": "My paint", "type": "static",
        "brightness": 50, "enable": 1}}
    module.has_custom_effects = False
    with pytest.raises(Unsupported):
        await lights.custom_effect(reg, "office-1", PAINT)


@pytest.mark.parametrize("patch", [{"name": "bad\nname"}, {"id": "bad id"}, {"type": "unknown"},
    {"custom": 0}, {"enable": True}, {"brightness": 101}, {"token": "x"},
    {"sequence": [[0, 101, 100]]}, {"sequence": [[False, 100, 100], [0, 0, 0]]},
    {"sequence": [[0, 0]]}, {"segments": [1, 0]}, {"segments": [1]},
    {"type": "random"}, {"transition_range": [20, 10]}, {"trans_sequence": [1]},
    {"direction": 5}, {"duration": "30"}])
async def test_invalid_custom_effect_never_calls_kasa(make_house, patch):
    reg, caller, devices = make_house(allow_write=frozenset({"light_effect"}))
    module = add_lighting_features(devices["office1"])
    with pytest.raises(BadRequest):
        await lights.custom_effect(reg, "office-1", {**PAINT, **patch})
    module.set_custom_effect.assert_not_awaited()


async def test_effect_failures_and_refresh_are_reported(make_house, monkeypatch):
    reg, caller, devices = make_house(allow_write=frozenset({"light_effect"}))
    module = add_lighting_features(devices["office1"])
    for fn in ("set_effect", "set_custom_effect"):
        getattr(module, fn).side_effect = TimeoutError()
    with pytest.raises(DeviceUnavailable, match="office-1.*did not answer"):
        await lights.set_effect(reg, "office-1", "Party")
    with pytest.raises(DeviceUnavailable, match="office-1.*did not answer"):
        await lights.custom_effect(reg, "office-1", PAINT)
    module.set_effect.side_effect = None
    entry = reg.resolve_one("office-1")
    entry.online = False
    monkeypatch.setattr(reg, "refresh", AsyncMock())
    with pytest.raises(DeviceUnavailable, match="accepted the change"):
        await lights.set_effect(reg, "office-1", "Party")


async def test_mcp_feature_wrappers(make_house, mcp):
    reg, caller, devices = make_house(allow_write=frozenset(lights.LIGHT_FEATURES))
    module = add_lighting_features(devices["office1"])
    cases = [("light_features", {}), ("light_set_effect", {"effect": "Relax"}),
        ("light_set_transition", {"kind": "on", "value": 20}),
        ("light_set_transition", {"kind": "off", "value": 0}),
        ("light_set_transition", {"kind": "change", "value": True}),
        ("light_set_transition", {"kind": "all", "value": False}),
        ("light_set_length", {"segments": 30}), ("light_set_led", {"enable": True}),
        ("light_custom_effect", {"effect": PAINT})]
    async with mcp(reg, caller) as client:
        for name, arguments in cases:
            result = await client.call_tool(name, {"device": "office-1", **arguments})
            assert not result.is_error, result
    assert module.set_effect.await_count == 1


@pytest.mark.parametrize("mode", sorted(lights.AUTO_MODES))
async def test_auto_white_read_write_policy_and_refresh(make_house, monkeypatch, mode):
    reg, caller, _ = make_house(tier=2, responses={"get_auto_light_info": {
        "enable": False, "mode": mode, "password": "PRIVATE"}})
    assert await lights.get_auto_white(reg, caller, "office-1") == {
        "device": "office-1", "auto_white": {"enable": False, "mode": mode}}
    refresh = AsyncMock()
    monkeypatch.setattr(reg, "refresh", refresh)
    assert await lights.set_auto_white(reg, caller, "office-1", True, mode) == {
        "device": "office-1", "changed": {"enable": True, "mode": mode}}
    refresh.assert_awaited_once_with(reg.resolve_one("office-1"), force=True)
    await lights.set_auto_white(reg, caller, "office-1", False)
    assert caller.sent[-1][2] == {"enable": False}
    reg, caller, _ = make_house(tier=1)
    with pytest.raises(Forbidden, match="level 2"):
        await lights.set_auto_white(reg, caller, "office-1", True, mode)
    assert caller.sent == []


@pytest.mark.parametrize("enable,mode", [(True, None), (1, "circadian"), (True, "other"), (False, "other")])
async def test_auto_white_invalid(make_house, enable, mode):
    reg, caller, _ = make_house(tier=2)
    with pytest.raises(BadRequest):
        await lights.set_auto_white(reg, caller, "office-1", enable, mode)
    assert caller.sent == []


@pytest.mark.parametrize("method", ["get_auto_light_info", "set_auto_light_info", "get_preset_rules",
                                     "edit_preset_rules", "set_preset_rules", "get_segment_effect_rule"])
async def test_auto_presets_and_segment_errors(make_house, method):
    brightness_only = method == "set_preset_rules"
    response = {"brightness": [10, 50]} if brightness_only else {"states": [{"brightness": 10}]}
    reg, caller, _ = make_house(tier=2, responses={"get_preset_rules": response})
    caller.respond(method, TimeoutError())
    with pytest.raises(DeviceUnavailable, match="office-1.*did not answer"):
        if method == "get_auto_light_info":
            await lights.get_auto_white(reg, caller, "office-1")
        elif method == "set_auto_light_info":
            await lights.set_auto_white(reg, caller, "office-1", True, "circadian")
        elif method == "get_preset_rules":
            await lights.get_presets(reg, caller, "office-1")
        elif method == "get_segment_effect_rule":
            await lights.get_segment_effect(reg, caller, "office-1")
        else:
            await lights.edit_preset(reg, caller, "office-1", 0, {"brightness": 50})


@pytest.mark.parametrize("brightness_only", [False, True])
async def test_preset_edit_and_policy(make_house, brightness_only):
    response = {"brightness": [10, 50]} if brightness_only else {"start_index": 0, "sum": 2,
        "states": [{"brightness": 10}, {"brightness": 50, "color_temp": 3000, "token": "PRIVATE"}]}
    reg, caller, _ = make_house(tier=2, responses={"get_preset_rules": response})
    listed = await lights.get_presets(reg, caller, "office-1")
    assert "PRIVATE" not in str(listed)
    state = {"brightness": 30} if brightness_only else {"brightness": 30, "hue": 20, "saturation": 80}
    result = await lights.edit_preset(reg, caller, "office-1", 1, state)
    assert result == {"device": "office-1", "index": 1, "previous": {"brightness": 50, **(
        {} if brightness_only else {"color_temp": 3000})}, "changed": state}
    method = "set_preset_rules" if brightness_only else "edit_preset_rules"
    expected = {"brightness": [10, 30]} if brightness_only else {"index": 1, "state": state}
    assert caller.sent[-1] == ("office-1", method, expected)
    reg, caller, _ = make_house(tier=1, responses={"get_preset_rules": response})
    with pytest.raises(Forbidden, match="level 2"):
        await lights.edit_preset(reg, caller, "office-1", 1, state)
    assert method not in caller.methods_sent()


async def test_preset_pagination_and_child_target(make_house):
    parent = fakes.bulb("ceiling", "Ceiling")
    parent.children = [fakes.bulb("ring", "Ring")]
    reg, caller, _ = make_house(tier=2, extra=[parent])
    caller.respond("get_preset_rules", lambda e, p: {"start_index": p["start_index"], "sum": 3,
        "states": [{"brightness": 10 + p["start_index"]}]})
    result = await lights.edit_preset(reg, caller, "ring", 2, {"brightness": 75})
    assert result["previous"] == {"brightness": 12}
    assert caller.sent[-1] == ("ring", "edit_preset_rules", {"index": 2, "state": {"brightness": 75}})
    assert all(slug == "ring" for slug, _, _ in caller.sent)


@pytest.mark.parametrize("index,state", [(True, {"brightness": 30}), (-1, {"brightness": 30}),
    (128, {"brightness": 30}), (2, {"brightness": 30}), (0, {"brightness": 0}), (0, {"on": True}),
    (0, {"brightness": 30, "auto": True}), (0, {"brightness": 30, "password": "x"})])
async def test_preset_validation_never_overwrites(make_house, index, state):
    reg, caller, _ = make_house(tier=2, responses={"get_preset_rules": {"brightness": [10, 50]}})
    with pytest.raises(BadRequest):
        await lights.edit_preset(reg, caller, "office-1", index, state)
    assert all(method == "get_preset_rules" for method in caller.methods_sent())


async def test_segment_effect_only_returns_decoded_metadata(make_house):
    reg, caller, _ = make_house(tier=0, responses={"get_segment_effect_rule": {
        "id": "effect1", "name": "Rain\nfall", "type": "breathe", "enable": 0, "brightness": 30,
        "states": [[1, 2, 3]], "display_colors": [[1, 2, 3]], "meshGroupTokenId": "PRIVATE",
        "password": "PRIVATE", "ssid": "PRIVATE"}})
    assert await lights.get_segment_effect(reg, caller, "office-1") == {"device": "office-1", "segment_effect": {
        "id": "effect1", "name": "Rain fall", "type": "breathe", "enable": 0, "brightness": 30}}


async def test_mcp_auto_white_presets_and_segment(make_house, mcp):
    reg, caller, _ = make_house(tier=2, responses={"get_preset_rules": {"states": [{"brightness": 40}]}})
    async with mcp(reg, caller) as client:
        for name, arguments in [("light_get_auto_white", {}), ("light_set_auto_white", {"enable": True, "mode": "light_track"}),
            ("light_get_presets", {}), ("light_edit_preset", {"index": 0, "state": {"brightness": 50}}),
            ("light_get_segment_effect", {})]:
            result = await client.call_tool(name, {"device": "office-1", **arguments})
            assert not result.is_error, result


async def test_false_response_and_failed_refresh_never_claim_success(make_house, monkeypatch):
    reg, caller, _ = make_house(tier=2, responses={"set_auto_light_info": False, "add_schedule_rule": {"error_code": -1}})
    with pytest.raises(DeviceUnavailable, match="refused"):
        await lights.set_auto_white(reg, caller, "office-1", False)
    with pytest.raises(DeviceUnavailable, match="refused"):
        await lights.change_rule(reg, caller, "office-1", "schedule", SCHEDULE)
    caller.respond("set_auto_light_info", {})
    reg.resolve_one("office-1").online = False
    monkeypatch.setattr(reg, "refresh", AsyncMock())
    with pytest.raises(DeviceUnavailable, match="could not be refreshed"):
        await lights.set_auto_white(reg, caller, "office-1", False)


# Sanitized lighting-only projections of the real L430C fixtures. No fixture-path dependency.
L430C_DEFAULTS = {"re_power_type": "always_on", "state": {
    "brightness": 100, "color_temp": 2700, "hue": 240, "saturation": 100}, "type": "last_states"}
L430C_PRESETS = {"start_index": 0, "sum": 7, "states": [
    {"brightness": 50, "color_temp": 2700, "hue": 0, "saturation": 100},
    {"brightness": 100, "color_temp": 0, "hue": 240, "saturation": 100},
    {"brightness": 100, "color_temp": 0, "hue": 0, "saturation": 100},
    {"brightness": 100, "color_temp": 0, "hue": 120, "saturation": 100},
    {"brightness": 100, "color_temp": 0, "hue": 277, "saturation": 86},
    {"brightness": 100, "color_temp": 0, "hue": 60, "saturation": 100},
    {"brightness": 100, "color_temp": 0, "hue": 300, "saturation": 100},
]}
L430C_RULES = {
    "schedule": {"enable": False, "rule_list": [], "schedule_rule_max_count": 32, "start_index": 0, "sum": 0},
    "countdown": {"countdown_rule_max_count": 1, "enable": False, "rule_list": []},
    "antitheft": {"antitheft_rule_max_count": 1, "enable": False, "rule_list": []},
}


async def test_real_l430c_read_shapes_and_supported_features(make_house):
    reg, caller, devices = make_house(tier=2, responses={
        "get_device_info": {"default_states": L430C_DEFAULTS, "segment_effect": {
            "brightness": 0, "custom": 0, "display_colors": [], "enable": 0, "id": "", "name": ""}},
        "get_preset_rules": L430C_PRESETS, "get_next_event": {},
        **{f"get_{kind}_rules": value for kind, value in L430C_RULES.items()},
    })
    assert await lights.get_defaults(reg, caller, "office-1") == {
        "device": "office-1", "default_states": L430C_DEFAULTS}
    assert await lights.get_presets(reg, caller, "office-1") == {"device": "office-1", "presets": L430C_PRESETS}
    assert await lights.next_event(reg, caller, "office-1") == {"device": "office-1"}
    assert await lights.get_segment_effect(reg, caller, "office-1") == {"device": "office-1", "segment_effect": {
        "brightness": 0, "custom": 0, "enable": 0}}
    assert "get_segment_effect_rule" not in caller.methods_sent()
    for kind in L430C_RULES:
        expected = {k: v for k, v in L430C_RULES[kind].items() if k != "rule_list"}
        assert await lights.list_rules(reg, caller, "office-1", kind) == {
            "device": "office-1", "kind": kind, "rules": expected}
        assert caller.sent[-1][2] == {"start_index": 0}
    await lights.set_defaults(reg, caller, "office-1", {"re_power_type": "last_states"})
    assert caller.sent[-1][2]["default_states"] == {**L430C_DEFAULTS, "re_power_type": "last_states"}
    await lights.edit_preset(reg, caller, "office-1", 0, L430C_PRESETS["states"][0])
    assert caller.sent[-1][2] == {"index": 0, "state": L430C_PRESETS["states"][0]}
    # The real fixture exposes on/off fades but no light_effect or LED feature.
    for kind in ("on", "off"):
        devices["office1"].features[f"smooth_transition_{kind}"] = fakes.FakeFeature(
            f"smooth_transition_{kind}", Feature.Type.Number, value=1, hi=60)
    result = await lights.get_features(reg, "office-1")
    assert set(result["features"]) == {"smooth_transition_on", "smooth_transition_off"}
    unknown = DeviceError("Error querying device: 192.0.2.1: UNKNOWN_METHOD_ERROR(-1002)",
                          error_code=SmartErrorCode.UNKNOWN_METHOD_ERROR)
    caller.respond("get_auto_light_info", unknown)
    with pytest.raises(Unsupported, match="office-1 does not support get_auto_light_info") as failure:
        await lights.get_auto_white(reg, caller, "office-1")
    assert "192.0.2.1" not in str(failure.value)
    # The L430C must be refused before set_auto_light_info is ever sent.
    with pytest.raises(Unsupported):
        await lights.set_auto_white(reg, caller, "office-1", True, "circadian")
    assert "set_auto_light_info" not in caller.methods_sent()


async def test_models_without_a_method_are_refused_without_a_write(make_house):
    unknown = DeviceError("UNKNOWN_METHOD_ERROR", error_code=SmartErrorCode.UNKNOWN_METHOD_ERROR)
    reg, caller, devices = make_house(tier=2, allow_write=frozenset({"light_effect"}), responses={
        "get_preset_rules": unknown, "get_device_info": {"device_on": True}, "get_schedule_rules": unknown,
        "add_countdown_rule": unknown})
    with pytest.raises(Unsupported):
        await lights.edit_preset(reg, caller, "office-1", 0, {"brightness": 40})
    with pytest.raises(Unsupported):
        await lights.get_presets(reg, caller, "office-1")
    with pytest.raises(Unsupported):
        await lights.set_defaults(reg, caller, "office-1", {"re_power_type": "always_on"})
    with pytest.raises(Unsupported):
        await lights.list_rules(reg, caller, "office-1", "schedule")
    with pytest.raises(Unsupported, match="add_countdown_rule"):
        await lights.change_rule(reg, caller, "office-1", "countdown", COUNTDOWN)
    # No lighting_effect module, as on the L430C: nothing reaches the device.
    with pytest.raises(NotFound):
        await lights.set_effect(reg, "office-1", "Party")
    with pytest.raises(NotFound):
        await lights.custom_effect(reg, "office-1", PAINT)
    assert [m for m in caller.methods_sent() if not m.startswith("get_")] == ["add_countdown_rule"]
    # Other device errors stay errors that name the device.
    caller.respond("get_preset_rules", DeviceError("x", error_code=SmartErrorCode.PARAMS_ERROR))
    with pytest.raises(DeviceUnavailable, match="office-1"):
        await lights.get_presets(reg, caller, "office-1")


async def test_rule_conflicts_and_contradictory_actions(make_house):
    reg, caller, _ = make_house(tier=2, responses={"add_schedule_rule": {"id": "new", "conflict_id": "old"}})
    with pytest.raises(BadRequest, match="conflicting"):
        await lights.change_rule(reg, caller, "office-1", "schedule", SCHEDULE)
    for kind, key, value in (("schedule", "s_action", "off"), ("countdown", "action", "on")):
        with pytest.raises(BadRequest, match="must agree"):
            await lights.change_rule(reg, caller, "office-1", kind, {**RULES[kind], key: value})


async def test_every_device_text_is_cleaned_and_empty_nested_fields_are_omitted(make_house):
    caller_result = {"rule_list": [{"id": "r1", "name": "  a\n" + "z" * 200,
        "desired_states": {"password": "PRIVATE"}, "wifi": "PRIVATE", "mac": "PRIVATE", "token": "PRIVATE"}]}
    reg, caller, _ = make_house(responses={"get_schedule_rules": caller_result})
    result = await lights.list_rules(reg, caller, "office-1", "schedule")
    assert result["rules"]["rule_list"] == [{"id": "r1", "name": "a " + "z" * 126}]
    assert "PRIVATE" not in str(result)


async def test_missing_rule_and_unsupported_feature_do_not_write(make_house):
    reg, caller, _ = make_house(tier=2)
    with pytest.raises(NotFound):
        await lights.enable_rule(reg, caller, "office-1", "schedule", "missing", True)
    with pytest.raises(NotFound):
        await lights.set_option(reg, "office-1", "led", True)
    assert all(method.startswith("get_") for method in caller.methods_sent())


@pytest.mark.parametrize("rule_id", ["", "x y", "x\ny", "x" * 129, "*", "bad/token", True])
async def test_rule_identifiers_are_strict(make_house, rule_id):
    reg, caller, _ = make_house(tier=2)
    with pytest.raises(BadRequest):
        await lights.delete_rule(reg, caller, "office-1", "schedule", rule_id)
    with pytest.raises(BadRequest):
        await lights.enable_rule(reg, caller, "office-1", "schedule", rule_id, True)
    assert caller.sent == []


async def test_partial_brightness_preset_list_cannot_overwrite_other_slots(make_house):
    reg, caller, _ = make_house(tier=2, responses={"get_preset_rules": {"brightness": [10, 50], "sum": 4}})
    with pytest.raises(Unsupported, match="incomplete brightness"):
        await lights.edit_preset(reg, caller, "office-1", 0, {"brightness": 40})
    assert caller.methods_sent() == ["get_preset_rules"]
