import pytest
from kasa import DeviceType

from tolpo.areas import doorbell
from tolpo.registry import BadRequest, DeviceUnavailable, Forbidden

from ..fakes import FakeDevice

pytestmark = pytest.mark.anyio


def house(make_house, *, child=False, **kwargs):
    bell = FakeDevice("bell-test", "Front bell", "D230", DeviceType.Doorbell)
    reg, caller, devices = make_house(extra=[bell], **kwargs)
    if child:
        devices["hub"].children.append(bell)
        reg._upsert(bell, reg.resolve_one("tapo-h200"))
    return reg, caller, bell


async def test_events_are_observations_compact_and_repeatable(make_house):
    reg, caller, _ = house(make_house, child=True)
    caller.respond("getLastAlarmInfo", {"system": {"last_alarm_info": {"last_alarm_time": "101", "last_alarm_type": "ring\n press", "key": "SECRET"}}})
    caller.respond("getCallStateInfo", {"call_manager": {"call_state_info": {"call_state": "idle", "token": "SECRET"}}})
    caller.respond("get_trigger_logs", {"logs": [{"id": 1, "timestamp": 102, "event": "press\n button", "params": {"mac": "SECRET"}}, {"timestamp": 99}]})
    out = await doorbell.events_recent(reg, caller, "front-bell", since=100, hub="tapo-h200")
    assert out == {"device": "front-bell", "unverified": True, "cursor": 102, "events": [{"source": "last_alarm", "timestamp": 101, "last_alarm_type": "ring press"}, {"source": "hub_trigger", "id": 1, "timestamp": 102, "event": "press button"}], "call_state": {"call_state": "idle"}}
    assert caller.sent == [("front-bell", "getLastAlarmInfo", {"system": {"name": "last_alarm_info"}}), ("front-bell", "getCallStateInfo", {"call_manager": {"name": "call_state_info"}}), ("front-bell", "get_trigger_logs", {"start_id": 0, "page_size": 10})]
    assert "events" not in await doorbell.events_recent(reg, caller, "front-bell", since=102, hub="tapo-h200")


async def test_poll_keeps_partial_results_and_reports_failures(make_house):
    reg, caller, _ = house(make_house, tier=0)
    caller.respond("getLastAlarmInfo", DeviceUnavailable("front-bell did not answer in time"))
    caller.respond("getCallStateInfo", {"call_state": "incoming"})
    out = await doorbell.events_recent(reg, caller, "front-bell")
    assert out["call_state"] == {"call_state": "incoming"}
    assert out["errors"] == [{"device": "front-bell", "method": "getLastAlarmInfo", "reason": "front-bell: getLastAlarmInfo: device did not answer in time"}]
    assert "events" not in out


async def test_event_inputs_and_kind_refused_before_wire(make_house):
    reg, caller, _ = house(make_house)
    for params in ({"limit": 0}, {"limit": 51}, {"since": -1}, {"since": True}, {"hub": "desk-lamp"}):
        with pytest.raises(BadRequest):
            await doorbell.events_recent(reg, caller, "front-bell", **params)
    with pytest.raises(BadRequest):
        await doorbell.events_recent(reg, caller, "hallway-cam")
    assert not caller.sent


async def test_poll_does_not_hide_policy_refusal(make_house, monkeypatch):
    reg, caller, _ = house(make_house)
    async def denied(*args, **kwargs):
        raise Forbidden("owner blocks observation")
    monkeypatch.setattr(caller, "call", denied)
    with pytest.raises(Forbidden):
        await doorbell.events_recent(reg, caller, "front-bell")


async def test_poll_mcp(make_house, mcp):
    reg, caller, _ = house(make_house, tier=0)
    async with mcp(reg, caller) as client:
        result = await client.call_tool("doorbell_events_recent", {"device": "front-bell"})
    assert not result.is_error
    assert result.structured_content == {"device": "front-bell", "unverified": True, "cursor": 0}


def chime_house(make_house, **kwargs):
    chime = FakeDevice("chime-test", "Hall chime", "CHIME", DeviceType.Chime)
    reg, caller, bell = house(make_house, **kwargs)
    bell.mac = "02:00:00:00:00:01"
    reg._adopt(chime)
    caller.respond("get_support_alarm_type_list", {"alarm_type_list": ["ding", "bell"]})
    return reg, caller


