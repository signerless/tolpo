"""Local settings and controls for bulbs, strips, and individual light children."""

from datetime import date
import re
from typing import Annotated, Any, Literal

from kasa import Module
from kasa.exceptions import DeviceError, SmartErrorCode
from pydantic import Field, StrictBool, StrictInt

from .. import policy
from ..policy import Tier
from ..registry import BadRequest, DeviceUnavailable, NotFound, Unsupported
from ..views import clean

METHODS = [policy.method("get_next_event", Tier.READ, ["light"], "Read the next lighting event.")]
# get_device_info and set_device_info belong to common.py.
for _family in ("schedule", "countdown", "antitheft"):
    METHODS.append(policy.method(f"get_{_family}_rules", Tier.READ, ["light"], f"Read lighting {_family} rules."))
    for _operation in ("add", "edit"):
        METHODS.append(policy.method(f"{_operation}_{_family}_rule", Tier.SETTING, ["light"],
                                     f"Change a lighting {_family} rule."))
    # UNVERIFIED: remove_antitheft_rules is inferred from a misspelled app method string.
    # UNVERIFIED: countdown/antitheft removal parameter shape follows the decoded schedule removal bean.
    METHODS.append(policy.method(f"remove_{_family}_rules", Tier.SETTING, ["light"],
                                 f"Delete lighting {_family} rules."))
# UNVERIFIED: get_auto_light_info and set_auto_light_info come from the app's AutoLightBean. No real device has
# answered them yet: the L430C reports the auto_light component but returns an unknown-method error.
METHODS.extend([
    policy.method("get_auto_light_info", Tier.READ, ["light"], "Read the Auto-White mode."),
    policy.method("set_auto_light_info", Tier.SETTING, ["light"], "Change automatic white lighting."),
    policy.method("get_preset_rules", Tier.READ, ["light"], "Read saved lighting presets."),
    policy.method("edit_preset_rules", Tier.SETTING, ["light"], "Edit a saved color or white preset."),
    policy.method("set_preset_rules", Tier.SETTING, ["light"], "Edit the saved brightness presets."),
    policy.method("get_segment_effect_rule", Tier.READ, ["light"], "Read decoded segment effect metadata."),
])

DeviceRef = Annotated[str, Field(description="Light id or name. Use a child id for a ceiling light's downlight or ring.")]
Object = Annotated[dict[str, Any], Field(description="Validated lighting object. See the lights area documentation for fields.")]
RuleKind = Annotated[Literal["schedule", "countdown", "antitheft"], Field(description="On-device schedule, countdown timer, or Away Mode.")]
RuleId = Annotated[str, Field(description="Existing rule id returned by light_list_rules.")]
Enabled = Annotated[bool, Field(strict=True, description="True to enable. False to disable.")]

STATE_FIELDS = {"on", "brightness", "color_temp", "hue", "saturation", "auto", "auto_mode", "transition_period"}
DEFAULT_FIELDS = {"type", "state", "brightness", "on_off", "re_power_type", "re_power_type_capability"}
OUTPUT_FIELDS = STATE_FIELDS | DEFAULT_FIELDS | {
    "value", "enable", "mode", "id", "name", "rule_list", "ruleList", "rules", "next_event",
    "s_min", "e_min", "s_type", "e_type", "time_offset", "week_day", "year", "month", "day",
    "s_action", "e_action", "desired_states", "delay", "remain", "action", "frequency",
    "start_min", "end_min", "start_time_type", "end_time_type", "start_time_offset", "end_time_offset",
    "start_index", "startIndex", "sum", "max_count", "maxCount", "current_rule_id", "currentRuleId",
    "schedule_rule_max_count", "countdown_rule_max_count", "antitheft_rule_max_count",
    "s_time", "e_time", "states", "custom", "segments", "speed", "carousel", "deviceType", "bAdjusted",
}
AUTO_MODES = {"circadian", "light_compensate", "light_track"}
DEFAULT_TYPES = {"custom", "last_states", "always_on", "smart_status", "toggle_states"}
REPOWER_TYPES = {"always_on", "always_off", "last_states", "toggle_states"}


def compact(value):
    """Project onto known lighting fields. Never pass raw device dictionaries to a model."""
    if isinstance(value, dict):
        out = {k: compact(v) for k, v in value.items() if k in OUTPUT_FIELDS}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, (list, tuple)):
        return [compact(v) for v in value[:128]]
    if isinstance(value, str):
        return clean(value, limit=128)
    return value if type(value) in (int, float, bool) else None


