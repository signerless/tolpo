"""Robot vacuum commands over the owner's existing python-kasa session.

Protocol fields come from python-kasa and the app audit. No device has been
available for hardware verification. Maps stay as metadata, never raw rasters.
"""

import base64
import binascii
import math
from typing import Annotated, Any, Literal

from kasa import Module
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import policy
from ..policy import Tier
from ..registry import BadRequest, DeviceUnavailable
from ..views import clean

_READS = (
    "getVacStatus", "getBatteryInfo", "getCleanStatus", "getCleanInfo", "getAreaUnit",
    "getCleanAttr", "getCarpetClean", "getMopState", "getDustCollectionInfo",
    "getVolume", "getChildLockInfo", "getConsumablesInfo", "getCleanRecords",
    "getMapInfo", "getMapData", "getScheduleRules", "getDoNotDisturb",
)
_CONTROLS = {
    "setSwitchClean": "Start or stop a cleaning job.",
    "setRobotPause": "Pause or resume a cleaning job.",
    "setSwitchCharge": "Return to the dock or cancel the return.",
    "runCleanTask": "Run a cleaning task with supplied room settings.",
    "startCleanTaskGroup": "Start a saved cleaning task group.",
}
_SETTINGS = {
    "setCleanAttr": "Change cleaning preferences.",
    "setCarpetClean": "Change carpet boost.",
    "setSwitchDustCollection": "Run the dock's dust collection machinery.",
    "setDustCollectionInfo": "Change automatic dust collection settings.",
    "setVolume": "Change speaker volume.",
    "addScheduleRule": "Add a schedule that can start unattended cleaning.",
    "editScheduleRule": "Change a schedule that can start unattended cleaning.",
    "removeScheduleRules": "Remove selected cleaning schedules.",
    "setDoNotDisturb": "Change the quiet window and its behavior.",
}
METHODS = [policy.method(m, Tier.READ, ["vacuum"], "Read robot vacuum information.") for m in _READS]
METHODS += [policy.method(m, Tier.CONTROL, ["vacuum"], s) for m, s in _CONTROLS.items()]
METHODS += [policy.method(m, Tier.SETTING, ["vacuum"], s) for m, s in _SETTINGS.items()]
METHODS += [
    policy.method("playSelectAudio", Tier.SECURITY, ["vacuum"], "Play a sound through the robot's speaker."),
    policy.method("setChildLockInfo", Tier.SECURITY, ["vacuum"], "Lock or unlock the robot's physical buttons."),
    policy.method("resetConsumablesTime", Tier.LIFECYCLE, ["vacuum"], "Erase a consumable's wear counter after maintenance."),
]

STATUS = dict(enumerate((
    "standby", "cleaning", "mapping", "remote control", "heading home", "charging", "charged", "paused",
    "sleep", "emptying dustbin", "mapping paused", "going to point", "go-to-point paused", "exploring",
    "exploring paused", "washing mop", "drying mop", "installing mop", "removing mop", "cutting hair",
)))
STATUS[100] = "error"  # python-kasa also recognizes this status.
ERROR = dict(enumerate((
    "wheel stuck", "side brush stuck", "main brush stuck", "wheel lifted", "bumper stuck", "trapped",
    "cliff blocked", "battery", "wheel", "side brush motor", "main brush motor", "suction fan", "water pump",
    "dustbin removed", "tilting", "lidar blocked", "lidar stuck", "lidar fault", "cannot locate while cleaning",
    "cannot locate while heading home", "dock not found", "low battery", "lidar cover stuck",
    "no dock and stopped going home", "dustbin needs cleaning", "clean water tank empty or missing",
    "dirty tank full or missing", "mop auto-install failed", "cleaning tray not in dock", "tray overflow",
    "mop detached", "oscillating mop bracket fault", "mop arms fault", "mop attachment not detected",
    "fan motor blocked", "mop attachment stuck", "extended mop stuck", "carpet sensor fault",
    "wipe front laser sensor", "wipe side laser sensor", "dirty-water pump fault", "drying fan fault",
    "drying heater fault", "dust bag missing", "mop install failed during vacuum-and-mop job",
), 1))
ERROR[0] = "ok"
PROMPT = {
    1: "low battery at start", 2: "low battery at start", 3: "mop installed", 4: "mop removed",
    5: "start emptying", 6: "cleaning finished", 7: "low battery going home", 8: "upgrading while cleaning",
    9: "charge done", 10: "dock not found", 11: "dust bag full", 12: "dust bag missing",
    13: "dust channel stuck", 14: "emptying too often", 15: "low battery going home", 16: "update failed",
    17: "update succeeded", 18: "charge start pose", 19: "charge closed", 20: "emptying done",
    21: "target area unreachable", 22: "recreating map", 23: "robot in a restricted area",
    30: "stuck too long", 31: "paused to recharge", 33: "water ran out",
}
CONSUMABLES = {
    "main_brush": ("roll_brush_time", 400), "side_brush": ("edge_brush_time", 200),
    "filter": ("filter_time", 200), "sensor": ("sensor_time", 30),
    "charging_contacts": ("charge_contact_time", 30),
}
ATTRS = ("suction", "suction_level", "cistern", "clean_number", "clean_type", "density")
DeviceRef = Annotated[str, Field(strict=True, description="Robot vacuum id or name")]
Confirm = Annotated[str | None, Field(strict=True, description="Exact phrase: device method followed by robot id")]
MapId = Annotated[int, Field(strict=True, ge=0, le=2**31 - 1, description="Map id returned by vacuum_maps")]
ProtocolId = Annotated[int, Field(strict=True, ge=0, le=2**31 - 1, description="Integer id from this robot")]


