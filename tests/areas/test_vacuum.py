"""Vacuum protocol tests. No robot, discovery or network is used."""

import base64
import contextlib
import json
from pathlib import Path

import pytest
from kasa import DeviceType, Module
from kasa.exceptions import AuthenticationError, DeviceError

from tolpo.areas import vacuum as v
from tolpo.calls import NeedsConfirmation
from tolpo.policy import Tier
from tolpo.registry import BadRequest, DeviceUnavailable, Entry, Forbidden

from ..fakes import FakeDevice


class FakeVacuum(FakeDevice):
    def __init__(self):
        super().__init__("vac-1", "Floor Robot", "RV30 Max", DeviceType.Vacuum)
        self.modules = {m: object() for m in (Module.Clean, Module.Consumables, Module.Dustbin, Module.Mop, Module.Speaker, Module.CleanRecords, Module.ChildLock)}
        self.updates = 0
        self.update_failure = None

    async def update(self):
        self.updates += 1
        if self.update_failure:
            raise self.update_failure


@pytest.fixture
def house(make_house):
    def make(tier=4, responses=None):
        robot = FakeVacuum()
        reg, caller, _ = make_house(tier=tier, extra=[robot], responses=responses)
        return reg, caller, robot
    return make


class ToolContext:
    def __init__(self, reg, caller):
        self.registry, self.caller, self.functions, self.tiers = reg, caller, {}, {}
        self.translated = contextlib.nullcontext

    async def ready_for(self, refs):
        pass

    def tool(self, *, title, tier=Tier.READ):
        def register(fn):
            self.functions[fn.__name__] = fn
            self.tiers[fn.__name__] = tier
            return fn
        return register


def wrappers(reg, caller):
    ctx = ToolContext(reg, caller)
    v.tools(ctx)
    return ctx


ZONE = {"id": 1, "vertexs": [[0, 0], [100, 0], [100, 200], [0, 200]]}
ZONE_WIRE = {"type": "area", **ZONE, "suction": 2, "cistern": 0, "clean_number": 1, "clean_type": 2, "density": 1, "carpet_strategy": 1}
START = {"clean_on": True, "clean_mode": 0, "clean_order": True, "force_clean": False}
SCHEDULE = {"enable": True, "mode": "repeat", "week_day": 3, "s_min": 480, "alarm_min": 0, "start_remind": False,
            "clean_attr": {"clean_mode": 3, "map_id": 7, "room_list": [1], "suction": 2, "cistern": 0, "clean_number": 1,
                           "clean_type": 2, "density": 1, "clean_order": True}}