async def test_chime_settings_read_and_write_refresh_hide_address(make_house, monkeypatch):
    reg, caller = chime_house(make_house, tier=2)
    caller.respond("get_chime_alarm_configure", {"mac": "SECRET", "type": "ding\n tone", "volume": "normal", "duration": 5, "on_off": 0, "key": "SECRET", "empty": ""})
    out = await doorbell.chime_get(reg, caller, "hall-chime", "front-bell")
    assert out == {"device": "hall-chime", "doorbell": "front-bell", "unverified": True, "settings": {"type": "ding tone", "volume": "normal", "duration": 5, "on_off": 0}, "tones": ["ding", "bell"]}
    refreshed = []
    async def refresh(entry, *, force):
        refreshed.append((entry.slug, force))
    monkeypatch.setattr(reg, "refresh", refresh)
    result = await doorbell.chime_set(reg, caller, "hall-chime", "front-bell", "ding", "low", 5, True)
    assert result["changed"] == {"type": "ding", "volume": "low", "duration": 5, "on_off": 1}
    assert caller.sent[-1] == ("hall-chime", "set_chime_alarm_configure", {"mac": "02:00:00:00:00:01", "type": "ding", "volume": "low", "duration": 5, "on_off": 1})
    assert refreshed == [("hall-chime", True)]


@pytest.mark.parametrize("device", ["hall-chime", "tapo-h200"])
async def test_chime_and_hub_sound_confirmation(make_house, device):
    from tolpo.calls import NeedsConfirmation
    reg, caller = chime_house(make_house, tier=3)
    for action, method in (("play", "play_alarm"), ("stop", "stop_alarm")):
        for confirm in (None, f"{method} wrong-device"):
            with pytest.raises(NeedsConfirmation):
                await doorbell.chime_sound_action(reg, caller, device, action, "ding", confirm=confirm)
        assert method not in caller.methods_sent()
        out = await doorbell.chime_sound_action(reg, caller, device, action, "ding", confirm=f"{method} {device}")
        assert out["changed"] == ("sound played" if action == "play" else "sound stopped")
    assert caller.sent[-1] == (device, "stop_alarm", {})


async def test_chime_refusal_error_and_validation(make_house):
    reg, caller = chime_house(make_house, tier=1)
    with pytest.raises(Forbidden, match="level 2"):
        await doorbell.chime_set(reg, caller, "hall-chime", "front-bell", "ding", "low", 5, True)
    with pytest.raises(Forbidden, match="level 3"):
        await doorbell.chime_sound_action(reg, caller, "hall-chime", "play", "ding", confirm="play_alarm hall-chime")
    assert not {"set_chime_alarm_configure", "play_alarm"} & set(caller.methods_sent())
    for tone, volume, duration, enabled in (("missing", "low", 5, True), ("ding", "invalid", 5, True), ("ding", "low", 0, True), ("ding", "low", True, True), ("ding\n", "low", 5, True), ("ding", "low", 5, 1)):
        with pytest.raises(BadRequest):
            await doorbell.chime_set(reg, caller, "hall-chime", "front-bell", tone, volume, duration, enabled)
    caller.respond("get_chime_alarm_configure", DeviceUnavailable("SECRET key"))
    with pytest.raises(DeviceUnavailable, match="get_chime_alarm_configure") as caught:
        await doorbell.chime_get(reg, caller, "hall-chime", "front-bell")
    assert "SECRET" not in str(caught.value)


async def test_chime_mcp(make_house, mcp):
    reg, caller = chime_house(make_house, tier=3)
    async with mcp(reg, caller) as client:
        for name, params in (("chime_settings_get", {"device": "hall-chime", "doorbell": "front-bell"}), ("chime_settings_set", {"device": "hall-chime", "doorbell": "front-bell", "tone": "ding", "volume": "low", "duration": 5, "enabled": True}), ("chime_sound", {"device": "hall-chime", "action": "play", "tone": "ding", "confirm": "play_alarm hall-chime"})):
            assert not (await client.call_tool(name, params)).is_error