def integer(value, lo, hi, label):
    if type(value) is not int or not lo <= value <= hi:
        raise BadRequest(f"{label} must be an integer from {lo} to {hi}")
    return value


def boolean(value, label):
    if type(value) is not bool:
        raise BadRequest(f"{label} must be true or false")
    return value


def ids(values, label):
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise BadRequest(f"{label} must contain 1 to 32 ids")
    out = [integer(v, 0, 2**31 - 1, label) for v in values]
    if len(set(out)) != len(out):
        raise BadRequest(f"{label} must contain distinct ids")
    return out


def _text_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or clean(value, 64) != value:
        raise BadRequest("schedule id must be 1 to 64 plain characters")
    return value


def _name(raw):
    if not isinstance(raw, str) or len(raw) > 1024:
        return None
    try:
        raw = base64.b64decode(raw, validate=True).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeError):
        pass
    return clean(raw) or None


def project(data, numbers=(), flags=(), texts=(), names=()):
    """Explicit field and type allowlists keep device secrets and arbitrary nesting out."""
    out = {}
    if not isinstance(data, dict):
        return out
    for key in numbers:
        value = data.get(key)
        if type(value) in (int, float) and math.isfinite(value):
            out[key] = value
    for key in flags:
        value = data.get(key)
        if type(value) is bool or type(value) is int and value in (0, 1):
            out[key] = bool(value)
    for key in texts:
        value = data.get(key)
        if isinstance(value, str) and (value := clean(value)):
            out[key] = value
    for key in names:
        if value := _name(data.get(key)):
            out[key] = value
    return out


def _rows(data, key, limit=32):
    values = data.get(key)
    return [v for v in values[:limit] if isinstance(v, dict)] if isinstance(values, list) else []


def _codes(values, table):
    return [{"code": n, "meaning": table.get(n, "unknown")} for n in values[:64] if type(n) is int] if isinstance(values, list) else []


def entry_for(registry, device):
    entry = registry.resolve_one(device)
    if entry.kind != "vacuum":
        raise BadRequest(f"{clean(entry.slug)} is not a robot vacuum")
    return entry


async def read(caller, entry, method, params=None):
    try:
        result = await caller.call(entry, method, params)
    except DeviceUnavailable as exc:
        # Device exceptions can carry raw response text. Return only a safe reason.
        reason = type(exc.__cause__ or exc).__name__
        raise DeviceUnavailable(f"{clean(entry.slug)}: {method} failed ({reason})") from None
    if not isinstance(result, dict):
        raise DeviceUnavailable(f"{clean(entry.slug)}: {method} returned invalid data")
    if result.get("error_code", 0) != 0:
        raise DeviceUnavailable(f"{clean(entry.slug)}: {method} reported an error")
    return result


async def write(registry, caller, device, method, params, confirm=None, note=None):
    entry = entry_for(registry, device)
    try:
        result = await caller.call(entry, method, params, confirm=confirm)
    except DeviceUnavailable as exc:
        reason = type(exc.__cause__ or exc).__name__
        raise DeviceUnavailable(f"{clean(entry.slug)}: {method} failed ({reason})") from None
    if not isinstance(result, dict) or result.get("error_code", 0) != 0:
        raise DeviceUnavailable(f"{clean(entry.slug)}: {method} reported an error")
    await registry.refresh(entry, force=True)
    out = {"id": clean(entry.slug), "changed": params}
    if note:
        out["note"] = note
    if not entry.online:
        out["refresh_error"] = f"{clean(entry.slug)}: state refresh failed ({clean(entry.error or 'unreachable')})"
    if method in ("addScheduleRule", "editScheduleRule"):
        # The device's schedule id goes under its own key. It must never replace the robot id, or an agent could
        # address its next command to whatever text the device returned.
        reply = project(result, texts=("id", "conflict_id"))
        if "id" in reply:
            out["rule_id"] = reply["id"]
        if "conflict_id" in reply:
            out["conflict_id"] = reply["conflict_id"]
        if out.get("conflict_id"):
            out["requested"] = out.pop("changed")
            out["note"] = (note + " " if note else "") + "Device reported a schedule conflict. Check schedules before assuming it was saved."
    return out