# Every write tool, and add/edit paths, with an independently specified wire shape.
WRITES = [
    ("vacuum_start", {}, "setSwitchClean", START, 1),
    ("vacuum_pause", {}, "setRobotPause", {"pause": True}, 1),
    ("vacuum_pause", {"paused": False}, "setRobotPause", {"pause": False}, 1),
    ("vacuum_return_home", {}, "setSwitchCharge", {"switch_charge": True}, 1),
    ("vacuum_return_home", {"returning": False}, "setSwitchCharge", {"switch_charge": False}, 1),
    ("vacuum_suction", {"level": 5}, "setCleanAttr", {"type": "global", "suction": 5}, 2),
    ("vacuum_water_level", {"level": 3}, "setCleanAttr", {"type": "global", "cistern": 3}, 2),
    ("vacuum_passes", {"count": 2}, "setCleanAttr", {"type": "global", "clean_number": 2}, 2),
    ("vacuum_carpet_boost", {"enabled": True}, "setCarpetClean", {"carpet_clean_prefer": "boost"}, 2),
    ("vacuum_carpet_boost", {"enabled": False}, "setCarpetClean", {"carpet_clean_prefer": "normal"}, 2),
    ("vacuum_empty_bin", {}, "setSwitchDustCollection", {"switch_dust_collection": True}, 2),
    ("vacuum_auto_empty", {"enabled": True, "mode": 3}, "setDustCollectionInfo", {"auto_dust_collection": True, "dust_collection_mode": 3}, 2),
    ("vacuum_volume", {"volume": 20}, "setVolume", {"volume": 20}, 2),
    ("vacuum_locate", {}, "playSelectAudio", {"audio_type": "seek_me"}, 3),
    ("vacuum_child_lock", {"enabled": True}, "setChildLockInfo", {"child_lock_status": True}, 3),
    ("vacuum_reset_consumable", {"part": "main_brush"}, "resetConsumablesTime", {"reset_list": ["roll_brush"]}, 4),
    ("vacuum_stop", {}, "setSwitchClean", {"clean_on": False}, 1),
    ("vacuum_spot", {}, "setSwitchClean", {**START, "clean_mode": 2}, 1),
    ("vacuum_clean_rooms", {"map_id": 7, "room_ids": [1, 2]}, "setSwitchClean", {**START, "clean_mode": 3, "map_id": 7, "room_list": [1, 2], "start_type": 1}, 1),
    ("vacuum_zones", {"map_id": 7, "zones": [v.Zone(**ZONE)]}, "setSwitchClean", {**START, "clean_mode": 4, "map_id": 7, "start_type": 1, "area_list": [ZONE_WIRE]}, 1),
    ("vacuum_preset", {"map_id": 7, "preset_id": 12}, "setSwitchClean", {**START, "clean_mode": 5, "map_id": 7, "start_type": 1, "custom_rule_id": 12}, 1),
    ("vacuum_task", {"map_id": 7, "room_ids": [1]}, "runCleanTask", {**START, "clean_mode": 6, "map_id": 7, "room_list": [1], "start_type": 1}, 1),
    ("vacuum_task_group", {"group_id": 5}, "startCleanTaskGroup", {"group_id": 5}, 1),
    ("vacuum_cleaning_type", {"cleaning_type": 0}, "setCleanAttr", {"type": "global", "clean_type": 0}, 2),
    ("vacuum_route_density", {"density": 3}, "setCleanAttr", {"type": "global", "density": 3}, 2),
    ("vacuum_set_schedule", {"week_day": 3, "minute": 480, "map_id": 7, "room_ids": [1]}, "addScheduleRule", SCHEDULE, 2),
    ("vacuum_set_schedule", {"week_day": 3, "minute": 480, "map_id": 7, "room_ids": [1], "rule_id": "s1"}, "editScheduleRule", {**SCHEDULE, "id": "s1"}, 2),
    ("vacuum_remove_schedule", {"rule_id": "s1"}, "removeScheduleRules", {"remove_all": False, "rule_list": [{"id": "s1"}]}, 2),
    ("vacuum_set_do_not_disturb", {"enabled": True, "start_minute": 1320, "end_minute": 420, "behavior": 31}, "setDoNotDisturb", {"do_not_disturb": True, "s_min": 1320, "e_min": 420, "display_notification": 31}, 2),
]

READ_TOOLS = [
    ("vacuum_status", {}, ["getVacStatus", "getBatteryInfo", "getCleanStatus", "getCleanInfo", "getAreaUnit", "getMopState"]),
    ("vacuum_settings", {}, ["getCleanAttr", "getCarpetClean", "getDustCollectionInfo", "getVolume", "getChildLockInfo"]),
    ("vacuum_consumables", {}, ["getConsumablesInfo"]),
    ("vacuum_records", {}, ["getCleanRecords"]),
    ("vacuum_maps", {}, ["getMapInfo"]),
    ("vacuum_current_map", {}, ["getMapInfo", "getMapData"]),
    ("vacuum_rooms", {"map_id": 7}, ["getMapData"]),
    ("vacuum_schedules", {"start_index": 3}, ["getScheduleRules"]),
    ("vacuum_do_not_disturb", {}, ["getDoNotDisturb"]),
]


def test_registry_vacuum_kind(house):
    reg, _, robot = house()
    assert Entry(robot, "robot").kind == "vacuum"
    assert reg.resolve_one("floor-robot").kind == "vacuum"
    assert reg.resolve_one("Floor Robot").kind == "vacuum"