def ready_house(make_house, **kwargs):
    reg, caller, bell = house(make_house, child=True, **kwargs)
    bell.mac = "02:00:00:00:00:01"
    reg._adopt(FakeDevice("chime-test", "Hall chime", "CHIME", DeviceType.Chime))
    caller.respond("get_support_alarm_type_list", {"alarm_type_list": ["ding"]})
    caller.respond("getQuickRespList", {"quick_response": {"quick_resp_audio": [{"reply_1": {"id": "1", "index": "0", "name": "Hello\n visitor", "duration": "3", "read_only": "1", "password": "SECRET"}}]}})
    for method, value in {
        "getSirenConfig": {"duration": 300, "siren_type": "Doorbell Ring 10", "volume": "6"},
        "getSirenStatus": {"status": "off", "time_left": 0},
        "getSirenTypeList": {"siren_type_list": ["Doorbell Ring 1", "Doorbell Ring 10", "Phone Ring"]},
    }.items():
        caller.respond(method, value)
    return reg, caller


# Expected tiers are independent of the area catalog so a risk downgrade fails this test.
READ_METHODS = {
    "getLastAlarmInfo", "getCallStateInfo", "get_trigger_logs", "get_chime_alarm_configure", "get_support_alarm_type_list",
    "getRingStatus", "getRingSoundConfig", "getRingTypeList", "getRingPlay", "getHubRingStatus", "getHubRingSoundConfig", "getHubRingTypeList", "getHubRingPlay", "getHubRingPlan", "getChimeRingPlan", "getExistingChimeCfg", "getQuickRespList", "getSmartRespStatus", "getSmartRespConfig", "getAntiTheftEnable", "getAntiTheftStatus", "getWakeUpConfig", "getBatteryOperatingMode", "getLedColor", "getSirenTypeList",
}
SETTING_METHODS = {
    "set_chime_alarm_configure", "setRingSoundConfig", "setHubRingSoundConfig", "setRingStatus", "setHubRingStatus", "setHubRingPlan", "setChimeRingPlan", "setSmartRespConfig", "setSmartRespStatus", "setWakeUpConfig", "setBatteryOperatingMode", "setLedColor", "setPreWakeup",
}
SECURITY_METHODS = {"play_alarm", "stop_alarm", "setRingPlay", "setHubRingPlay", "setExistingChimeCfg", "playQuickResp", "stopQuickResp", "setAntiTheftEnable"}


def method_device(method):
    if method in {"get_chime_alarm_configure", "get_support_alarm_type_list", "set_chime_alarm_configure", "play_alarm", "stop_alarm"}:
        return "hall-chime"
    if method in {"setPreWakeup", "getSirenTypeList"}:
        return "tapo-h200"
    return "front-bell"


async def test_declared_tiers_and_kind_boundaries(make_house):
    reg, caller = ready_house(make_house, tier=4)
    expected = {**dict.fromkeys(READ_METHODS, 0), **dict.fromkeys(SETTING_METHODS, 2), **dict.fromkeys(SECURITY_METHODS, 3)}
    assert {rule.method: int(rule.tier) for rule in doorbell.METHODS} == expected
    for method in expected:
        with pytest.raises(Forbidden):
            await caller.call(reg.resolve_one("desk-lamp"), method)
    assert not caller.sent


@pytest.mark.parametrize("method", sorted(SETTING_METHODS | SECURITY_METHODS))
async def test_every_write_refused_below_level(make_house, method):
    reg, caller = ready_house(make_house, tier=1)
    device = method_device(method)
    with pytest.raises(Forbidden):
        await caller.call(reg.resolve_one(device), method, confirm=f"{method} {device}")
    assert not caller.sent


@pytest.mark.parametrize("method", sorted(SECURITY_METHODS))
async def test_every_security_method_needs_its_exact_confirmation(make_house, method):
    from tolpo.calls import NeedsConfirmation
    reg, caller = ready_house(make_house, tier=3)
    entry = reg.resolve_one(method_device(method))
    for confirm in (None, f"{method} another-device", f"another-method {entry.slug}"):
        with pytest.raises(NeedsConfirmation):
            await caller.call(entry, method, confirm=confirm)
    assert not caller.sent
    await caller.call(entry, method, confirm=f"{method} {entry.slug}")
    assert caller.methods_sent() == [method]