def integer(value, name, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        raise BadRequest(f"{name} must be an integer from {lo} to {hi}")
    return value


def boolean(value, name):
    if type(value) is not bool:
        raise BadRequest(f"{name} takes true or false")
    return value


def choice(value, name, choices):
    if not isinstance(value, str) or value not in choices:
        raise BadRequest(f"{name} has an unsupported value")
    return value


def fields(value, allowed, name):
    if not isinstance(value, dict) or not value or set(value) - allowed:
        raise BadRequest(f"{name} must be a nonempty object with supported fields")
    return dict(value)


def identifier(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        raise BadRequest("rule id must contain 1 to 128 letters, digits, underscores or hyphens")
    return value


def state_object(value):
    out = fields(value, STATE_FIELDS, "light state")
    for key, lo, hi in (("brightness", 1, 100), ("color_temp", 0, 9000), ("hue", 0, 360),
                        ("saturation", 0, 100), ("transition_period", 0, 60000)):
        if key in out:
            integer(out[key], key, lo, hi)
    for key in ("on", "auto"):
        if key in out:
            boolean(out[key], key)
    if "auto_mode" in out:
        choice(out["auto_mode"], "auto_mode", AUTO_MODES)
    if ("hue" in out) != ("saturation" in out):
        raise BadRequest("hue and saturation must be supplied together")
    # A white state can also carry remembered hue/saturation, as on the L430C fixture.
    if 0 < out.get("color_temp", 0) < 1500:
        raise BadRequest("color_temp must be zero or from 1500 to 9000 kelvin")
    return out


def light_entry(registry, ref):
    entry = registry.resolve_one(ref)
    if entry.kind != "light":
        raise BadRequest(f"{clean(entry.slug)} is not a light")
    return entry


def missing_method(exc):
    """True when the device answered that it does not know the method, as the L430C does for get_auto_light_info."""
    cause = exc.__cause__
    return isinstance(cause, DeviceError) and cause.error_code is SmartErrorCode.UNKNOWN_METHOD_ERROR


async def ask(caller, entry, method, params=None):
    """Send one method. A model that lacks the method gets a clean refusal, not the library's error text."""
    try:
        return await caller.call(entry, method, params)
    except DeviceUnavailable as exc:
        if missing_method(exc):
            raise Unsupported(f"{clean(entry.slug)} does not support {method}") from None
        raise


async def refreshed(registry, entry):
    await registry.refresh(entry, force=True)
    if not entry.online:
        raise DeviceUnavailable(f"{clean(entry.slug)} accepted the change but its state could not be refreshed")


async def write(registry, caller, entry, method, params):
    result = await ask(caller, entry, method, params)
    if result is False or (isinstance(result, dict) and result.get("error_code", 0) != 0):
        raise DeviceUnavailable(f"{clean(entry.slug)} refused {method}")
    await refreshed(registry, entry)
    return {"device": clean(entry.slug), "changed": compact(params)}


async def defaults_read(registry, caller, ref):
    entry = light_entry(registry, ref)
    info = await ask(caller, entry, "get_device_info")
    raw = info.get("default_states") if isinstance(info, dict) else None
    if not isinstance(raw, dict):
        raise Unsupported(f"{clean(entry.slug)} did not report power-on defaults")
    return entry, raw


async def get_defaults(registry, caller, ref):
    entry, raw = await defaults_read(registry, caller, ref)
    return {"device": clean(entry.slug), "default_states": compact({k: v for k, v in raw.items() if k in DEFAULT_FIELDS})}


async def set_defaults(registry, caller, ref, settings):
    patch = fields(settings, DEFAULT_FIELDS - {"re_power_type_capability"}, "default_states")
    if "brightness" in patch and ("type" in patch or "state" in patch):
        raise BadRequest("use either a brightness default or type and state")
    if "type" in patch:
        choice(patch["type"], "type", DEFAULT_TYPES)
    if "re_power_type" in patch:
        choice(patch["re_power_type"], "re_power_type", REPOWER_TYPES)
    if "on_off" in patch:
        boolean(patch["on_off"], "on_off")
    if "state" in patch:
        patch["state"] = state_object(patch["state"])
    if "brightness" in patch:
        bright = fields(patch["brightness"], {"type", "value"}, "brightness default")
        choice(bright.get("type"), "brightness type", {"custom", "last_states"})
        if bright["type"] == "custom":
            integer(bright.get("value"), "brightness value", 1, 100)
        elif "value" in bright:
            raise BadRequest("last_states brightness has no value")
    entry, raw = await defaults_read(registry, caller, ref)
    if "re_power_type" in patch:
        if "re_power_type" not in raw:
            raise Unsupported(f"{clean(entry.slug)} did not report power-outage behavior")
        capabilities = raw.get("re_power_type_capability")
        if capabilities is not None and patch["re_power_type"] not in capabilities:
            raise BadRequest("this light does not support that power-outage behavior")
    current = {k: v for k, v in raw.items() if k in DEFAULT_FIELDS - {"re_power_type_capability"}}
    # Preserve the other half of the default object when changing only outage behavior.
    current.update(patch)
    if "brightness" in patch:
        current.pop("type", None)
        current.pop("state", None)
    elif "type" in patch or "state" in patch:
        current.pop("brightness", None)
        if current.get("type") != "custom":
            if "state" in patch:
                raise BadRequest("state requires a custom default type")
            current.pop("state", None)
    if current.get("type") == "custom" and not current.get("state"):
        raise BadRequest("a custom default requires a state")
    # UNVERIFIED: real models must confirm set_device_info accepts this default_states object.
    await write(registry, caller, entry, "set_device_info", {"default_states": current})
    return {"device": clean(entry.slug), "changed": {"default_states": compact(current)}}


RULE_FIELDS = {
    "schedule": {"id", "enable", "mode", "s_min", "s_type", "e_min", "e_type", "time_offset",
                 "week_day", "year", "month", "day", "s_action", "e_action", "desired_states"},
    "countdown": {"id", "enable", "delay", "desired_states", "action"},
    "antitheft": {"id", "enable", "mode", "start_min", "end_min", "start_time_type", "end_time_type",
                  "start_time_offset", "end_time_offset", "frequency", "week_day", "year", "month", "day"},
}


def rule_object(kind, value, *, editing=False):
    choice(kind, "rule kind", RULE_FIELDS)
    out = fields(value, RULE_FIELDS[kind], "rule")
    if editing:
        identifier(out.get("id"))
    elif "id" in out:
        raise BadRequest("new rules must not contain an id")
    boolean(out.get("enable"), "enable")
    if kind == "countdown":
        integer(out.get("delay"), "delay seconds", 1, 86400)
        if "action" in out:
            choice(out["action"], "action", {"on", "off"})
        if "desired_states" in out:
            out["desired_states"] = state_object(out["desired_states"])
            boolean(out["desired_states"].get("on"), "desired_states.on")
            if "action" in out and out["action"] != ("on" if out["desired_states"]["on"] else "off"):
                raise BadRequest("action and desired_states.on must agree")
        if "action" not in out and "desired_states" not in out:
            raise BadRequest("countdown requires action or desired_states")
        return out
    # UNVERIFIED: uppercase enum wire values are inferred from the app's enum beans.
    # Confirm the serialization on a SMART bulb before using rules on real hardware.
    choice(out.get("mode"), "mode", {"ONCE", "REPEAT"})
    # UNVERIFIED: the weekday bit order (bit 0 Sunday) has not been seen in a real rule.
    days = integer(out.get("week_day"), "week_day bit mask", 0, 127)
    if out["mode"] == "REPEAT":
        if days == 0:
            raise BadRequest("a repeated rule requires at least one weekday")
        if any(out.get(k, 0) != 0 for k in ("year", "month", "day")):
            raise BadRequest("repeated rules must not contain a calendar date")
        for key in ("year", "month", "day"):
            if key in out:
                integer(out[key], key, 0, 0)
    else:
        if days != 0:
            raise BadRequest("a one-time rule requires week_day zero")
        try:
            date(integer(out.get("year"), "year", 2000, 2099),
                 integer(out.get("month"), "month", 1, 12), integer(out.get("day"), "day", 1, 31))
        except ValueError:
            raise BadRequest("the rule date does not exist") from None
    if kind == "schedule":
        ends = (("s_type", "s_min", "time_offset", False), ("e_type", "e_min", None, True))
        if "desired_states" in out:
            out["desired_states"] = state_object(out["desired_states"])
            boolean(out["desired_states"].get("on"), "desired_states.on")
        for key in ("s_action", "e_action"):
            if key in out:
                choice(out[key], key, {"on", "off"})
        if "s_action" in out and "desired_states" in out:
            if out["s_action"] != ("on" if out["desired_states"]["on"] else "off"):
                raise BadRequest("s_action and desired_states.on must agree")
        if "desired_states" not in out and "s_action" not in out:
            raise BadRequest("schedule requires desired_states or s_action")
    else:
        ends = (("start_time_type", "start_min", "start_time_offset", False),
                ("end_time_type", "end_min", "end_time_offset", False))
        integer(out.get("frequency"), "frequency minutes", 1, 1440)
    for type_key, min_key, offset_key, optional in ends:
        if optional and type_key not in out:
            if min_key in out or "e_action" in out:
                raise BadRequest("end time requires e_type")
            continue
        time_type = choice(out.get(type_key), type_key, {"NORMAL", "SUNRISE", "SUNSET", "NONE"} if optional
                           else {"NORMAL", "SUNRISE", "SUNSET"})
        if time_type == "NONE":
            if min_key in out:
                integer(out[min_key], min_key, 0, 0)
            if "e_action" in out:
                raise BadRequest("NONE end time cannot have an end action or nonzero minute")
        else:
            integer(out.get(min_key), min_key, 0, 1439)
        if offset_key and offset_key in out:
            integer(out[offset_key], offset_key, -720, 720)
            if time_type == "NORMAL" and out[offset_key] != 0:
                raise BadRequest("normal times cannot have a solar offset")
    if kind == "antitheft" and out["start_time_type"] == out["end_time_type"] == "NORMAL":
        if out["start_min"] == out["end_min"]:
            raise BadRequest("Away Mode needs a nonempty time window")
    return out


async def rules_page(registry, caller, ref, kind, start_index=0):
    choice(kind, "rule kind", RULE_FIELDS)
    integer(start_index, "start_index", 0, 10000)
    entry = light_entry(registry, ref)
    # All three reads are captured with start_index zero in the L430C fixture.
    params = {"start_index": start_index}
    if kind != "schedule" and start_index:
        raise BadRequest("only schedules have a start_index")
    result = await ask(caller, entry, f"get_{kind}_rules", params)
    if not isinstance(result, dict):
        raise DeviceUnavailable(f"{clean(entry.slug)} returned an invalid rule list")
    return entry, result


async def list_rules(registry, caller, ref, kind, start_index=0):
    entry, result = await rules_page(registry, caller, ref, kind, start_index)
    out = {"device": clean(entry.slug), "kind": kind}
    if compact(result):
        out["rules"] = compact(result)
    return out


async def change_rule(registry, caller, ref, kind, rule, *, editing=False):
    params = rule_object(kind, rule, editing=editing)
    entry = light_entry(registry, ref)
    method = f"{'edit' if editing else 'add'}_{kind}_rule"
    result = await ask(caller, entry, method, params)
    if result is False or (isinstance(result, dict) and result.get("error_code", 0) != 0):
        raise DeviceUnavailable(f"{clean(entry.slug)} refused {method}")
    if isinstance(result, dict) and result.get("conflict_id"):
        raise BadRequest(f"{clean(entry.slug)} reported a conflicting {kind} rule")
    await refreshed(registry, entry)
    out = {"device": clean(entry.slug), "kind": kind, "changed": compact(params)}
    if isinstance(result, dict) and result.get("id"):
        out["id"] = clean(str(result["id"]), limit=128)
    return out


async def delete_rule(registry, caller, ref, kind, rule_id):
    choice(kind, "rule kind", RULE_FIELDS)
    identifier(rule_id)
    entry = light_entry(registry, ref)
    await write(registry, caller, entry, f"remove_{kind}_rules",
                {"remove_all": False, "rule_list": [{"id": rule_id}]})
    return {"device": clean(entry.slug), "kind": kind, "deleted": rule_id}


async def enable_rule(registry, caller, ref, kind, rule_id, enable):
    identifier(rule_id)
    boolean(enable, "enable")
    index = 0
    for _ in range(100):
        entry, result = await rules_page(registry, caller, ref, kind, index)
        rows = result.get("rule_list", result.get("ruleList", result.get("rules", [])))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DeviceUnavailable(f"{clean(entry.slug)} returned an invalid rule list")
        for row in rows:
            if row.get("id") == rule_id:
                # Remain is read-only. Refuse unknown writable fields instead of losing them.
                params = {k: v for k, v in row.items() if k != "remain"}
                params["enable"] = enable
                return await change_rule(registry, caller, ref, kind, params, editing=True)
        index += len(rows)
        total = result.get("sum", index)
        if not rows or kind != "schedule" or type(total) is not int or index >= total:
            break
    raise NotFound(f"{clean(entry.slug)} has no matching {kind} rule")


async def next_event(registry, caller, ref):
    entry = light_entry(registry, ref)
    result = await ask(caller, entry, "get_next_event")
    out = {"device": clean(entry.slug)}
    if compact(result):
        out["next_event"] = compact(result)
    return out


LIGHT_FEATURES = {"light_effect", "smooth_transitions", "smooth_transition_on", "smooth_transition_off",
                  "smooth_transition_change", "strip_segments", "led"}


def effect_module(registry, entry):
    module = entry.device.modules.get(Module.LightEffect)
    if module is None:
        raise Unsupported(f"{clean(entry.slug)} has no lighting effects module")
    return module


async def get_features(registry, ref):
    entry = light_entry(registry, ref)
    await registry.refresh(entry)
    if not entry.online:
        raise DeviceUnavailable(f"{clean(entry.slug)} is not reachable")
    values = {}
    for name in sorted(LIGHT_FEATURES):
        feature = entry.device.features.get(name)
        if feature is None:
            continue
        value = compact(registry.feature_value(feature))
        row = {"writable": policy.can_write(entry.kind, name, registry.cfg.allow_write, registry.cfg.tier)}
        if value not in (None, "", [], {}):
            row["value"] = value
        if feature.choices:
            row["choices"] = [clean(str(v)) for v in feature.choices]
        values[name] = row
    return {"device": clean(entry.slug), **({"features": values} if values else {})}


async def set_option(registry, ref, feature_id, value):
    choice(feature_id, "lighting feature", LIGHT_FEATURES - {"light_effect"})
    entry = light_entry(registry, ref)
    if feature_id in {"smooth_transition_on", "smooth_transition_off", "strip_segments"}:
        integer(value, feature_id, 0, 3600 if feature_id != "strip_segments" else 1000)
    else:
        boolean(value, feature_id)
    # The registry keeps the existing allow_write policy and the device's own feature bounds.
    await registry.set_feature(entry, feature_id, value)
    if not entry.online:
        raise DeviceUnavailable(f"{clean(entry.slug)} accepted the change but its state could not be refreshed")
    return {"device": clean(entry.slug), "changed": {feature_id: value}}


async def set_effect(registry, ref, effect, brightness=None, transition=None):
    # None keeps python-kasa's behavior: the light's current brightness and the effect's own transition.
    if brightness is not None:
        integer(brightness, "brightness", 1, 100)
    if transition is not None:
        integer(transition, "transition milliseconds", 0, 60000)
    entry = light_entry(registry, ref)
    registry._require_write(entry, "light_effect")
    module = effect_module(registry, entry)
    choice(effect, "effect", module.effect_list)
    await registry.guarded(entry, lambda: module.set_effect(effect, brightness=brightness, transition=transition))
    await refreshed(registry, entry)
    on = effect != module.LIGHT_EFFECTS_OFF
    return {"device": clean(entry.slug), "changed": {"light_effect": clean(effect),
            **({"brightness": brightness} if on and brightness is not None else {}),
            **({"transition": transition} if on and transition is not None else {})}}


COLOR_FIELDS = {"sequence", "display_colors", "init_states", "backgrounds"}
EFFECT_FIELDS = COLOR_FIELDS | {
    "id", "name", "type", "custom", "enable", "brightness", "segments", "duration", "transition",
    "transition_range", "hue_range", "saturation_range", "brightness_range", "direction", "fadeoff",
    "spread", "repeat_times", "expansion_strategy", "random_seed", "segment_length", "run_time", "trans_sequence",
}


def effect_object(value):
    out = fields(value, EFFECT_FIELDS, "custom effect")
    identifier(out.get("id"))
    name = out.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 64 or clean(name) != name:
        raise BadRequest("effect name must be 1 to 64 characters on one line")
    choice(out.get("type"), "effect type", {"static", "sequence", "random", "pulse"})
    out.setdefault("custom", 1)
    integer(out["custom"], "custom", 1, 1)
    out.setdefault("enable", 1)
    integer(out["enable"], "enable", 0, 1)
    out.setdefault("brightness", 50)
    integer(out["brightness"], "brightness", 1, 100)
    if out["type"] != "static":
        out.setdefault("transition", 1000)
    for key in COLOR_FIELDS:
        if key not in out:
            continue
        rows = out[key]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
            raise BadRequest(f"{key} must have 1 to 128 HSV color triplets")
        for color in rows:
            if not isinstance(color, list) or len(color) != 3:
                raise BadRequest(f"{key} requires HSV triplets")
            for component, name, hi in zip(color, ("hue", "saturation", "value"), (360, 100, 100)):
                integer(component, name, 0, hi)
    if out["type"] in {"static", "sequence", "pulse"} and "sequence" not in out:
        raise BadRequest("this effect type requires a sequence")
    if out["type"] == "static":
        # UNVERIFIED: the one-boundary-per-color painting shape needs a real strip fixture.
        segments = out.get("segments")
        if not isinstance(segments, list) or len(segments) != len(out["sequence"]):
            raise BadRequest("a static painting requires one segment boundary per color")
    if "segments" in out:
        segments = out["segments"]
        if not isinstance(segments, list) or not 1 <= len(segments) <= 128:
            raise BadRequest("segments must contain 1 to 128 boundaries")
        for segment in segments:
            integer(segment, "segment boundary", 0, 1000)
        if any(a >= b for a, b in zip(segments, segments[1:])):
            raise BadRequest("segment boundaries must increase strictly")
    for key, hi in (("duration", 3600000), ("transition", 60000), ("fadeoff", 60000), ("run_time", 3600000),
                    ("direction", 4), ("spread", 1000), ("repeat_times", 10000), ("expansion_strategy", 2),
                    ("random_seed", 2147483647), ("segment_length", 1000)):
        if key in out:
            integer(out[key], key, 0, hi)
    for key, hi in (("transition_range", 60000), ("hue_range", 360), ("saturation_range", 100), ("brightness_range", 100)):
        if key in out:
            values = out[key]
            if not isinstance(values, list) or len(values) != 2:
                raise BadRequest(f"{key} requires a minimum and maximum")
            integer(values[0], key, 0, hi)
            integer(values[1], key, values[0], hi)
    if out["type"] == "random" and "hue_range" not in out:
        raise BadRequest("random effects require hue_range")
    if "trans_sequence" in out and out["trans_sequence"] != []:
        raise BadRequest("only an empty trans_sequence has a verified shape")
    return out


async def custom_effect(registry, ref, effect):
    params = effect_object(effect)
    entry = light_entry(registry, ref)
    registry._require_write(entry, "light_effect")
    module = effect_module(registry, entry)
    if not module.has_custom_effects:
        raise Unsupported(f"{clean(entry.slug)} does not support custom strip effects")
    await registry.guarded(entry, lambda: module.set_custom_effect(params))
    await refreshed(registry, entry)
    return {"device": clean(entry.slug), "changed": {"id": params["id"], "name": clean(params["name"]),
            "type": params["type"], "brightness": params["brightness"], "enable": params["enable"]}}


async def get_auto_white(registry, caller, ref):
    entry = light_entry(registry, ref)
    result = await ask(caller, entry, "get_auto_light_info")
    if not isinstance(result, dict):
        raise DeviceUnavailable(f"{clean(entry.slug)} returned invalid Auto-White information")
    auto = compact({k: v for k, v in result.items() if k in {"enable", "mode"}})
    return {"device": clean(entry.slug), **({"auto_white": auto} if auto else {})}


async def set_auto_white(registry, caller, ref, enable, mode=None):
    boolean(enable, "enable")
    if mode is not None:
        choice(mode, "mode", AUTO_MODES)
    elif enable:
        raise BadRequest("enabling Auto-White requires a mode")
    entry = light_entry(registry, ref)
    # Read first: the L430C lists an auto_light component but answers get_auto_light_info with an
    # unknown-method error. A model that cannot read Auto-White is refused before any write is sent.
    params = {"enable": enable, **({"mode": mode} if mode is not None else {})}
    if caller.plan(entry, "set_auto_light_info")["allowed"]:
        current = await ask(caller, entry, "get_auto_light_info")
        if not isinstance(current, dict):
            raise DeviceUnavailable(f"{clean(entry.slug)} returned invalid Auto-White information")
    # A blocked write goes straight to the caller, which refuses it without touching the device.
    return await write(registry, caller, entry, "set_auto_light_info", params)


async def presets_page(registry, caller, ref, start_index):
    integer(start_index, "start_index", 0, 10000)
    entry = light_entry(registry, ref)
    # UNVERIFIED: ordinary child forwarding must be checked on a ceiling light's ring/downlight.
    result = await ask(caller, entry, "get_preset_rules", {"start_index": start_index})
    if not isinstance(result, dict):
        raise DeviceUnavailable(f"{clean(entry.slug)} returned an invalid preset list")
    return entry, result


async def get_presets(registry, caller, ref, start_index=0):
    entry, result = await presets_page(registry, caller, ref, start_index)
    presets = compact({k: v for k, v in result.items() if k in {"start_index", "sum", "states", "brightness"}})
    return {"device": clean(entry.slug), **({"presets": presets} if presets else {})}


async def edit_preset(registry, caller, ref, index, state):
    integer(index, "preset index", 0, 127)
    params = state_object(state)
    if set(params) - {"brightness", "color_temp", "hue", "saturation"} or "brightness" not in params:
        raise BadRequest("a preset requires brightness and optional color_temp or hue and saturation")
    entry, result = await presets_page(registry, caller, ref, 0)
    if isinstance(result.get("brightness"), list) and not result.get("states"):
        bright = result["brightness"][:]
        if not bright or len(bright) > 128 or index >= len(bright):
            raise BadRequest("preset index is outside the stored brightness list")
        for value in bright:
            integer(value, "stored preset brightness", 1, 100)
        if result.get("sum", len(bright)) != len(bright):
            raise Unsupported(f"{clean(entry.slug)} returned an incomplete brightness preset list")
        if set(params) != {"brightness"}:
            raise BadRequest("this light has brightness-only presets")
        previous = {"brightness": bright[index]}
        bright[index] = params["brightness"]
        await write(registry, caller, entry, "set_preset_rules", {"brightness": bright})
    else:
        start = 0
        previous = None
        for _ in range(128):
            states = result.get("states")
            if not isinstance(states, list) or any(not isinstance(row, dict) for row in states):
                raise Unsupported(f"{clean(entry.slug)} did not report editable state presets")
            reported_start = result.get("start_index", start)
            if type(reported_start) is not int or reported_start != start:
                raise DeviceUnavailable(f"{clean(entry.slug)} returned an invalid preset page")
            if start <= index < start + len(states):
                previous = compact(states[index - start])
                break
            start += len(states)
            total = result.get("sum", start)
            if not states or type(total) is not int or start >= total:
                raise BadRequest("preset index is outside the stored state list")
            entry, result = await presets_page(registry, caller, ref, start)
        if previous is None:
            raise BadRequest("preset list exceeded the supported page count")
        await write(registry, caller, entry, "edit_preset_rules", {"index": index, "state": params})
    return {"device": clean(entry.slug), "index": index, "previous": previous, "changed": compact(params)}


async def get_segment_effect(registry, caller, ref):
    entry = light_entry(registry, ref)
    info = await ask(caller, entry, "get_device_info")
    result = info.get("segment_effect") if isinstance(info, dict) else None
    if not isinstance(result, dict):
        result = await ask(caller, entry, "get_segment_effect_rule")
    if not isinstance(result, dict):
        raise DeviceUnavailable(f"{clean(entry.slug)} returned invalid segment effect information")
    # states and display_colors are deliberately omitted: their V2 encodings are undecoded.
    metadata = compact({k: v for k, v in result.items() if k in {
        "id", "name", "type", "enable", "custom", "brightness", "segments", "speed", "carousel", "deviceType"}})
    return {"device": clean(entry.slug), **({"segment_effect": metadata} if metadata else {})}


def tools(ctx):
    @ctx.tool(title="Light power-on defaults")
    async def light_get_defaults(device: DeviceRef) -> dict[str, Any]:
        """Read power-on defaults and power-outage behavior for one light or light child."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await get_defaults(ctx.registry, ctx.caller, device)

    @ctx.tool(title="Set light power-on defaults", tier=Tier.SETTING)
    async def light_set_defaults(device: DeviceRef, settings: Object) -> dict[str, Any]:
        """Change power-on or power-outage defaults. Supports individual light children."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await set_defaults(ctx.registry, ctx.caller, device, settings)

    @ctx.tool(title="Light device rules")
    async def light_list_rules(device: DeviceRef, kind: RuleKind,
                               start_index: Annotated[int, Field(strict=True, ge=0, description="Schedule page start index.")] = 0) -> dict[str, Any]:
        """List on-device schedules, countdown timers, or Away Mode rules."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await list_rules(ctx.registry, ctx.caller, device, kind, start_index)

    @ctx.tool(title="Add light device rule", tier=Tier.SETTING)
    async def light_add_rule(device: DeviceRef, kind: RuleKind, rule: Object) -> dict[str, Any]:
        """Add a validated schedule, countdown timer, or Away Mode rule. Times use device local time."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await change_rule(ctx.registry, ctx.caller, device, kind, rule)

    @ctx.tool(title="Edit light device rule", tier=Tier.SETTING)
    async def light_edit_rule(device: DeviceRef, kind: RuleKind, rule: Object) -> dict[str, Any]:
        """Replace a complete existing lighting rule, including its id."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await change_rule(ctx.registry, ctx.caller, device, kind, rule, editing=True)

    @ctx.tool(title="Delete light device rule", tier=Tier.SETTING)
    async def light_delete_rule(device: DeviceRef, kind: RuleKind, rule_id: RuleId) -> dict[str, Any]:
        """Delete one on-device lighting rule by id."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await delete_rule(ctx.registry, ctx.caller, device, kind, rule_id)

    @ctx.tool(title="Enable light device rule", tier=Tier.SETTING)
    async def light_enable_rule(device: DeviceRef, kind: RuleKind, rule_id: RuleId, enable: Enabled) -> dict[str, Any]:
        """Enable or disable a stored rule while preserving its times, days and target state."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await enable_rule(ctx.registry, ctx.caller, device, kind, rule_id, enable)

    @ctx.tool(title="Next light event")
    async def light_next_event(device: DeviceRef) -> dict[str, Any]:
        """Read the next on-device event for a light or an individual ceiling-light child."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await next_event(ctx.registry, ctx.caller, device)

    @ctx.tool(title="Light effects and settings")
    async def light_features(device: DeviceRef) -> dict[str, Any]:
        """Read built-in effect choices, fade settings, strip length, and LED when the device exposes them."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await get_features(ctx.registry, device)

    @ctx.tool(title="Apply light effect", tier=Tier.CONTROL)
    async def light_set_effect(device: DeviceRef,
                               effect: Annotated[str, Field(description="Exact built-in effect name from light_features, including Off.")],
                               brightness: Annotated[int | None, Field(strict=True, description="Effect brightness from 1 to 100. Leave out to keep the current brightness.")] = None,
                               transition: Annotated[int | None, Field(strict=True, description="Transition milliseconds from 0 to 60000. Leave out to keep the effect's own timing.")] = None) -> dict[str, Any]:
        """Apply a built-in strip effect or bulb Party or Relax. The light_effect write permission still applies."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await set_effect(ctx.registry, device, effect, brightness, transition)

    @ctx.tool(title="Set light fade", tier=Tier.SETTING)
    async def light_set_transition(device: DeviceRef,
                                   kind: Annotated[Literal["on", "off", "change", "all"], Field(description="On/off fade duration, change smoothing, or older all-fades toggle.")],
                                   value: Annotated[StrictInt | StrictBool, Field(description="On/off: seconds, zero disables. Change/all: true or false.")]) -> dict[str, Any]:
        """Set an on/off fade or smoothing toggle. The existing feature write permission and device bounds apply."""
        await ctx.ready_for([device])
        with ctx.translated():
            choice(kind, "transition kind", {"on", "off", "change", "all"})
            feature = "smooth_transitions" if kind == "all" else f"smooth_transition_{kind}"
            return await set_option(ctx.registry, device, feature, value)

    @ctx.tool(title="Set light strip length", tier=Tier.SETTING)
    async def light_set_length(device: DeviceRef,
                               segments: Annotated[int, Field(strict=True, description="Number of 10 cm segments. The device's feature range applies.")]) -> dict[str, Any]:
        """Set the persistent lit length of a strip. The strip_segments write permission still applies."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await set_option(ctx.registry, device, "strip_segments", segments)

    @ctx.tool(title="Set light LED", tier=Tier.SETTING)
    async def light_set_led(device: DeviceRef, enable: Enabled) -> dict[str, Any]:
        """Set the indicator LED if this light exposes it. The led write permission still applies."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await set_option(ctx.registry, device, "led", enable)

    @ctx.tool(title="Apply custom strip effect", tier=Tier.CONTROL)
    async def light_custom_effect(device: DeviceRef, effect: Object) -> dict[str, Any]:
        """Apply a validated classic strip effect, including static color painting. Requires light_effect write permission."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await custom_effect(ctx.registry, device, effect)

    @ctx.tool(title="Light Auto-White mode")
    async def light_get_auto_white(device: DeviceRef) -> dict[str, Any]:
        """Read the Auto-White enable flag and mode for one light or child."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await get_auto_white(ctx.registry, ctx.caller, device)

    @ctx.tool(title="Set light Auto-White mode", tier=Tier.SETTING)
    async def light_set_auto_white(device: DeviceRef, enable: Enabled,
                                   mode: Annotated[Literal["circadian", "light_compensate", "light_track"] | None,
                                                   Field(description="Auto-White mode. Required when enabling.")] = None) -> dict[str, Any]:
        """Enable or disable circadian, light compensation, or light tracking Auto-White."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await set_auto_white(ctx.registry, ctx.caller, device, enable, mode)

    @ctx.tool(title="Light saved presets")
    async def light_get_presets(device: DeviceRef,
                                start_index: Annotated[int, Field(strict=True, ge=0, description="Preset page start index.")] = 0) -> dict[str, Any]:
        """Read saved brightness or color states for one light or light child."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await get_presets(ctx.registry, ctx.caller, device, start_index)

    @ctx.tool(title="Edit light saved preset", tier=Tier.SETTING)
    async def light_edit_preset(device: DeviceRef,
                                index: Annotated[int, Field(strict=True, description="Zero-based stored preset index from 0 to 127.")],
                                state: Object) -> dict[str, Any]:
        """Edit one saved preset. Return the previous value. Use a child id for ceiling-light presets."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await edit_preset(ctx.registry, ctx.caller, device, index, state)

    @ctx.tool(title="Light segment effect metadata")
    async def light_get_segment_effect(device: DeviceRef) -> dict[str, Any]:
        """Read decoded Lighting Effect V2 metadata. Omit undecoded states and display colors."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await get_segment_effect(ctx.registry, ctx.caller, device)