@pytest.mark.anyio
@pytest.mark.parametrize("name,args,method,expected,tier", WRITES)
async def test_every_write_wire_refresh_compact(house, name, args, method, expected, tier):
    reg, caller, robot = house(tier, {method: {"password": "HIDDEN", "token": "HIDDEN", "details": {"ssid": "HIDDEN"}}})
    ctx = wrappers(reg, caller)
    assert ctx.tiers[name] == tier
    if tier >= 3:
        args = {**args, "confirm": f"{method} floor-robot"}
    result = await ctx.functions[name]("floor-robot", **args)
    assert caller.sent[-1] == ("floor-robot", method, expected)
    assert result["id"] == "floor-robot" and result["changed"] == expected
    assert "HIDDEN" not in json.dumps(result)
    assert robot.updates == 1
    if name in ("vacuum_stop", "vacuum_task", "vacuum_set_schedule"):
        assert "UNVERIFIED" in result["note"]


@pytest.mark.anyio
@pytest.mark.parametrize("name,args,method,expected,tier", WRITES)
async def test_every_write_blocked_below_level(house, name, args, method, expected, tier):
    reg, caller, robot = house(tier - 1)
    if tier >= 3:
        args = {**args, "confirm": f"{method} floor-robot"}
    with pytest.raises(Forbidden) as caught:
        await wrappers(reg, caller).functions[name]("floor-robot", **args)
    assert method in str(caught.value)
    assert not any(s in str(caught.value) for s in ("allow_methods", "config", "TOLPO_"))
    assert method not in caller.methods_sent() and robot.updates == 0


@pytest.mark.anyio
@pytest.mark.parametrize("name,args,method,expected,tier", [r for r in WRITES if r[4] >= 3])
async def test_confirmation_names_robot_and_method(house, name, args, method, expected, tier):
    reg, caller, _ = house()
    fn = wrappers(reg, caller).functions[name]
    for phrase in (None, "yes", f"{method} another-robot", "wrongMethod floor-robot"):
        with pytest.raises(NeedsConfirmation):
            await fn("floor-robot", **args, confirm=phrase)
    assert caller.sent == []
    await fn("floor-robot", **args, confirm=f"{method} floor-robot")
    assert caller.methods_sent() == [method]


@pytest.mark.anyio
@pytest.mark.parametrize("name,args,method,expected,tier", WRITES)
async def test_every_write_device_error(house, name, args, method, expected, tier):
    reg, caller, robot = house(responses={method: DeviceError("secret-device-message", error_code=None)})
    if tier >= 3:
        args = {**args, "confirm": f"{method} floor-robot"}
    with pytest.raises(DeviceUnavailable, match=f"floor-robot: {method} failed \\(DeviceError\\)") as caught:
        await wrappers(reg, caller).functions[name]("floor-robot", **args)
    assert "secret-device-message" not in str(caught.value) and robot.updates == 0


@pytest.mark.anyio
@pytest.mark.parametrize("name,args,methods", READ_TOOLS)
async def test_every_read_at_level_zero(house, name, args, methods):
    reg, caller, _ = house(0, {"getMapInfo": {"current_map_id": 7}})
    result = await wrappers(reg, caller).functions[name]("floor-robot", **args)
    assert caller.methods_sent() == methods
    assert result["id"] == "floor-robot"
    assert all(value is not None and value != [] and value != {} for value in result.values())
    if name == "vacuum_settings":
        assert caller.sent[0][2] == {"type": "global"}
    if "getMapData" in methods:
        assert caller.sent[-1][2] == {"map_id": 7, "type": 0}
    if name == "vacuum_schedules":
        assert caller.sent[0][2] == {"start_index": 3}


@pytest.mark.anyio
@pytest.mark.parametrize("method", v._READS)
async def test_each_read_error_is_reported(house, method):
    reg, caller, _ = house(responses={method: AuthenticationError("RAW_SECRET")})
    with pytest.raises(DeviceUnavailable, match=f"floor-robot: {method} failed \\(AuthenticationError\\)"):
        await v.read(caller, reg.resolve_one("floor-robot"), method)