@pytest.mark.parametrize("method", sorted(READ_METHODS | SETTING_METHODS | SECURITY_METHODS))
async def test_every_method_reports_device_error_without_secrets(make_house, method):
    reg, caller = ready_house(make_house, tier=3)
    device = method_device(method)
    caller.respond(method, DeviceUnavailable("rejected SECRET password token 02:00:00:00:00:01"))
    with pytest.raises(DeviceUnavailable) as caught:
        await doorbell._call(caller, reg.resolve_one(device), method, confirm=f"{method} {device}")
    assert method in str(caught.value) and device in str(caught.value)
    assert "SECRET" not in str(caught.value) and "02:00" not in str(caught.value)


@pytest.mark.parametrize("setting", sorted(doorbell.READS))
async def test_setting_reads_hide_extra_fields_and_flatten_text(make_house, setting):
    reg, caller = ready_house(make_house, tier=0)
    for method, module, section, fields in doorbell.READS[setting]:
        value = {field: "text\n value" for field in fields}
        value.update({"key": "SECRET", "mac_addr": "SECRET", "ssid": "SECRET", "setup_code": "SECRET", "token": "SECRET", "nested": {"password": "SECRET"}})
        if method.endswith("TypeList"):
            value = ["ding\n tone", "", {"key": "SECRET"}]
        caller.respond(method, {module: {section: value}})
    out = await doorbell.read_setting(reg, caller, "front-bell", setting)
    assert out["values"]
    assert "SECRET" not in str(out) and "\\n" not in str(out)
    assert caller.methods_sent() == [item[0] for item in doorbell.READS[setting]]


# These cases exercise every tool and branch through the MCP signature and real caller policy.
TOOL_CASES = [
    ("doorbell_events_recent", {"device": "front-bell"}, 0, "getLastAlarmInfo"),
    ("chime_settings_get", {"device": "hall-chime", "doorbell": "front-bell"}, 0, "get_chime_alarm_configure"),
    ("chime_settings_set", {"device": "hall-chime", "doorbell": "front-bell", "tone": "ding", "volume": "low", "duration": 5, "enabled": True}, 2, "set_chime_alarm_configure"),
    ("chime_sound", {"device": "hall-chime", "action": "play", "tone": "ding", "confirm": "play_alarm hall-chime"}, 3, "play_alarm"),
    ("chime_sound", {"device": "hall-chime", "action": "stop", "confirm": "stop_alarm hall-chime"}, 3, "stop_alarm"),
    ("doorbell_settings_get", {"device": "front-bell", "setting": "ring"}, 0, "getRingStatus"),
    ("doorbell_ring_sound_set", {"device": "front-bell", "target": "doorbell", "tone": "ding", "volume": 1}, 2, "setRingSoundConfig"),
    ("doorbell_ring_sound_set", {"device": "front-bell", "target": "hub", "tone": "ding", "volume": 6}, 2, "setHubRingSoundConfig"),
    ("doorbell_ring_enabled_set", {"device": "front-bell", "target": "doorbell", "enabled": False}, 2, "setRingStatus"),
    ("doorbell_ring_enabled_set", {"device": "front-bell", "target": "hub", "enabled": True}, 2, "setHubRingStatus"),
    ("doorbell_ring_preview", {"device": "front-bell", "target": "doorbell", "action": "play", "tone": "ding", "confirm": "setRingPlay front-bell"}, 3, "setRingPlay"),
    ("doorbell_ring_preview", {"device": "front-bell", "target": "hub", "action": "stop", "confirm": "setHubRingPlay front-bell"}, 3, "setHubRingPlay"),
    ("doorbell_ring_plan_set", {"device": "front-bell", "target": "hub", "enabled": True, "schedule": "0900-1700,127"}, 2, "setHubRingPlan"),
    ("doorbell_ring_plan_set", {"device": "front-bell", "target": "chime", "enabled": False, "schedule": "0000-0000,127"}, 2, "setChimeRingPlan"),
    ("doorbell_existing_chime_set", {"device": "front-bell", "chime_type": "mechanical", "ring_length": 0, "confirm": "setExistingChimeCfg front-bell"}, 3, "setExistingChimeCfg"),
    ("doorbell_quick_replies", {"device": "front-bell"}, 0, "getQuickRespList"),
    ("doorbell_quick_reply", {"device": "front-bell", "action": "play", "reply_id": "1", "confirm": "playQuickResp front-bell"}, 3, "playQuickResp"),
    ("doorbell_quick_reply", {"device": "front-bell", "action": "stop", "confirm": "stopQuickResp front-bell"}, 3, "stopQuickResp"),
    ("doorbell_auto_response_set", {"device": "front-bell", "reply_id": "1", "delay": 5}, 2, "setSmartRespConfig"),
    ("doorbell_auto_response_set", {"device": "front-bell", "enabled": False}, 2, "setSmartRespStatus"),
    ("doorbell_anti_theft_set", {"device": "front-bell", "enabled": True, "confirm": "setAntiTheftEnable front-bell"}, 3, "setAntiTheftEnable"),
    ("doorbell_wake_up_set", {"device": "front-bell", "trigger": "doorbell"}, 2, "setWakeUpConfig"),
    ("doorbell_pre_wake", {"device": "front-bell"}, 2, "setPreWakeup"),
    ("doorbell_battery_mode_set", {"device": "front-bell", "mode": "battery_life_first"}, 2, "setBatteryOperatingMode"),
    ("doorbell_button_color_set", {"device": "front-bell", "color": "blue"}, 2, "setLedColor"),
]