async def status(registry, caller, device):
    entry = entry_for(registry, device)
    vac = await read(caller, entry, "getVacStatus", {})
    out = {"id": clean(entry.slug), **project(vac, ("status", "water_temp"))}
    if type(vac.get("status")) is int:
        out["state"] = STATUS.get(vac["status"], "unknown")
    for key, table in (("err_status", ERROR), ("prompt", PROMPT)):
        if codes := _codes(vac.get(key), table):
            out[key] = codes
    for method, nums, flags in (
        ("getBatteryInfo", ("battery_percentage",), ()),
        ("getCleanStatus", ("clean_status", "recharge_status"), ("is_working", "is_mapping", "is_relocating", "is_back_wash")),
        ("getCleanInfo", ("clean_area", "clean_time", "clean_percent", "unclean_area"), ()),
        ("getAreaUnit", ("area_unit",), ()),
    ):
        out.update(project(await read(caller, entry, method, {}), nums, flags))
    if Module.Mop in entry.device.modules:
        out.update(project(await read(caller, entry, "getMopState", {}), flags=("mop_state",)))
    return out


async def settings(registry, caller, device):
    entry = entry_for(registry, device)
    out = {"id": clean(entry.slug)}
    out.update(project(await read(caller, entry, "getCleanAttr", {"type": "global"}), ATTRS))
    out.update(project(await read(caller, entry, "getCarpetClean", {}), texts=("carpet_clean_prefer",)))
    for module, method, nums, flags in (
        (Module.Dustbin, "getDustCollectionInfo", ("dust_collection_mode", "dust_freq"), ("auto_dust_collection",)),
        (Module.Speaker, "getVolume", ("volume",), ()),
        (Module.ChildLock, "getChildLockInfo", (), ("child_lock_status",)),
    ):
        if module in entry.device.modules:
            out.update(project(await read(caller, entry, method, {}), nums, flags))
    return out


async def clean_start(registry, caller, device, mode=0, map_id=None, room_ids=None, areas=None, preset_id=None):
    integer(mode, 0, 5, "clean mode")
    params = {"clean_on": True, "clean_mode": mode, "clean_order": True, "force_clean": False}
    if map_id is not None:
        params["map_id"] = integer(map_id, 0, 2**31 - 1, "map id")
    if mode in (3, 4, 5):
        if map_id is None:
            raise BadRequest("targeted cleaning requires a map id")
        params["start_type"] = 1
    if mode == 3:
        params["room_list"] = ids(room_ids, "rooms")
    elif mode == 4:
        if not isinstance(areas, list) or not 1 <= len(areas) <= 16:
            raise BadRequest("zones must contain 1 to 16 rectangles")
        try:
            zones = [Zone.model_validate(a) for a in areas]
        except ValidationError:
            raise BadRequest("zones require a valid id, four rectangle vertices and bounded cleaning settings") from None
        if len({z.id for z in zones}) != len(zones):
            raise BadRequest("zone ids must be distinct")
        params["area_list"] = [z.wire() for z in zones]
    elif mode == 5:
        params["custom_rule_id"] = integer(preset_id, 0, 2**31 - 1, "preset id")
    return await write(registry, caller, device, "setSwitchClean", params)