@pytest.mark.anyio
@pytest.mark.parametrize("name,args,methods", READ_TOOLS)
async def test_read_tool_errors_reach_user(house, name, args, methods):
    reg, caller, _ = house(responses={methods[0]: TimeoutError()})
    with pytest.raises(DeviceUnavailable, match="TimeoutError"):
        await wrappers(reg, caller).functions[name]("floor-robot", **args)


@pytest.mark.anyio
async def test_tools_refuse_other_kinds_before_wire(house):
    reg, caller, _ = house()
    ctx = wrappers(reg, caller)
    for name, args, *_ in WRITES + READ_TOOLS:
        with pytest.raises(BadRequest, match="not a robot vacuum"):
            await ctx.functions[name]("desk-lamp", **args)
    assert caller.sent == []


@pytest.mark.anyio
async def test_every_method_is_declared_only_for_vacuums(house):
    reg, caller, _ = house()
    for rule in v.METHODS:
        assert rule.kinds == frozenset({"vacuum"})
        with pytest.raises(Forbidden):
            await caller.call(reg.resolve_one("desk-lamp"), rule.method, confirm=f"{rule.method} desk-lamp")
    assert caller.sent == []


@pytest.mark.anyio
async def test_status_keeps_all_errors_and_sparse_prompts(house):
    reg, caller, _ = house(responses={
        "getVacStatus": {"status": 19, "err_status": [4, 15, 45, 987], "prompt": [23, 30, 31, 33, 988], "token": "HIDDEN"},
        "getBatteryInfo": {"battery_percentage": 70},
        "getCleanStatus": {"clean_status": 7, "is_working": True, "is_mapping": False},
        "getCleanInfo": {"clean_area": 20, "clean_time": 30, "clean_percent": 40},
        "getAreaUnit": {"area_unit": 0}, "getMopState": {"mop_state": False},
    })
    out = await v.status(reg, caller, "floor-robot")
    assert out["state"] == "cutting hair" and out["status"] == 19
    assert [e["meaning"] for e in out["err_status"]] == ["wheel lifted", "tilting", "mop install failed during vacuum-and-mop job", "unknown"]
    assert [e["meaning"] for e in out["prompt"]] == ["robot in a restricted area", "stuck too long", "paused to recharge", "water ran out", "unknown"]
    assert out["battery_percentage"] == 70 and out["clean_percent"] == 40 and out["mop_state"] is False
    assert "HIDDEN" not in json.dumps(out)
    assert set(range(20)) <= set(v.STATUS) and set(range(1, 46)) <= set(v.ERROR) and len(v.PROMPT) == 26


@pytest.mark.anyio
async def test_optional_modules_do_not_trigger_unsupported_reads(house):
    reg, caller, robot = house()
    robot.modules = {Module.Clean: object()}
    await v.status(reg, caller, "floor-robot")
    assert "getMopState" not in caller.methods_sent()
    caller.sent.clear()
    await v.settings(reg, caller, "floor-robot")
    assert caller.methods_sent() == ["getCleanAttr", "getCarpetClean"]


@pytest.mark.anyio
async def test_settings_reads_known_values_only(house):
    reg, caller, _ = house(responses={
        "getCleanAttr": {"suction": 5, "cistern": 0, "clean_number": 3, "clean_type": 2, "density": 3, "ssid": "HIDDEN"},
        "getCarpetClean": {"carpet_clean_prefer": "boost"},
        "getDustCollectionInfo": {"auto_dust_collection": False, "dust_collection_mode": 0, "dust_freq": 2},
        "getVolume": {"volume": 20}, "getChildLockInfo": {"child_lock_status": True},
    })
    out = await v.settings(reg, caller, "floor-robot")
    assert out == {"id": "floor-robot", "suction": 5, "cistern": 0, "clean_number": 3, "clean_type": 2,
                   "density": 3, "carpet_clean_prefer": "boost", "auto_dust_collection": False,
                   "dust_collection_mode": 0, "dust_freq": 2, "volume": 20, "child_lock_status": True}