@pytest.mark.parametrize("name,params,level,method", TOOL_CASES)
async def test_tools_happy_paths_refresh_and_annotations(make_house, mcp, monkeypatch, name, params, level, method):
    reg, caller = ready_house(make_house, tier=3)
    refreshed = []
    async def refresh(entry, *, force):
        refreshed.append((entry.slug, force))
    monkeypatch.setattr(reg, "refresh", refresh)
    async with mcp(reg, caller) as client:
        registered = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert registered[name].annotations.read_only_hint == (level == 0)
        if level:
            assert registered[name].annotations.destructive_hint == (level >= 3)
        result = await client.call_tool(name, params)
    assert not result.is_error, result.content
    assert method in caller.methods_sent()
    assert bool(refreshed) == (level > 0)
    assert all(force for _, force in refreshed)
    assert "SECRET" not in str(result.structured_content)
    assert "mac" not in str(result.structured_content).lower()  # no address returned after writes


@pytest.mark.parametrize("name,params,level,method", TOOL_CASES)
async def test_tools_device_error(make_house, mcp, name, params, level, method):
    reg, caller = ready_house(make_house, tier=3)
    caller.respond(method, DeviceUnavailable("SECRET backend failure"))
    async with mcp(reg, caller) as client:
        result = await client.call_tool(name, params)
    if name == "doorbell_events_recent":
        assert result.structured_content["errors"]
    else:
        assert result.is_error and method in result.content[0].text
    assert "SECRET" not in str(result)


@pytest.mark.parametrize("name,params,level,method", [case for case in TOOL_CASES if case[2] > 0])
async def test_tools_owner_policy_and_confirmation(make_house, mcp, name, params, level, method):
    reg, caller = ready_house(make_house, tier=1)
    async with mcp(reg, caller) as client:
        denied = await client.call_tool(name, params)
    assert denied.is_error
    assert not set(caller.methods_sent()) & (SETTING_METHODS | SECURITY_METHODS)
    assert "allow_methods" not in denied.content[0].text
    if level == 3:
        reg, caller = ready_house(make_house, tier=3)
        unconfirmed = {key: value for key, value in params.items() if key != "confirm"}
        async with mcp(reg, caller) as client:
            denied = await client.call_tool(name, unconfirmed)
        assert denied.is_error and "confirm=" in denied.content[0].text
        assert not set(caller.methods_sent()) & (SETTING_METHODS | SECURITY_METHODS)