class Zone(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: int = Field(strict=True, ge=0, le=2**31 - 1, description="Zone id")
    vertexs: list[list[int]] = Field(strict=True, min_length=4, max_length=4, description="Four corners in existing map coordinates, in perimeter order")
    suction: int = Field(strict=True, default=2, ge=0, le=5, description="Protocol suction level")
    cistern: int = Field(strict=True, default=0, ge=0, le=3, description="Protocol water level")
    clean_number: int = Field(strict=True, default=1, ge=1, le=3, description="Cleaning passes")
    clean_type: int = Field(strict=True, default=2, ge=0, le=4, description="0 both, 1 mop, 2 vacuum, 3 vacuum then mop, 4 AI")
    density: int = Field(strict=True, default=1, ge=1, le=3, description="1 daily, 2 fast, 3 meticulous")
    carpet_strategy: int = Field(strict=True, default=1, ge=0, le=2, description="0 skip, 1 vacuum, 2 vacuum and mop")

    def wire(self):
        points = self.vertexs
        if any(len(p) != 2 or any(type(v) is not int or abs(v) > 2**31 - 1 for v in p) for p in points):
            raise BadRequest("zone vertices must be integer coordinate pairs")
        xs, ys = {p[0] for p in points}, {p[1] for p in points}
        if len(xs) != 2 or len(ys) != 2 or len({tuple(p) for p in points}) != 4:
            raise BadRequest("zone must be a nonempty rectangle")
        if any(a[0] != b[0] and a[1] != b[1] for a, b in zip(points, points[1:] + points[:1])):
            raise BadRequest("zone corners must follow the rectangle perimeter")
        return {"type": "area", **self.model_dump()}


async def attribute(registry, caller, device, key, value):
    bounds = {"suction": (0, 5), "cistern": (0, 3), "clean_number": (1, 3), "clean_type": (0, 4), "density": (1, 3)}
    if key not in bounds:
        raise BadRequest("unknown cleaning preference")
    integer(value, *bounds[key], key)
    return await write(registry, caller, device, "setCleanAttr", {"type": "global", key: value})


async def auto_empty(registry, caller, device, enabled, mode):
    entry = entry_for(registry, device)
    boolean(enabled, "enabled")
    integer(mode, 0, 3, "auto empty mode")
    # Preserve the known frequency field, never echo arbitrary device fields into a write.
    old = project(await read(caller, entry, "getDustCollectionInfo", {}), ("dust_freq",))
    return await write(registry, caller, device, "setDustCollectionInfo", {
        **old, "auto_dust_collection": enabled, "dust_collection_mode": mode,
    })


async def consumables(registry, caller, device):
    entry = entry_for(registry, device)
    data = await read(caller, entry, "getConsumablesInfo", {})
    rows = []
    for name, (key, hours) in CONSUMABLES.items():
        if type(used := data.get(key)) in (int, float) and math.isfinite(used):
            rows.append({"part": name, "used_minutes": used, "remaining_minutes": max(0, hours * 60 - used), "assumed_lifetime_hours": hours})
    out = {"id": clean(entry.slug)}
    if rows:
        out["consumables"] = rows
    extra = project(data, ("main_brush_lid_time", "rag_time", "clean_tray"))
    if extra:
        out["other_counters"] = extra
    return out


async def reset_consumable(registry, caller, device, part, confirm):
    if part not in CONSUMABLES:
        raise BadRequest("choose main_brush, side_brush, filter, sensor or charging_contacts")
    key = CONSUMABLES[part][0].removesuffix("_time")
    return await write(registry, caller, device, "resetConsumablesTime", {"reset_list": [key]}, confirm)


async def records(registry, caller, device, limit=10):
    integer(limit, 1, 32, "record limit")
    entry = entry_for(registry, device)
    data = await read(caller, entry, "getCleanRecords", {})
    nums = ("clean_area", "clean_time", "clean_type", "start_type", "task_type", "info_num", "wash_times", "record_index", "timestamp", "error", "message", "map_id")
    out = {"id": clean(entry.slug), **project(data, ("total_time", "total_area", "total_number"))}
    rows = [project(r, nums, ("dust_collection", "is_ai", "is_custom"), ("rule_name",)) for r in _rows(data, "record_list", limit)]
    if rows := [r for r in rows if r]:
        out["records"] = rows
    last = data.get("lastest_day_record")
    if isinstance(last, list) and len(last) == 4:
        last = dict(zip(("timestamp", "clean_time", "clean_area", "dust_collection"), last))
    if last := project(last, nums, ("dust_collection",)):
        out["last_clean"] = last
    return out


async def maps(registry, caller, device):
    entry = entry_for(registry, device)
    data = await read(caller, entry, "getMapInfo", {})
    out = {"id": clean(entry.slug), **project(data, ("current_map_id", "map_num"), ("is_multi_floor", "auto_change_map"))}
    rows = [project(m, ("map_id", "update_time"), ("map_locked", "is_saved", "is_base_exist", "is_aligned", "auto_area_flag"), names=("map_name",)) for m in _rows(data, "map_list")]
    if rows := [r for r in rows if r]:
        out["maps"] = rows
    return out


async def map_data(registry, caller, device, map_id=None, rooms_only=False):
    entry = entry_for(registry, device)
    if map_id is None:
        info = await read(caller, entry, "getMapInfo", {})
        map_id = info.get("current_map_id")
    integer(map_id, 0, 2**31 - 1, "map id")
    data = await read(caller, entry, "getMapData", {"map_id": map_id, "type": 0})
    out = {"id": clean(entry.slug), "map_id": map_id}
    rooms = [project(a, ("id", "color", *ATTRS), names=("name",)) for a in _rows(data, "area_list") if a.get("type") == "room"]
    if rooms := [r for r in rooms if r]:
        out["rooms"] = rooms
    if not rooms_only:
        out.update(project(data, ("pix_len", "pix_lz4len", "resolution_unit"), ("is_update", "map_locked")))
        if isinstance(data.get("resolution_unit"), str):
            out.update(project(data, texts=("resolution_unit",)))
        out["bitmap_decoded"] = False
        out["note"] = "Map bitmap is not decoded. Raster encoding is not verified."
    return out


async def task(registry, caller, device, map_id, room_ids):
    # UNVERIFIED: mode 6 with runCleanTask has not been checked on hardware.
    params = {"clean_on": True, "clean_mode": 6, "clean_order": True, "force_clean": False,
              "start_type": 1, "map_id": integer(map_id, 0, 2**31 - 1, "map id"), "room_list": ids(room_ids, "rooms")}
    return await write(registry, caller, device, "runCleanTask", params, note="UNVERIFIED: task mode 6 needs device verification.")


async def schedules(registry, caller, device, start_index=0):
    integer(start_index, 0, 2**31 - 1, "start index")
    entry = entry_for(registry, device)
    data = await read(caller, entry, "getScheduleRules", {"start_index": start_index})
    out = {"id": clean(entry.slug), **project(data, ("schedule_rule_max_count",))}
    rows = []
    for rule in _rows(data, "rule_list"):
        row = project(rule, ("week_day", "s_min", "alarm_min", "invalid", "year", "month", "day"), ("enable", "start_remind", "cancel"), ("id", "mode"))
        attr = project(rule.get("clean_attr"), (*ATTRS, "clean_mode", "map_id", "custom_rule_id", "group_id"), ("clean_order", "is_custom"))
        raw_attr = rule.get("clean_attr")
        if isinstance(raw_attr, dict) and isinstance(values := raw_attr.get("room_list"), list):
            values = [v for v in values[:32] if type(v) is int]
            if values:
                attr["room_list"] = values
        if attr:
            row["clean_attr"] = attr
        if row:
            rows.append(row)
    if rows:
        out["rules"] = rows
    out["note"] = "week_day is the raw protocol value. Day encoding is not verified."
    return out


async def set_schedule(registry, caller, device, week_day, minute, map_id, room_ids, enabled=True, rule_id=None):
    # UNVERIFIED: weekday bit mapping and device-local s_min behavior need a device trace.
    params = {"enable": boolean(enabled, "enabled"), "mode": "repeat",
              "week_day": integer(week_day, 1, 127, "raw weekday value"),
              "s_min": integer(minute, 0, 1439, "start minute"), "alarm_min": 0, "start_remind": False,
              "clean_attr": {"clean_mode": 3, "map_id": integer(map_id, 0, 2**31 - 1, "map id"),
                             "room_list": ids(room_ids, "rooms"), "suction": 2, "cistern": 0,
                             "clean_number": 1, "clean_type": 2, "density": 1, "clean_order": True}}
    method = "addScheduleRule"
    if rule_id is not None:
        params["id"] = _text_id(rule_id)
        method = "editScheduleRule"
    return await write(registry, caller, device, method, params, note="UNVERIFIED: schedule day and local time encoding. Schedules can start unattended motion.")


async def remove_schedule(registry, caller, device, rule_id):
    return await write(registry, caller, device, "removeScheduleRules", {"remove_all": False, "rule_list": [{"id": _text_id(rule_id)}]})


async def dnd(registry, caller, device):
    entry = entry_for(registry, device)
    return {"id": clean(entry.slug), **project(await read(caller, entry, "getDoNotDisturb", {}), ("s_min", "e_min", "display_notification"), ("do_not_disturb",))}


async def set_dnd(registry, caller, device, enabled, start_minute, end_minute, behavior):
    params = {"do_not_disturb": boolean(enabled, "enabled"), "s_min": integer(start_minute, 0, 1439, "start minute"),
              "e_min": integer(end_minute, 0, 1439, "end minute"), "display_notification": integer(behavior, 0, 31, "quiet behavior bit field")}
    if params["s_min"] == params["e_min"]:
        raise BadRequest("quiet window start and end must differ")
    return await write(registry, caller, device, "setDoNotDisturb", params)


async def _invoke(ctx, device, function, *args, **kwargs):
    await ctx.ready_for([device])
    with ctx.translated():
        return await function(ctx.registry, ctx.caller, device, *args, **kwargs)


def tools(ctx):
    @ctx.tool(title="Robot vacuum status")
    async def vacuum_status(device: DeviceRef) -> dict[str, Any]:
        """Read status, every reported error and prompt, battery and cleaning progress."""
        return await _invoke(ctx, device, status)

    @ctx.tool(title="Robot vacuum settings")
    async def vacuum_settings(device: DeviceRef) -> dict[str, Any]:
        """Read cleaning preferences and available dock, speaker and child lock settings."""
        return await _invoke(ctx, device, settings)

    @ctx.tool(title="Start whole home cleaning", tier=Tier.CONTROL)
    async def vacuum_start(device: DeviceRef) -> dict[str, Any]:
        """Start a new whole-home cleaning job. Use vacuum_pause with paused=false to resume."""
        return await _invoke(ctx, device, clean_start)

    @ctx.tool(title="Pause or resume cleaning", tier=Tier.CONTROL)
    async def vacuum_pause(device: DeviceRef, paused: Annotated[bool, Field(strict=True, description="True pauses; false resumes")] = True) -> dict[str, Any]:
        """Pause or resume the current job."""
        return await _invoke(ctx, device, write, "setRobotPause", {"pause": boolean(paused, "paused")})

    @ctx.tool(title="Return to dock", tier=Tier.CONTROL)
    async def vacuum_return_home(device: DeviceRef, returning: Annotated[bool, Field(strict=True, description="True returns to dock; false cancels return")] = True) -> dict[str, Any]:
        """Send the robot to its charging dock or cancel its return."""
        return await _invoke(ctx, device, write, "setSwitchCharge", {"switch_charge": boolean(returning, "returning")})

    @ctx.tool(title="Vacuum suction", tier=Tier.SETTING)
    async def vacuum_suction(device: DeviceRef, level: Annotated[int, Field(strict=True, ge=0, le=5, description="0 off, 1 quiet, 2 standard, 3 turbo, 4 max, 5 ultra")]) -> dict[str, Any]:
        """Change global suction. Ultra is reset by the robot after the job."""
        return await _invoke(ctx, device, attribute, "suction", level)

    @ctx.tool(title="Mop water level", tier=Tier.SETTING)
    async def vacuum_water_level(device: DeviceRef, level: Annotated[int, Field(strict=True, ge=0, le=3, description="0 off, 1 low, 2 medium, 3 high")]) -> dict[str, Any]:
        """Change the global mop water level."""
        return await _invoke(ctx, device, attribute, "cistern", level)

    @ctx.tool(title="Cleaning passes", tier=Tier.SETTING)
    async def vacuum_passes(device: DeviceRef, count: Annotated[int, Field(strict=True, ge=1, le=3, description="Number of passes, 1 to 3")]) -> dict[str, Any]:
        """Change the global number of cleaning passes."""
        return await _invoke(ctx, device, attribute, "clean_number", count)

    @ctx.tool(title="Carpet boost", tier=Tier.SETTING)
    async def vacuum_carpet_boost(device: DeviceRef, enabled: Annotated[bool, Field(strict=True, description="Boost suction on carpet")]) -> dict[str, Any]:
        """Enable or disable carpet suction boost."""
        return await _invoke(ctx, device, write, "setCarpetClean", {"carpet_clean_prefer": "boost" if boolean(enabled, "enabled") else "normal"})

    @ctx.tool(title="Empty robot dustbin", tier=Tier.SETTING)
    async def vacuum_empty_bin(device: DeviceRef) -> dict[str, Any]:
        """Start dust collection machinery at a compatible dock."""
        return await _invoke(ctx, device, write, "setSwitchDustCollection", {"switch_dust_collection": True})

    @ctx.tool(title="Automatic dust collection", tier=Tier.SETTING)
    async def vacuum_auto_empty(device: DeviceRef, enabled: Annotated[bool, Field(strict=True, description="Enable automatic dust collection")], mode: Annotated[int, Field(strict=True, ge=0, le=3, description="0 smart, 1 light, 2 balanced, 3 max")] = 0) -> dict[str, Any]:
        """Change automatic dust collection and its mode, preserving reported frequency."""
        return await _invoke(ctx, device, auto_empty, enabled, mode)

    @ctx.tool(title="Robot speaker volume", tier=Tier.SETTING)
    async def vacuum_volume(device: DeviceRef, volume: Annotated[int, Field(strict=True, ge=0, le=100, description="Volume from 0 to 100")]) -> dict[str, Any]:
        """Change speaker volume."""
        integer(volume, 0, 100, "volume")
        return await _invoke(ctx, device, write, "setVolume", {"volume": volume})

    @ctx.tool(title="Locate robot by sound", tier=Tier.SECURITY)
    async def vacuum_locate(device: DeviceRef, confirm: Confirm = None) -> dict[str, Any]:
        """Play the seek-me sound. Requires confirm='playSelectAudio <robot id>'."""
        return await _invoke(ctx, device, write, "playSelectAudio", {"audio_type": "seek_me"}, confirm)

    @ctx.tool(title="Robot physical child lock", tier=Tier.SECURITY)
    async def vacuum_child_lock(device: DeviceRef, enabled: Annotated[bool, Field(strict=True, description="Lock the physical buttons")], confirm: Confirm = None) -> dict[str, Any]:
        """Lock or unlock physical buttons. Requires confirm='setChildLockInfo <robot id>'."""
        return await _invoke(ctx, device, write, "setChildLockInfo", {"child_lock_status": boolean(enabled, "enabled")}, confirm)

    @ctx.tool(title="Robot consumable counters")
    async def vacuum_consumables(device: DeviceRef) -> dict[str, Any]:
        """Read wear counters in minutes. Remaining times use python-kasa's assumed lifetimes."""
        return await _invoke(ctx, device, consumables)

    @ctx.tool(title="Reset consumable counter", tier=Tier.LIFECYCLE)
    async def vacuum_reset_consumable(device: DeviceRef, part: Annotated[Literal["main_brush", "side_brush", "filter", "sensor", "charging_contacts"], Field(description="Replaced or cleaned part")], confirm: Confirm = None) -> dict[str, Any]:
        """Erase one wear counter after maintenance. Requires confirm='resetConsumablesTime <robot id>'."""
        return await _invoke(ctx, device, reset_consumable, part, confirm)

    @ctx.tool(title="Robot cleaning records")
    async def vacuum_records(device: DeviceRef, limit: Annotated[int, Field(strict=True, ge=1, le=32, description="Maximum records to return")] = 10) -> dict[str, Any]:
        """Read compact cleaning history and totals. Timestamps can reveal presence patterns."""
        return await _invoke(ctx, device, records, limit)

    @ctx.tool(title="Stop robot job", tier=Tier.CONTROL)
    async def vacuum_stop(device: DeviceRef) -> dict[str, Any]:
        """UNVERIFIED: try ending the job with clean_on=false. Needs a stop-button trace."""
        # UNVERIFIED: the audit inferred this parameter from its name.
        return await _invoke(ctx, device, write, "setSwitchClean", {"clean_on": False}, note="UNVERIFIED: clean_on=false as stop needs device verification.")

    @ctx.tool(title="Spot cleaning", tier=Tier.CONTROL)
    async def vacuum_spot(device: DeviceRef) -> dict[str, Any]:
        """Start spot cleaning around the robot's current position."""
        return await _invoke(ctx, device, clean_start, 2)

    @ctx.tool(title="Clean selected rooms", tier=Tier.CONTROL)
    async def vacuum_clean_rooms(device: DeviceRef, map_id: MapId, room_ids: Annotated[list[ProtocolId], Field(strict=True, min_length=1, max_length=32, description="Room ids from vacuum_rooms")]) -> dict[str, Any]:
        """Start cleaning selected rooms on an explicit map with global settings."""
        return await _invoke(ctx, device, clean_start, 3, map_id, room_ids)

    @ctx.tool(title="Clean map rectangles", tier=Tier.CONTROL)
    async def vacuum_zones(device: DeviceRef, map_id: MapId, zones: Annotated[list[Zone], Field(strict=True, min_length=1, max_length=16, description="Rectangles in existing map coordinates. Coordinates and units must be known.")]) -> dict[str, Any]:
        """Clean rectangular zones with per-zone settings. This does not edit the map."""
        return await _invoke(ctx, device, clean_start, 4, map_id, areas=[z.model_dump() for z in zones])

    @ctx.tool(title="Start saved cleaning preset", tier=Tier.CONTROL)
    async def vacuum_preset(device: DeviceRef, map_id: MapId, preset_id: Annotated[int, Field(strict=True, ge=0, description="Existing customization rule id")]) -> dict[str, Any]:
        """Start an existing custom preset on its map."""
        return await _invoke(ctx, device, clean_start, 5, map_id, preset_id=preset_id)

    @ctx.tool(title="Run cleaning task", tier=Tier.CONTROL)
    async def vacuum_task(device: DeviceRef, map_id: MapId, room_ids: Annotated[list[ProtocolId], Field(strict=True, min_length=1, max_length=32, description="Room ids on the explicit map")]) -> dict[str, Any]:
        """UNVERIFIED: run a room task with runCleanTask and mode 6. Uses global cleaning settings."""
        return await _invoke(ctx, device, task, map_id, room_ids)

    @ctx.tool(title="Start saved task group", tier=Tier.CONTROL)
    async def vacuum_task_group(device: DeviceRef, group_id: Annotated[int, Field(strict=True, ge=0, description="Existing saved task group id")]) -> dict[str, Any]:
        """Start a saved task group, the app's task-group cleaning mode 7."""
        integer(group_id, 0, 2**31 - 1, "group id")
        return await _invoke(ctx, device, write, "startCleanTaskGroup", {"group_id": group_id})

    @ctx.tool(title="Cleaning type", tier=Tier.SETTING)
    async def vacuum_cleaning_type(device: DeviceRef, cleaning_type: Annotated[int, Field(strict=True, ge=0, le=4, description="0 vacuum and mop, 1 mop, 2 vacuum, 3 vacuum then mop, 4 AI")]) -> dict[str, Any]:
        """Change the global cleaning type."""
        return await _invoke(ctx, device, attribute, "clean_type", cleaning_type)

    @ctx.tool(title="Cleaning route density", tier=Tier.SETTING)
    async def vacuum_route_density(device: DeviceRef, density: Annotated[int, Field(strict=True, ge=1, le=3, description="1 daily, 2 fast, 3 meticulous")]) -> dict[str, Any]:
        """Change the global route spacing."""
        return await _invoke(ctx, device, attribute, "density", density)

    @ctx.tool(title="Robot map list")
    async def vacuum_maps(device: DeviceRef) -> dict[str, Any]:
        """Read saved map metadata and the current map id. Names are decoded and flattened."""
        return await _invoke(ctx, device, maps)

    @ctx.tool(title="Current robot map metadata")
    async def vacuum_current_map(device: DeviceRef) -> dict[str, Any]:
        """Read the current map's metadata and room names. The bitmap is not decoded or returned."""
        return await _invoke(ctx, device, map_data)

    @ctx.tool(title="Robot rooms")
    async def vacuum_rooms(device: DeviceRef, map_id: Annotated[int | None, Field(strict=True, ge=0, description="Map id, or omit for current map")] = None) -> dict[str, Any]:
        """Read room ids, names and cleaning preferences on a map."""
        return await _invoke(ctx, device, map_data, map_id, rooms_only=True)

    @ctx.tool(title="Robot schedules")
    async def vacuum_schedules(device: DeviceRef, start_index: Annotated[int, Field(strict=True, ge=0, description="Device pagination start index")] = 0) -> dict[str, Any]:
        """Read up to 32 schedules from one page. week_day stays a raw protocol value."""
        return await _invoke(ctx, device, schedules, start_index)

    @ctx.tool(title="Set robot schedule", tier=Tier.SETTING)
    async def vacuum_set_schedule(device: DeviceRef, week_day: Annotated[int, Field(strict=True, ge=1, le=127, description="Raw weekday value verified independently on the robot. Day mapping is UNVERIFIED.")], minute: Annotated[int, Field(strict=True, ge=0, le=1439, description="Start minute in device local time, UNVERIFIED")], map_id: MapId, room_ids: Annotated[list[ProtocolId], Field(strict=True, min_length=1, max_length=32, description="Rooms to clean")], enabled: Annotated[bool, Field(strict=True, description="Enable unattended scheduled motion")] = True, rule_id: Annotated[str | None, Field(strict=True, max_length=64, description="Existing id to edit; omit to add")] = None) -> dict[str, Any]:
        """UNVERIFIED: add or edit a repeating room schedule. It can start unattended motion. Uses vacuum-only defaults."""
        return await _invoke(ctx, device, set_schedule, week_day, minute, map_id, room_ids, enabled, rule_id)

    @ctx.tool(title="Remove one robot schedule", tier=Tier.SETTING)
    async def vacuum_remove_schedule(device: DeviceRef, rule_id: Annotated[str, Field(strict=True, min_length=1, max_length=64, description="One existing schedule id")]) -> dict[str, Any]:
        """Remove one schedule. Does not remove all schedules."""
        return await _invoke(ctx, device, remove_schedule, rule_id)

    @ctx.tool(title="Robot quiet window")
    async def vacuum_do_not_disturb(device: DeviceRef) -> dict[str, Any]:
        """Read the quiet window and its behavior bit field."""
        return await _invoke(ctx, device, dnd)

    @ctx.tool(title="Set robot quiet window", tier=Tier.SETTING)
    async def vacuum_set_do_not_disturb(device: DeviceRef, enabled: Annotated[bool, Field(strict=True, description="Enable the quiet window")], start_minute: Annotated[int, Field(strict=True, ge=0, le=1439, description="Start minute of device local day")], end_minute: Annotated[int, Field(strict=True, ge=0, le=1439, description="End minute, may be next day")], behavior: Annotated[int, Field(strict=True, ge=0, le=31, description="Bit sum: 1 no resume, 2 voice off, 4 dim LEDs, 8 no auto empty, 16 no auto dry")] = 15) -> dict[str, Any]:
        """Change the quiet window. During it schedules do not start. Overnight windows are allowed."""
        return await _invoke(ctx, device, set_dnd, enabled, start_minute, end_minute, behavior)