@pytest.mark.anyio
async def test_auto_empty_preserves_frequency_without_echoing_secrets(house):
    reg, caller, _ = house(responses={"getDustCollectionInfo": {"dust_freq": 2, "token": "HIDDEN", "unknown": "HIDDEN"}})
    out = await v.auto_empty(reg, caller, "floor-robot", False, 1)
    assert caller.sent[-1][2] == {"dust_freq": 2, "auto_dust_collection": False, "dust_collection_mode": 1}
    assert "HIDDEN" not in json.dumps(out)


@pytest.mark.anyio
async def test_consumables_lifetimes_and_all_reset_names(house):
    reg, caller, _ = house(responses={"getConsumablesInfo": {"roll_brush_time": 15, "filter_time": 12001, "rag_time": 23, "ssid": "HIDDEN"}})
    out = await v.consumables(reg, caller, "floor-robot")
    assert out["consumables"] == [
        {"part": "main_brush", "used_minutes": 15, "remaining_minutes": 23985, "assumed_lifetime_hours": 400},
        {"part": "filter", "used_minutes": 12001, "remaining_minutes": 0, "assumed_lifetime_hours": 200},
    ]
    assert out["other_counters"] == {"rag_time": 23}
    for part, expected in (("main_brush", "roll_brush"), ("side_brush", "edge_brush"), ("filter", "filter"), ("sensor", "sensor"), ("charging_contacts", "charge_contact")):
        await v.reset_consumable(reg, caller, "floor-robot", part, "resetConsumablesTime floor-robot")
        assert caller.sent[-1][2] == {"reset_list": [expected]}


@pytest.mark.anyio
async def test_records_last_clean_tuple_bounded_and_flat(house):
    reg, caller, _ = house(responses={"getCleanRecords": {
        "total_time": 300, "total_area": 200, "total_number": 4,
        "record_list": [{"clean_time": 10, "timestamp": 1234, "rule_name": "  Job\n" + "a" * 100, "token": "HIDDEN"}] * 50,
        "lastest_day_record": [1234, 10, 8, False], "password": "HIDDEN",
    }})
    out = await v.records(reg, caller, "floor-robot", 2)
    assert len(out["records"]) == 2 and len(out["records"][0]["rule_name"]) == 64
    assert "\n" not in out["records"][0]["rule_name"] and "HIDDEN" not in json.dumps(out)
    assert out["last_clean"] == {"timestamp": 1234, "clean_time": 10, "clean_area": 8, "dust_collection": False}


@pytest.mark.anyio
async def test_maps_rooms_decoded_no_bitmap_no_secrets(house):
    encoded = base64.b64encode((" Room\n" + "x" * 100).encode()).decode()
    reg, caller, _ = house(responses={
        "getMapInfo": {"current_map_id": 7, "map_num": 1, "map_list": [{"map_id": 7, "map_name": encoded, "is_saved": True, "mac": "HIDDEN"}]},
        "getMapData": {"map_data": "RAW_BITMAP", "map_hash": "HASH", "pix_len": 600, "pix_lz4len": 70, "resolution_unit": "cm", "bit_list": {"clean": 3},
                       "area_list": [{"type": "room", "id": 1, "name": encoded, "suction": 2, "token": "HIDDEN"},
                                     {"type": "forbid", "id": 2, "name": "not-a-room"}], "token": "HIDDEN"},
    })
    maps = await v.maps(reg, caller, "floor-robot")
    assert len(maps["maps"][0]["map_name"]) == 64
    out = await v.map_data(reg, caller, "floor-robot")
    assert out["map_id"] == 7 and out["bitmap_decoded"] is False and out["resolution_unit"] == "cm"
    assert len(out["rooms"]) == 1 and len(out["rooms"][0]["name"]) == 64
    assert not any(word in json.dumps(out) for word in ("RAW_BITMAP", "HIDDEN", "HASH", "bit_list"))
    assert v._name("Plain\nname") == "Plain name" and v._name("%%%") == "%%%"
    assert v._name(base64.b64encode(b"\xff").decode()) is not None
    assert v._name("x" * 1025) is None