async def test_exact_write_shapes(make_house):
    reg, caller = ready_house(make_house, tier=3)
    await doorbell.ring_configure(reg, caller, "front-bell", "hub", "ding", 6)
    await doorbell.ring_enable(reg, caller, "front-bell", "doorbell", False)
    await doorbell.ring_preview(reg, caller, "front-bell", "doorbell", "play", "ding", "setRingPlay front-bell")
    await doorbell.ring_plan(reg, caller, "front-bell", "chime", True, "0000-0000,127")
    await doorbell.existing_chime(reg, caller, "front-bell", "digital", 2, "setExistingChimeCfg front-bell")
    await doorbell.quick_reply_action(reg, caller, "front-bell", "play", "1", "playQuickResp front-bell")
    await doorbell.auto_response_configure(reg, caller, "front-bell", "1", 5)
    await doorbell.auto_response(reg, caller, "front-bell", enabled=True)
    await doorbell.anti_theft(reg, caller, "front-bell", True, "setAntiTheftEnable front-bell")
    await doorbell.wake_up(reg, caller, "front-bell", "detection")
    await doorbell.pre_wake(reg, caller, "front-bell")
    await doorbell.battery_mode(reg, caller, "front-bell", "super_power_saving", False)
    await doorbell.button_color(reg, caller, "front-bell", "blue")
    writes = [(method, params) for _, method, params in caller.sent if method in SETTING_METHODS | SECURITY_METHODS]
    assert writes == [
        ("setHubRingSoundConfig", {"hub_ring": {"config": {"ring_type": "ding", "volume": "6"}}}),
        ("setRingStatus", {"ring": {"status": {"enabled": "off"}}}),
        ("setRingPlay", {"ring": {"play": {"status": "on", "ring_type": "ding"}}}),
        ("setChimeRingPlan", {"chime_ring_plan": {"chn1_chime_ring_plan": {"enabled": "on", "ring_plan_1": "0000-0000,127"}}}),
        ("setExistingChimeCfg", {"existing_chime": {"config": {"type": "digital", "ring_length": 2}}}),
        ("playQuickResp", {"quick_response": {"play_quick_resp_audio": {"id": "1", "force": "0"}}}),
        ("setSmartRespConfig", {"quick_response": {"smart_resp_config": {"id": "1", "play_interval": "5"}}}),
        ("setSmartRespStatus", {"quick_response": {"smart_resp_status": {"enabled": "on"}}}),
        ("setAntiTheftEnable", {"anti_theft": {"config": {"enabled": "on"}}}),
        ("setWakeUpConfig", {"wake_up": {"config": {"wake_up_type": "detection"}}}),
        ("setPreWakeup", {"preWakeUp": {"child_device_list": [{"device_id": "bell-test"}]}}),
        ("setBatteryOperatingMode", {"battery": {"operating": {"mode": "super_power_saving", "follow_config": False}}}),
        ("setLedColor", {"led": {"color": {"name": "blue"}}}),
    ]


async def test_h200_fixture_tones_and_siren_left_to_the_hub_area(make_house):
    from kasa.protocols.smartcamprotocol import SmartCamProtocol
    reg, caller = ready_house(make_house, tier=3)
    reg.resolve_one("tapo-h200").device.protocol = object.__new__(SmartCamProtocol)
    assert await doorbell._available_tones(caller, reg.resolve_one("tapo-h200")) == ["Doorbell Ring 1", "Doorbell Ring 10", "Phone Ring"]
    assert caller.sent == [("tapo-h200", "getSirenTypeList", {"siren": {}})]
    with pytest.raises(BadRequest, match="hub_siren"):
        await doorbell.chime_sound_action(reg, caller, "tapo-h200", "play", "ding", confirm="play_alarm tapo-h200")
    assert "get_support_alarm_type_list" not in caller.methods_sent()
    # The hub siren is the hub area's: this area never rewrites the hub's saved alarm sound or sounds it.
    declared = {rule.method for rule in doorbell.METHODS}
    assert not declared & {"setSirenConfig", "setSirenStatus", "getSirenConfig", "getSirenStatus"}