@pytest.mark.anyio
async def test_schedule_reads_compact_and_conflict_is_visible(house):
    reg, caller, _ = house(responses={
        "getScheduleRules": {"schedule_rule_max_count": 10, "rule_list": [{**SCHEDULE, "id": "s1\nhello", "token": "HIDDEN"}]},
        "addScheduleRule": {"id": "s2", "conflict_id": "s1", "password": "HIDDEN"},
        "getDoNotDisturb": {"do_not_disturb": True, "s_min": 1320, "e_min": 420, "display_notification": 31, "token": "HIDDEN"},
    })
    out = await v.schedules(reg, caller, "floor-robot")
    assert out["rules"][0]["id"] == "s1 hello" and out["rules"][0]["clean_attr"]["room_list"] == [1]
    saved = await v.set_schedule(reg, caller, "floor-robot", 3, 480, 7, [1])
    assert saved["conflict_id"] == "s1" and "conflict" in saved["note"]
    assert "changed" not in saved and saved["requested"] == SCHEDULE and "UNVERIFIED" in saved["note"]
    assert await v.dnd(reg, caller, "floor-robot") == {"id": "floor-robot", "do_not_disturb": True, "s_min": 1320, "e_min": 420, "display_notification": 31}
    assert "HIDDEN" not in json.dumps([out, saved])


@pytest.mark.anyio
async def test_refresh_failure_reports_successful_write_and_stale_state(house):
    reg, caller, robot = house()
    robot.update_failure = TimeoutError()
    out = await v.clean_start(reg, caller, "floor-robot")
    assert out["changed"] == START and out["refresh_error"] == "floor-robot: state refresh failed (TimeoutError)"


@pytest.mark.anyio
@pytest.mark.parametrize("result", [{"error_code": -1, "message": "HIDDEN"}, None, []])
async def test_invalid_wire_results_do_not_claim_success(house, result):
    reg, caller, robot = house(responses={"setRobotPause": result, "getVacStatus": result})
    with pytest.raises(DeviceUnavailable):
        await v.write(reg, caller, "floor-robot", "setRobotPause", {"pause": True})
    with pytest.raises(DeviceUnavailable):
        await v.status(reg, caller, "floor-robot")
    assert robot.updates == 0


@pytest.mark.anyio
@pytest.mark.parametrize("key,value", [("suction", -1), ("suction", 6), ("cistern", 4), ("clean_number", 0), ("clean_number", 4), ("clean_type", 5), ("density", 0), ("density", 4), ("suction", True), ("suction", 1.5), ("token", 1)])
async def test_attributes_validate_before_wire(house, key, value):
    reg, caller, _ = house()
    with pytest.raises(BadRequest):
        await v.attribute(reg, caller, "floor-robot", key, value)
    assert caller.sent == []


@pytest.mark.anyio
async def test_invalid_targets_settings_schedules_before_wire(house):
    reg, caller, _ = house()
    cases = [
        (v.clean_start, (3, 7, [])), (v.clean_start, (3, 7, [True])), (v.clean_start, (3, 7, [1, 1])),
        (v.clean_start, (3, None, [1])), (v.clean_start, (5, 7, None, None, -1)),
        (v.task, (-1, [1])), (v.task, (7, [1] * 33)),
        (v.auto_empty, (True, 4)), (v.auto_empty, (1, 0)),
        (v.reset_consumable, ("password", "resetConsumablesTime floor-robot")),
        (v.records, (33,)), (v.map_data, (-1,)), (v.schedules, (-1,)),
        (v.set_schedule, (0, 480, 7, [1])), (v.set_schedule, (3, 1440, 7, [1])),
        (v.set_schedule, (3, 480, 7, [])), (v.set_schedule, (3, 480, 7, [1], True, "s1\n")),
        (v.remove_schedule, ("",)), (v.remove_schedule, ("x" * 65,)),
        (v.set_dnd, (True, 100, 100, 15)), (v.set_dnd, (True, 0, 1440, 15)),
        (v.set_dnd, (True, 0, 100, 32)), (v.set_dnd, (1, 0, 100, 1)),
    ]
    for fn, args in cases:
        with pytest.raises(BadRequest):
            await fn(reg, caller, "floor-robot", *args)
    assert caller.sent == []