INVALID_CALLS = [
    (doorbell.ring_configure, ("front-bell", "doorbell", "ding", 101)),
    (doorbell.ring_configure, ("front-bell", "doorbell", "ding", True)),
    (doorbell.ring_enable, ("front-bell", "hub", "on")),
    (doorbell.ring_plan, ("front-bell", "hub", True, "2500-1700,127")),
    (doorbell.ring_plan, ("front-bell", "hub", True, "0900-1760,127")),
    (doorbell.ring_plan, ("front-bell", "hub", True, "0900-1700,128")),
    (doorbell.ring_plan, ("front-bell", "hub", True, "0900-1700,0")),
    (doorbell.existing_chime, ("front-bell", "unknown", 1)),
    (doorbell.existing_chime, ("front-bell", "mechanical", 11)),
    (doorbell.quick_reply_action, ("front-bell", "play", "unknown")),
    (doorbell.auto_response_configure, ("front-bell", "1", -1)),
    (doorbell.auto_response_configure, ("front-bell", "unknown", 5)),
    (doorbell.anti_theft, ("front-bell", 1, "setAntiTheftEnable front-bell")),
    (doorbell.auto_response, ("front-bell", 1)),
    (doorbell.auto_response, ("front-bell",)),
    (doorbell.auto_response, ("front-bell", None, "1")),
    (doorbell.auto_response, ("front-bell", True, "1", 5)),
    (doorbell.wake_up, ("front-bell", "unknown")),
    (doorbell.battery_mode, ("front-bell", "unknown", False)),
    (doorbell.battery_mode, ("front-bell", "battery_life_first", 1)),
    (doorbell.button_color, ("front-bell", "blue\n")),
    (doorbell.button_color, ("front-bell", "x" * 33)),
]


@pytest.mark.parametrize("fn,args", INVALID_CALLS)
async def test_invalid_input_cannot_write(make_house, fn, args):
    reg, caller = ready_house(make_house, tier=3)
    with pytest.raises(BadRequest):
        await fn(reg, caller, *args)
    assert not set(caller.methods_sent()) & (SETTING_METHODS | SECURITY_METHODS)


async def test_parent_hub_constraints(make_house):
    reg, caller, _ = house(make_house)
    with pytest.raises(BadRequest):
        await doorbell.pre_wake(reg, caller, "front-bell")
    with pytest.raises(BadRequest):
        await doorbell.events_recent(reg, caller, "front-bell", hub="tapo-h200")
    assert not caller.sent


async def test_quick_reply_compact_output_and_empty_fields(make_house):
    reg, caller = ready_house(make_house)
    out = await doorbell.quick_replies(reg, caller, "front-bell")
    assert out == {"device": "front-bell", "unverified": True, "replies": [{"id": "1", "index": "0", "name": "Hello visitor", "duration": "3", "read_only": "1"}]}
    caller.respond("getQuickRespList", {"quick_response": {"quick_resp_audio": []}})
    assert "replies" not in await doorbell.quick_replies(reg, caller, "front-bell")


async def test_smartcam_hub_per_doorbell_chime_wrapper(make_house):
    from kasa.protocols.smartcamprotocol import SmartCamProtocol
    reg, caller = ready_house(make_house, tier=2)
    reg.resolve_one("tapo-h200").device.protocol = object.__new__(SmartCamProtocol)
    caller.respond("get_chime_alarm_configure", {"chime": {"type": "Doorbell Ring 10", "volume": "normal", "duration": 5, "on_off": 1, "mac": "SECRET"}})
    out = await doorbell.chime_get(reg, caller, "tapo-h200", "front-bell")
    assert out["settings"]["type"] == "Doorbell Ring 10" and "SECRET" not in str(out)
    assert caller.sent[0] == ("tapo-h200", "get_chime_alarm_configure", {"chime": {"mac": "02:00:00:00:00:01"}})
    await doorbell.chime_set(reg, caller, "tapo-h200", "front-bell", "Doorbell Ring 10", "normal", 5, True)
    assert caller.sent[-1] == ("tapo-h200", "set_chime_alarm_configure", {"chime": {"mac": "02:00:00:00:00:01", "type": "Doorbell Ring 10", "volume": "normal", "duration": 5, "on_off": 1}})
    assert "get_support_alarm_type_list" not in caller.methods_sent()


async def test_cached_address_required_without_exposing_it(make_house):
    reg, caller, _ = house(make_house)
    with pytest.raises(BadRequest, match="cached pairing address"):
        await doorbell.chime_get(reg, caller, "tapo-h200", "front-bell")
    assert not caller.sent