@pytest.mark.anyio
@pytest.mark.parametrize("zone", [
    {**ZONE, "token": "secret"}, {**ZONE, "id": True}, {**ZONE, "suction": 6}, {**ZONE, "clean_type": 5},
    {**ZONE, "vertexs": [[0, 0]]}, {**ZONE, "vertexs": [[0, 0], [1, 1], [2, 2], [3, 3]]},
    {**ZONE, "vertexs": [[0, 0], [1, 1], [1, 0], [0, 1]]},
    {**ZONE, "vertexs": [[0, 0], [0, 0], [1, 1], [0, 1]]},
    {**ZONE, "vertexs": [[0, 0], [2**40, 0], [2**40, 200], [0, 200]]},
    {**ZONE, "vertexs": [[0, 0], [100, 0], [100, 200], [0, True]]},
])
async def test_zone_validation_before_wire(house, zone):
    reg, caller, _ = house()
    with pytest.raises(BadRequest):
        await v.clean_start(reg, caller, "floor-robot", 4, 7, areas=[zone])
    assert caller.sent == []


@pytest.mark.anyio
async def test_zone_count_and_duplicate_ids(house):
    reg, caller, _ = house()
    for zones in ([], [ZONE] * 17, [ZONE, ZONE]):
        with pytest.raises(BadRequest):
            await v.clean_start(reg, caller, "floor-robot", 4, 7, areas=zones)
    assert caller.sent == []


def test_projection_blocks_nested_hidden_values_and_flattens_text():
    assert v.project({"status": {"token": "secret"}, "volume": float("inf"), "cistern": True, "map_name": "\n", "password": "secret"},
                     ("status", "volume", "cistern"), names=("map_name",)) == {}
    assert v.project({"rule_name": "Job\n" + "x" * 100}, texts=("rule_name",)) == {"rule_name": "Job " + "x" * 60}


@pytest.mark.anyio
async def test_mcp_schema_roundtrip_and_errors(house, mcp):
    reg, caller, _ = house(4, {"getVacStatus": {"status": 8}})
    async with mcp(reg, caller) as client:
        all_tools = {t.name: t for t in (await client.list_tools()).tools}
        for name, tier in wrappers(reg, caller).tiers.items():
            assert all_tools[name].description
            assert all_tools[name].annotations.read_only_hint == (tier == 0)
            if tier > 0:
                assert all_tools[name].annotations.destructive_hint == (tier >= 3)
        out = await client.call_tool("vacuum_status", {"device": "floor-robot"})
        assert out.structured_content["state"] == "sleep"
        denied = await client.call_tool("vacuum_locate", {"device": "floor-robot"})
        assert denied.is_error and "playSelectAudio floor-robot" in denied.content[0].text
        caller.sent.clear()
        for name, args in (("vacuum_suction", {"level": True}), ("vacuum_suction", {"level": "2"}), ("vacuum_suction", {"level": 6}), ("vacuum_volume", {"volume": -1}), ("vacuum_clean_rooms", {"map_id": 7, "room_ids": [True]}), ("vacuum_zones", {"map_id": 7, "zones": [{**ZONE, "token": "hidden"}]})):
            result = await client.call_tool(name, {"device": "floor-robot", **args})
            assert result.is_error
        assert caller.sent == []
        done = await client.call_tool("vacuum_zones", {"device": "floor-robot", "map_id": 7, "zones": [ZONE]})
        assert not done.is_error and caller.sent[-1][2]["area_list"] == [ZONE_WIRE]


def test_every_tool_and_method_is_exercised_and_documented(house):
    reg, caller, _ = house()
    names = set(wrappers(reg, caller).functions)
    assert names == {r[0] for r in WRITES + READ_TOOLS}
    exercised_methods = {r[2] for r in WRITES} | {m for _, _, methods in READ_TOOLS for m in methods}
    assert exercised_methods == {r.method for r in v.METHODS}
    doc = (Path(__file__).parents[2] / "docs/areas/vacuum.md").read_text()
    assert all(f"- `{name}`:" in doc for name in names)
    assert "UNVERIFIED" in doc and "fakes" in doc