async def test_invalid_chime_tone_refused_at_allowed_level(make_house):
    reg, caller = ready_house(make_house, tier=3)
    with pytest.raises(BadRequest, match="does not advertise"):
        await doorbell.chime_set(reg, caller, "hall-chime", "front-bell", "unknown", "normal", 5, True)
    assert "set_chime_alarm_configure" not in caller.methods_sent()


async def test_legacy_ring_request_shapes(make_house):
    reg, caller = ready_house(make_house, tier=3)
    await doorbell.read_setting(reg, caller, "front-bell", "ring", legacy=True)
    assert all(params == {"ring": {}} for _, _, params in caller.sent)
    await doorbell.ring_enable(reg, caller, "front-bell", "doorbell", True, legacy=True)
    assert caller.sent[-1][2] == {"ring": {"config": {"enabled": "on"}}}
    await doorbell.ring_preview(reg, caller, "front-bell", "doorbell", "stop", None, "setRingPlay front-bell", legacy=True)
    assert caller.sent[-1][2] == {"ring": {"status": "off"}}


async def test_event_bounds_dedup_malformed_timestamps_and_parent_auto_selection(make_house):
    reg, caller = ready_house(make_house)
    caller.respond("getLastAlarmInfo", {"last_alarm_time": "invalid", "last_alarm_type": "press"})
    caller.respond("get_trigger_logs", {"logs": [{"id": 1, "timestamp": 101, "event": "ring"}, {"id": 1, "timestamp": 101, "event": "ring"}, {"timestamp": True}, {"timestamp": "invalid"}, {"timestamp": "102"}]})
    out = await doorbell.events_recent(reg, caller, "front-bell", limit=5)
    assert out["events"] == [{"id": 1, "timestamp": 101, "event": "ring", "source": "hub_trigger"}, {"timestamp": 102, "source": "hub_trigger"}]
    assert len(out["events"]) <= 5 and out["cursor"] == 102
    assert caller.sent[-1] == ("front-bell", "get_trigger_logs", {"start_id": 0, "page_size": 5})


@pytest.mark.parametrize("name,params", [
    ("doorbell_ring_sound_set", {"device": "front-bell", "target": "doorbell", "tone": "ding", "volume": True}),
    ("doorbell_ring_sound_set", {"device": "front-bell", "target": "doorbell", "tone": "ding", "volume": "1"}),
    ("doorbell_anti_theft_set", {"device": "front-bell", "enabled": 1, "confirm": "setAntiTheftEnable front-bell"}),
    ("doorbell_anti_theft_set", {"device": "front-bell", "enabled": "true", "confirm": "setAntiTheftEnable front-bell"}),
    ("doorbell_auto_response_set", {"device": "front-bell", "enabled": True, "reply_id": "1", "delay": 5}),
    ("doorbell_ring_plan_set", {"device": "front-bell", "target": "hub", "enabled": True, "schedule": "2500-1700,127"}),
])
async def test_mcp_input_types_and_bounds_do_not_coerce_unsafe_values(make_house, mcp, name, params):
    reg, caller = ready_house(make_house, tier=3)
    async with mcp(reg, caller) as client:
        result = await client.call_tool(name, params)
    assert result.is_error
    assert not caller.sent


async def test_anti_theft_is_level_3_and_needs_its_confirmation(make_house):
    from tolpo.calls import NeedsConfirmation
    reg, caller = ready_house(make_house, tier=2)
    with pytest.raises(Forbidden, match="level 3"):
        await doorbell.anti_theft(reg, caller, "front-bell", True, "setAntiTheftEnable front-bell")
    reg, caller = ready_house(make_house, tier=3)
    for confirm in (None, "setAntiTheftEnable another-bell"):
        with pytest.raises(NeedsConfirmation):
            await doorbell.anti_theft(reg, caller, "front-bell", False, confirm)
    assert "setAntiTheftEnable" not in caller.methods_sent()
    out = await doorbell.anti_theft(reg, caller, "front-bell", False, "setAntiTheftEnable front-bell")
    assert out["changed"] == {"anti_theft": False}
