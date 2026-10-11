"""Device-side plug and switch rules. All traffic uses the policy caller."""

import base64
import math
import re
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field

from .. import policy, views
from ..policy import Tier
from ..registry import DeviceUnavailable, Forbidden, RegistryError

KINDS = ["plug", "switch"]
RuleKind = Literal["countdown", "schedule", "away"]
RULE_NAMES = {"countdown": "countdown", "schedule": "schedule", "away": "antitheft"}
METHODS = [
    policy.method(f"get_{name}_rules", Tier.READ, KINDS, f"Read {label} rules.")
    for label, name in RULE_NAMES.items()
] + [
    policy.method(f"{action}_{name}_{suffix}", Tier.SETTING, KINDS, f"{summary} {label} rules.")
    for label, name in RULE_NAMES.items()
    for action, suffix, summary in (("add", "rule", "Add"), ("edit", "rule", "Edit"), ("remove", "rules", "Delete"))
] + [
    policy.method("set_schedule_all_enable", Tier.SETTING, KINDS, "Enable or disable all schedule rules."),
    policy.method("set_antitheft_all_enable", Tier.SETTING, KINDS, "Enable or disable all Away Mode rules."),
]

# Only protocol facts needed by this area may reach an agent. Unknown fields are dropped,
# including credentials and identifiers belonging to the network or account.
RULE_FIELDS = frozenset({
    "id", "delay", "remain", "enable", "action", "desired_states", "device_on",
    "s_min", "e_min", "s_type", "e_type", "s_action", "e_action", "week_day",
    "year", "month", "day", "mode", "time_offset", "start_min", "end_min",
    "start_time_type", "end_time_type", "start_time_offset", "end_time_offset", "frequency",
})
OUTPUT_FIELDS = RULE_FIELDS | {"rule_list", "start_index", "sum", "countdown_rule_max_count",
                              "schedule_rule_max_count", "antitheft_rule_max_count", "conflict_id"}

METHODS += [
    policy.method(method, Tier.READ, KINDS, summary)
    for method, summary in (
        ("get_energy_usage", "Read energy totals, runtime and cost figures."),
        ("get_energy_data", "Read one page of energy history."),
        ("get_energy_data_mwh", "Read one page of energy history in milliwatt-hours."),
        ("get_power_data", "Read one page of power history."),
        ("get_electricity_price_config", "Read the electricity price configuration."),
    )
] + [
    policy.method("set_electricity_price_config", Tier.SETTING, KINDS, "Set a constant electricity price."),
    policy.method("clear_energy_data", Tier.SECURITY, KINDS, "Permanently erase the device's energy statistics."),
]
DEFAULT_FIELDS = {"default_states", "type", "state", "on", "on_off", "last_on_off", "brightness", "value",
                  "re_power_type", "re_power_type_capability"}
ENERGY_FIELDS = {"today_energy", "month_energy", "today_runtime", "month_runtime", "past7_runtime",
                 "current_power", "local_time", "electricity_charge", "saved_electricity_charge",
                 "today_charge_energy", "month_charge_energy", "today_energy_mwh", "month_energy_mwh",
                 "today_charge_energy_mwh", "month_charge_energy_mwh", "past24h_mwh", "past7d_mwh",
                 "past30d_mwh", "past1y_mwh", "today", "past7", "past30", "time_usage", "on_time"}
ENERGY_FIELDS |= {"past24h", "past7d", "past30d", "past1y"}
HISTORY_FIELDS = {"start_timestamp", "end_timestamp", "data", "charge_data", "interval", "version"}
PRICE_FIELDS = {"type", "constant_price", "time_of_use_config", "summer", "winter", "onpeak", "midpeak",
                "offpeak", "period", "weekday_config", "weekend_config"}
HARDWARE_KINDS = ["plug", "switch", "light"]  # python-kasa classifies dimmers as lights
METHODS += [
    policy.method(method, Tier.READ, KINDS, summary)
    for method, summary in (
        ("get_auto_off_config", "Read the automatic shut-off configuration."),
        ("get_charging_protect", "Read Charge Guard configuration."),
        ("get_protection_power", "Read power protection configuration."),
        ("get_max_power", "Read the device's maximum power."),
    )
] + [
    policy.method(method, Tier.READ, HARDWARE_KINDS, summary)
    for method, summary in (
        ("get_delay_action_info", "Read delayed shut-off configuration."),
        ("get_calibrate_brightness", "Read dimming calibration."),
        ("get_dimmer_custom_action_info", "Read hardware button actions."),
        ("get_double_click_info", "Read double-click configuration."),
        ("get_sleep_mode_interval", "Read the hardware sleep interval."),
        ("get_led_info", "Read the indicator LED configuration."),
    )
] + [
    policy.method("cancel_auto_off", Tier.SECURITY, KINDS, "Cancel the running auto-off timer and leave the load energized longer."),
    policy.method("set_delay_action_info", Tier.SETTING, HARDWARE_KINDS, "Configure delayed shut-off."),
    policy.method("set_double_click_info", Tier.SETTING, HARDWARE_KINDS, "Configure double-click recognition."),
    policy.method("set_calibrate_brightness", Tier.SETTING, HARDWARE_KINDS, "Set dimming calibration thresholds and an optional curve."),
    policy.method("set_led_info", Tier.SETTING, HARDWARE_KINDS, "Configure the indicator LED and its night window."),
]
STATUS_FIELDS = {"overcurrent_status", "overheat_status", "power_protection_status", "charging_status",
                 "auto_off_status", "auto_off_remain_time", "auto_off_end_time", "dc_state"}
AUTO_FIELDS = {"enable", "delay_min"}
CHARGE_FIELDS = {"charging_enabled", "charging_power", "charging_time", "type"}
POWER_FIELDS = {"enabled", "protection_power", "max_power"}
DELAY_FIELDS = {"enabled", "state", "on", "time", "delay_trigger"}
CALIBRATION_FIELDS = {"curve_type", "min_threshold", "max_threshold"}
# UNVERIFIED: hardware read envelopes below are inferred from the parameter beans.
# No wall-switch or dimmer response fixture was available.
BUTTON_FIELDS = {"short_press", "double_click", "gentle_on", "gentle_off"}
DOUBLE_FIELDS = {"enable", "interval"}
LED_FIELDS = {"led_rule", "led_status", "night_mode", "night_mode_type", "start_time", "end_time",
              "sunrise_offset", "sunset_offset", "bri_config", "bri_type", "overall_bri", "day_bri", "night_bri"}
# UNVERIFIED: wiring_status is the candidate wire key for the audited WiringStatus enum.
HARDWARE_FIELDS = {"low_power_mode", "cooldown", "dc_state", "wiring_status", "fan_sleep_mode_on", "fan_speed_level"}
RUNTIME_FIELDS = {"time_usage", "today", "past7", "past30"}
HardwareSection = Literal["state", "delay", "calibration", "buttons", "double_click", "sleep", "led"]
HARDWARE_READS = {
    "state": ("get_device_info", HARDWARE_FIELDS), "delay": ("get_delay_action_info", DELAY_FIELDS),
    "calibration": ("get_calibrate_brightness", CALIBRATION_FIELDS),
    "buttons": ("get_dimmer_custom_action_info", BUTTON_FIELDS),
    "double_click": ("get_double_click_info", DOUBLE_FIELDS),
    "sleep": ("get_sleep_mode_interval", {"interval"}), "led": ("get_led_info", LED_FIELDS),
}


async def protection(registry, caller, device, section="state"):
    choices = {"state": ("get_device_info", STATUS_FIELDS), "charge_guard": ("get_charging_protect", CHARGE_FIELDS),
               "power": ("get_protection_power", POWER_FIELDS), "max_power": ("get_max_power", POWER_FIELDS)}
    if section not in choices:
        raise ValueError("protection section must be state, charge_guard, power or max_power")
    method, fields = choices[section]
    return await read(registry, caller, device, method, fields)


async def identity(registry, caller, device, name=None, icon=None):
    params = {}
    if name is not None:
        if not isinstance(name, str) or not name.strip() or len(name) > 64 or views.clean(name) != name:
            raise ValueError("name must be 1 to 64 flat characters without surrounding whitespace")
        params["nickname"] = base64.b64encode(name.encode()).decode("ascii")
    if icon is not None:
        if not isinstance(icon, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", icon):
            raise ValueError("icon must be 1 to 64 letters, digits, underscores or hyphens")
        params["avatar"] = icon
    if not params:
        raise ValueError("supply a name or icon")
    out = await write(registry, caller, device, "set_device_info", params)
    out["fields"] = [key for key, value in (("name", name), ("icon", icon)) if value is not None]
    return out


async def location(registry, caller, device, latitude, longitude):
    for value, limit, label in ((latitude, 90, "latitude"), (longitude, 180, "longitude")):
        if type(value) not in (int, float) or not math.isfinite(value) or not -limit <= value <= limit:
            raise ValueError(f"{label} must be a finite number from {-limit} to {limit}")
    # python-kasa uses ten-thousandths of a degree in device info.
    params = {"latitude": round(latitude * 10000), "longitude": round(longitude * 10000)}
    out = await write(registry, caller, device, "set_device_info", params)
    out["fields"] = ["location"]
    return out


async def hardware(registry, caller, device, section):
    if section not in HARDWARE_READS:
        raise ValueError("unknown hardware section")
    method, fields = HARDWARE_READS[section]
    return await read(registry, caller, device, method, fields, kinds=HARDWARE_KINDS)


async def delay_off(registry, caller, device, enabled, seconds):
    # UNVERIFIED: state.on=false with time in seconds comes from the delay-action
    # beans; v2 delay_trigger values are not known, so this sends the v1 shape only.
    params = {"enabled": boolean(enabled), "state": {"on": False}, "time": integer(seconds, 1, 86400, "seconds")}
    return await write(registry, caller, device, "set_delay_action_info", params, kinds=HARDWARE_KINDS)


async def double_click(registry, caller, device, enabled, interval):
    # UNVERIFIED: milliseconds and 100..2000 are conservative tool bounds, not model limits.
    params = {"enable": boolean(enabled), "interval": integer(interval, 100, 2000, "interval_ms")}
    return await write(registry, caller, device, "set_double_click_info", params, kinds=HARDWARE_KINDS)


async def calibration(registry, caller, device, minimum, maximum, curve=None):
    # UNVERIFIED: threshold percentages and curve ids need a dimmer fixture.
    # Bounds are tool limits. No calibration test is started by this call.
    low, high = integer(minimum, 1, 100, "minimum"), integer(maximum, 1, 100, "maximum")
    if low > high:
        raise ValueError("minimum must not exceed maximum")
    params = {"min_threshold": low, "max_threshold": high}
    if curve is not None:
        params["curve_type"] = integer(curve, 0, 255, "curve_type")
    return await write(registry, caller, device, "set_calibrate_brightness", params, kinds=HARDWARE_KINDS)


async def low_power(registry, caller, device, enabled):
    boolean(enabled)
    entry = target(registry, device, HARDWARE_KINDS)
    info = response_object(entry, "get_device_info", await caller.call(entry, "get_device_info"))
    if "low_power_mode" not in info:
        raise RegistryError(f"{views.clean(entry.slug)}: device did not report low_power_mode")
    return await write(registry, caller, device, "set_device_info", {"low_power_mode": int(enabled)}, kinds=HARDWARE_KINDS)


async def led_set(registry, caller, device, mode, start=None, end=None):
    if mode not in ("always", "never", "night_mode"):
        raise ValueError("LED mode must be always, never or night_mode")
    params = {"led_rule": mode}
    if mode == "night_mode":
        first, last = minute(start), minute(end)
        if first == last:
            raise ValueError("LED night window must be nonzero")
        # UNVERIFIED: time is the candidate fixed-window enum. The P115 fixture
        # confirms the nested night_mode object but only sunrise_sunset was captured.
        params["night_mode"] = {"night_mode_type": "time", "start_time": first, "end_time": last}
    elif start is not None or end is not None:
        raise ValueError("start and end only apply to night_mode")
    # Dimmers may be lights. Ordinary lights belong to the lights area.
    entry = registry.resolve_one(device)
    kinds = HARDWARE_KINDS if entry.device.device_type.name == "Dimmer" else KINDS
    return await write(registry, caller, device, "set_led_info", params, kinds=kinds)


async def read(registry, caller, device, method, fields, params=None, kinds=KINDS):
    entry = target(registry, device, kinds)
    result = response_object(entry, method, await caller.call(entry, method, params))
    return {"device": views.clean(entry.slug), **compact(result, fields)}


async def default_state(registry, caller, device):
    entry = target(registry, device)
    result = response_object(entry, "get_device_info", await caller.call(entry, "get_device_info"))
    return {"device": views.clean(entry.slug), **compact({"default_states": result.get("default_states")}, DEFAULT_FIELDS)}


async def set_default_state(registry, caller, device, mode):
    if mode not in ("last_states", "always_on", "always_off", "toggle_states"):
        raise ValueError("default must be last_states, always_on, always_off or toggle_states")
    entry = target(registry, device)
    info = response_object(entry, "get_device_info", await caller.call(entry, "get_device_info"))
    current = info.get("default_states")
    if not isinstance(current, dict):
        raise RegistryError(f"{views.clean(entry.slug)}: device did not report default_states")
    capabilities = current.get("re_power_type_capability")
    if capabilities:
        if mode not in capabilities:
            raise ValueError("requested power-loss default is not supported by this socket")
        # UNVERIFIED: smart_status plus re_power_type needs a v2 device fixture.
        payload = {"type": "smart_status", "re_power_type": mode}
    elif mode == "last_states":
        payload = {"type": "last_states"}
    elif mode == "toggle_states":
        raise ValueError("toggle needs a reported re_power_type capability")
    else:
        # UNVERIFIED: custom state.on is the legacy bean shape; verify on a plug.
        payload = {"type": "custom", "state": {"on": mode == "always_on"}}
    out = await write(registry, caller, device, "set_device_info", {"default_states": payload})
    out["default"] = mode
    return out


async def energy_history(registry, caller, device, series, start, end, interval, precision="wh"):
    if series not in ("energy", "power") or precision not in ("wh", "mwh"):
        raise ValueError("series must be energy or power; precision must be wh or mwh")
    integer(start, 0, 253402300799, "start_timestamp")
    integer(end, 0, 253402300799, "end_timestamp")
    intervals = (60, 1440, 43200) if series == "energy" else (5, 60)
    if type(interval) is not int or interval not in intervals:
        raise ValueError(f"{series} interval must be one of {intervals} minutes")
    if end <= start or end - start > interval * 60 * 512:
        raise ValueError("history range must be positive and contain at most 512 intervals")
    if series == "power" and precision != "wh":
        raise ValueError("power history does not have an energy precision option")
    method = "get_power_data" if series == "power" else "get_energy_data_mwh" if precision == "mwh" else "get_energy_data"
    out = await read(registry, caller, device, method, HISTORY_FIELDS,
                     {"start_timestamp": start, "end_timestamp": end, "interval": interval})
    if any(len(out.get(key, [])) > 513 for key in ("data", "charge_data")):
        raise DeviceUnavailable(f"{out['device']}: {method} returned too many samples")
    return out


async def set_price(registry, caller, device, price):
    integer(price, 0, 2147483647, "constant_price")
    out = await write(registry, caller, device, "set_electricity_price_config", {"type": "constant", "constant_price": price})
    out["constant_price"] = price
    return out


def compact(value, allowed=OUTPUT_FIELDS):
    """Allow known fields only, flatten strings, and omit empty values without losing zero."""
    if isinstance(value, dict):
        out = {key: compact(item, allowed) for key, item in value.items() if key in allowed}
        return {key: item for key, item in out.items() if item is not None and item != "" and item != [] and item != {}}
    if isinstance(value, list):
        return [compact(item, allowed) for item in value]
    if isinstance(value, str):
        return views.clean(value)
    if value is None or type(value) in (bool, int, float):
        return value
    return None


def response_object(entry, method, result):
    if not isinstance(result, dict):
        raise DeviceUnavailable(f"{views.clean(entry.slug)}: {method} returned an invalid response")
    return result


def rule_list(entry, method, result):
    rules = result.get("rule_list", [])
    if not isinstance(rules, list) or any(not isinstance(rule, dict) for rule in rules):
        raise DeviceUnavailable(f"{views.clean(entry.slug)}: {method} returned an invalid rule list")
    return rules


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer from {low} to {high}")
    return value


def boolean(value, label="enabled"):
    if type(value) is not bool:
        raise ValueError(f"{label} must be true or false")
    return value


def rule_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("rule id must contain 1 to 64 letters, digits, underscores or hyphens")
    return value


def minute(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("time must be HH:MM from 00:00 to 23:59")
    hour, minutes = value.split(":")
    return int(hour) * 60 + int(minutes)


def calendar(days, on_date):
    if not isinstance(days, list) or len(days) > 7 or len(set(days)) != len(days):
        raise ValueError("days must be unique weekdays from 0 (Sunday) to 6 (Saturday)")
    mask = sum(1 << integer(day, 0, 6, "weekday") for day in days)
    if bool(days) == bool(on_date):
        raise ValueError("supply weekdays for a repeating rule or one YYYY-MM-DD date")
    # UNVERIFIED: numeric mode/time enums need SMART fixtures (repeat=1, once=0;
    # normal=1, none=0). Field names come from the audited rule beans.
    out = {"week_day": mask, "mode": 1 if days else 0}
    if on_date:
        if not isinstance(on_date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", on_date):
            raise ValueError("date must be YYYY-MM-DD")
        try:
            when = date.fromisoformat(on_date)
        except ValueError:
            raise ValueError("date must be a valid calendar date") from None
        out.update(year=when.year, month=when.month, day=when.day)
    return out


def target(registry, device, kinds=KINDS):
    entry = registry.resolve_one(device)
    # A parent with children (a power strip, a multi-gang switch) is never a target: a write there could act on
    # every socket at once. Each socket or gang is its own entry and goes through python-kasa's child protocol.
    if entry.kind not in kinds or entry.device.device_type.name == "Strip" or getattr(entry.device, "children", None):
        raise Forbidden(f"{views.clean(entry.slug)}: plug or switch required")
    if kinds == HARDWARE_KINDS and entry.kind != "switch" and entry.device.device_type.name != "Dimmer":
        raise Forbidden(f"{views.clean(entry.slug)}: wall switch or dimmer required")
    return entry


async def write(registry, caller, device, method, params=None, *, confirm=None, kinds=KINDS):
    entry = target(registry, device, kinds)
    result = await caller.call(entry, method, params, confirm=confirm)
    if isinstance(result, dict) and result.get("conflict_id"):
        raise RegistryError(f"{views.clean(entry.slug)}: rule conflicts with {views.clean(str(result['conflict_id']))}")
    await registry.refresh(entry, force=True)
    if not entry.online:
        raise DeviceUnavailable(f"{views.clean(entry.slug)}: {method} completed but refresh failed ({views.clean(entry.error or 'unavailable')})")
    out = {"device": views.clean(entry.slug), "changed": method}
    safe = compact(result)
    if safe:
        out["result"] = safe
    return out


def rule_name(kind):
    if kind not in RULE_NAMES:
        raise ValueError("rule kind must be countdown, schedule or away")
    return RULE_NAMES[kind]


async def read_rules(registry, caller, device, kind, start_index=0):
    entry = target(registry, device)
    name = rule_name(kind)
    integer(start_index, 0, 65535, "start_index")
    method = f"get_{name}_rules"
    result = response_object(entry, method, await caller.call(entry, method, {"start_index": start_index}))
    rule_list(entry, method, result)
    return {"device": views.clean(entry.slug), "kind": kind, **compact(result)}


async def save_rule(registry, caller, device, kind, payload, identifier=None):
    name = rule_name(kind)
    if identifier is not None:
        payload = {**payload, "id": rule_id(identifier)}
    return await write(registry, caller, device, f"{'edit' if identifier is not None else 'add'}_{name}_rule", payload)


async def countdown(registry, caller, device, seconds, on, enabled=True, identifier=None):
    payload = {"delay": integer(seconds, 1, 86400, "seconds"), "enable": boolean(enabled),
               "desired_states": {"device_on": boolean(on, "on")}}
    return await save_rule(registry, caller, device, "countdown", payload, identifier)


async def schedule(registry, caller, device, at, on, days, on_date=None, enabled=True, identifier=None):
    payload = {**calendar(days, on_date), "s_min": minute(at), "s_type": 1, "e_type": 0,
               "enable": boolean(enabled), "desired_states": {"device_on": boolean(on, "on")}}
    return await save_rule(registry, caller, device, "schedule", payload, identifier)


async def away(registry, caller, device, start, end, days, on_date=None, enabled=True, identifier=None):
    first, last = minute(start), minute(end)
    if first == last:
        raise ValueError("Away Mode needs a nonzero window")
    # UNVERIFIED: frequency=5 is a conservative candidate; the app's allowed range
    # and unit are not established. No frequency knob is exposed to an agent.
    payload = {**calendar(days, on_date), "start_min": first, "end_min": last,
               "start_time_type": 1, "end_time_type": 1, "start_time_offset": 0,
               "end_time_offset": 0, "frequency": 5, "enable": boolean(enabled)}
    return await save_rule(registry, caller, device, "away", payload, identifier)


async def delete_rule(registry, caller, device, kind, identifier):
    name = rule_name(kind)
    return await write(registry, caller, device, f"remove_{name}_rules",
                       {"remove_all": False, "rule_list": [{"id": rule_id(identifier)}]})


async def enable_rule(registry, caller, device, kind, enabled, identifier=None):
    name = rule_name(kind)
    boolean(enabled)
    if identifier is None:
        if kind == "countdown":
            raise ValueError("countdown enable needs a rule id")
        return await write(registry, caller, device, f"set_{name}_all_enable", {"enable": enabled})
    rule_id(identifier)
    entry = target(registry, device)
    index = 0
    for _ in range(64):
        method = f"get_{name}_rules"
        raw = response_object(entry, method, await caller.call(entry, method, {"start_index": index}))
        rules = rule_list(entry, method, raw)
        for rule in rules:
            if rule.get("id") == identifier:
                # Preserve the known rule fields when changing its enable flag.
                payload = compact(rule, RULE_FIELDS)
                payload["id"] = identifier
                payload["enable"] = enabled
                return await save_rule(registry, caller, device, kind, payload, identifier)
        index += len(rules)
        if not rules or index >= raw.get("sum", index):
            break
    raise RegistryError(f"{views.clean(entry.slug)}: {kind} rule {identifier} was not found in the returned pages")


Device = Annotated[str, Field(description="Plug, strip socket or switch id or name. Select the child socket itself.")]
Days = Annotated[list[Annotated[int, Field(strict=True)]], Field(description="Unique weekdays: Sunday=0 through Saturday=6. Empty for a dated rule.")]
Identifier = Annotated[str | None, Field(description="Existing rule id to edit. Omit to add a new rule.")]


def tools(ctx):
    async def run(fn, device, *args, **kwargs):
        await ctx.ready_for([device])
        with ctx.translated():
            return await fn(ctx.registry, ctx.caller, device, *args, **kwargs)

    @ctx.tool(title="Plug and switch rules")
    async def plug_rules(device: Device, kind: Annotated[RuleKind, Field(description="countdown, schedule or away")],
                         start_index: Annotated[int, Field(description="First rule index for this page", strict=True)] = 0) -> dict[str, Any]:
        """List one page of on-device rules, including their ids. Works on individual strip sockets and switches."""
        return await run(read_rules, device, kind, start_index)

    @ctx.tool(title="Plug countdown", tier=Tier.SETTING)
    async def plug_countdown_set(device: Device, seconds: Annotated[int, Field(description="Delay in seconds, 1 to 86400", strict=True)],
                                 on: Annotated[bool, Field(description="Power state when the timer expires", strict=True)],
                                 enabled: Annotated[bool, Field(description="Whether the rule runs", strict=True)] = True,
                                 rule_id: Identifier = None) -> dict[str, Any]:
        """Add or replace a countdown timer. Can energize a load while unattended. UNVERIFIED on SMART hardware."""
        return await run(countdown, device, seconds, on, enabled, rule_id)

    @ctx.tool(title="Plug schedule", tier=Tier.SETTING)
    async def plug_schedule_set(device: Device, at: Annotated[str, Field(description="Device-local time HH:MM")],
                                on: Annotated[bool, Field(description="Power state at that time", strict=True)], days: Days,
                                on_date: Annotated[str | None, Field(description="YYYY-MM-DD for a one-off rule")] = None,
                                enabled: Annotated[bool, Field(description="Whether the rule runs", strict=True)] = True,
                                rule_id: Identifier = None) -> dict[str, Any]:
        """Add or replace a fixed-time schedule. Use weekdays or a date. UNVERIFIED numeric rule enums."""
        return await run(schedule, device, at, on, days, on_date, enabled, rule_id)

    @ctx.tool(title="Plug Away Mode", tier=Tier.SETTING)
    async def plug_away_set(device: Device, start: Annotated[str, Field(description="Window start HH:MM, device-local")],
                            end: Annotated[str, Field(description="Window end HH:MM; may cross midnight")], days: Days,
                            on_date: Annotated[str | None, Field(description="YYYY-MM-DD for a one-off window")] = None,
                            enabled: Annotated[bool, Field(description="Whether the rule runs", strict=True)] = True,
                            rule_id: Identifier = None) -> dict[str, Any]:
        """Add or replace an Away Mode window with random power changes. UNVERIFIED enums and frequency."""
        return await run(away, device, start, end, days, on_date, enabled, rule_id)

    @ctx.tool(title="Delete plug rule", tier=Tier.SETTING)
    async def plug_rule_delete(device: Device, kind: Annotated[RuleKind, Field(description="Rule family")],
                               rule_id: Annotated[str, Field(description="One existing rule id to delete")]) -> dict[str, Any]:
        """Delete one on-device timer, schedule or Away Mode rule. Other rules are preserved."""
        return await run(delete_rule, device, kind, rule_id)

    @ctx.tool(title="Enable plug rule", tier=Tier.SETTING)
    async def plug_rule_enable(device: Device, kind: Annotated[RuleKind, Field(description="Rule family")],
                               enabled: Annotated[bool, Field(description="True to enable, false to disable", strict=True)],
                               rule_id: Annotated[str | None, Field(description="Rule id; omit for all schedule or away rules")] = None) -> dict[str, Any]:
        """Enable or disable one rule. Omit the id to change all schedules or all Away Mode rules."""
        return await run(enable_rule, device, kind, enabled, rule_id)

    @ctx.tool(title="Plug power-loss default")
    async def plug_default_state(device: Device) -> dict[str, Any]:
        """Read the socket's power-loss default and supported choices."""
        return await run(default_state, device)

    @ctx.tool(title="Set plug power-loss default", tier=Tier.SETTING)
    async def plug_default_state_set(device: Device,
                                     mode: Annotated[Literal["last_states", "always_on", "always_off", "toggle_states"],
                                                     Field(description="State when power returns. Always-on can energize a heater.")]) -> dict[str, Any]:
        """Set one socket's power-loss behavior. Always-on can energize a heater. UNVERIFIED legacy and v2 write shapes."""
        return await run(set_default_state, device, mode)

    @ctx.tool(title="Plug energy totals and costs")
    async def plug_energy_usage(device: Device) -> dict[str, Any]:
        """Read energy totals, runtime, generation and cost figures. Preserve the device's units and cost structure."""
        return await run(read, device, "get_energy_usage", ENERGY_FIELDS)

    @ctx.tool(title="Plug energy or power history")
    async def plug_energy_history(device: Device,
                                   series: Annotated[Literal["energy", "power"], Field(description="Energy consumption or average power history")],
                                   start_timestamp: Annotated[int, Field(description="Range start, UTC seconds", strict=True)],
                                   end_timestamp: Annotated[int, Field(description="Range end, UTC seconds", strict=True)],
                                   interval: Annotated[int, Field(description="Minutes: energy 60/1440/43200; power 5/60", strict=True)],
                                   precision: Annotated[Literal["wh", "mwh"], Field(description="Energy precision; leave wh for power")] = "wh") -> dict[str, Any]:
        """Read one bounded history page, including its timestamps. Request the next range explicitly. At most 512 intervals."""
        return await run(energy_history, device, series, start_timestamp, end_timestamp, interval, precision)

    @ctx.tool(title="Plug electricity price")
    async def plug_electricity_price(device: Device) -> dict[str, Any]:
        """Read constant or seasonal time-of-use price configuration. Currency and price scaling are not established."""
        return await run(read, device, "get_electricity_price_config", PRICE_FIELDS)

    @ctx.tool(title="Set plug electricity price", tier=Tier.SETTING)
    async def plug_electricity_price_set(device: Device,
                                         constant_price: Annotated[int, Field(description="Nonnegative integer in the device's native price scale", strict=True)]) -> dict[str, Any]:
        """Set a constant price. Replaces time-of-use mode. UNVERIFIED price scaling; do not interpret as a currency amount."""
        return await run(set_price, device, constant_price)

    @ctx.tool(title="Erase plug energy statistics", tier=Tier.SECURITY)
    async def plug_energy_clear(device: Device,
                                confirm: Annotated[str | None, Field(description="Exact phrase: clear_energy_data <device id>")] = None) -> dict[str, Any]:
        """Permanently erase stored energy statistics. Requires the confirmation phrase naming this device."""
        return await run(write, device, "clear_energy_data", confirm=confirm)

    @ctx.tool(title="Plug auto-off configuration")
    async def plug_auto_off(device: Device) -> dict[str, Any]:
        """Read automatic shut-off settings. Use plug_protection state for the running timer's remaining seconds."""
        return await run(read, device, "get_auto_off_config", AUTO_FIELDS)

    @ctx.tool(title="Cancel plug auto-off", tier=Tier.SECURITY)
    async def plug_auto_off_cancel(device: Device,
                                   confirm: Annotated[str | None, Field(description="Exact phrase: cancel_auto_off <device id>")] = None) -> dict[str, Any]:
        """Cancel a running automatic shut-off. The load may stay on longer. Requires confirmation."""
        return await run(write, device, "cancel_auto_off", confirm=confirm)

    @ctx.tool(title="Plug protection polling read")
    async def plug_protection(device: Device,
                               section: Annotated[Literal["state", "charge_guard", "power", "max_power"], Field(description="Read current status or one protection configuration")] = "state") -> dict[str, Any]:
        """Poll overcurrent, overheat, protection trips and charging state, or read Charge Guard. No cloud push subscription."""
        return await run(protection, device, section)

    @ctx.tool(title="Rename plug or select icon", tier=Tier.SETTING)
    async def plug_identity_set(device: Device,
                                name: Annotated[str | None, Field(description="New flat name, 1 to 64 characters")] = None,
                                icon: Annotated[str | None, Field(description="Device icon identifier; letters, digits, hyphen or underscore")] = None) -> dict[str, Any]:
        """Rename one plug, switch or socket, or change its icon. Reports changed fields without echoing identity data."""
        return await run(identity, device, name, icon)

    @ctx.tool(title="Set plug home location", tier=Tier.SETTING)
    async def plug_location_set(device: Device,
                                latitude: Annotated[float, Field(description="Latitude in degrees, -90 to 90", strict=True)],
                                longitude: Annotated[float, Field(description="Longitude in degrees, -180 to 180", strict=True)]) -> dict[str, Any]:
        """Store a location on one plug, switch or socket. Does not return coordinates. Region and clock are left to their own tools."""
        return await run(location, device, latitude, longitude)

    @ctx.tool(title="Plug runtime counters")
    async def plug_runtime(device: Device) -> dict[str, Any]:
        """Read minutes the load was on today and over the past 7 and 30 days."""
        return await run(read, device, "get_device_usage", RUNTIME_FIELDS)

    @ctx.tool(title="Switch and dimmer hardware settings")
    async def switch_settings(device: Device,
                              section: Annotated[HardwareSection, Field(description="state, delay, calibration, buttons, double_click, sleep or led")] = "state") -> dict[str, Any]:
        """Read one hardware setting on a wall switch or dimmer. UNVERIFIED calibration, action, sleep and wiring response shapes."""
        return await run(hardware, device, section)

    @ctx.tool(title="Switch delayed shut-off", tier=Tier.SETTING)
    async def switch_delay_off_set(device: Device,
                                    enabled: Annotated[bool, Field(description="Enable delayed shut-off", strict=True)],
                                    seconds: Annotated[int, Field(description="Delay from 1 to 86400 seconds", strict=True)]) -> dict[str, Any]:
        """Configure delayed shut-off on a switch or dimmer. UNVERIFIED v1 payload and time unit; does not set a v2 trigger."""
        return await run(delay_off, device, enabled, seconds)

    @ctx.tool(title="Switch double-click recognition", tier=Tier.SETTING)
    async def switch_double_click_set(device: Device,
                                       enabled: Annotated[bool, Field(description="Enable double-click recognition", strict=True)],
                                       interval_ms: Annotated[int, Field(description="Recognition interval, candidate milliseconds, 100 to 2000", strict=True)] = 500) -> dict[str, Any]:
        """Configure double-click recognition on a switch or dimmer. UNVERIFIED unit and limits. Does not change button actions."""
        return await run(double_click, device, enabled, interval_ms)

    @ctx.tool(title="Switch dimming calibration thresholds", tier=Tier.SETTING)
    async def switch_calibration_set(device: Device,
                                      minimum: Annotated[int, Field(description="Candidate minimum brightness percent, 1 to 100", strict=True)],
                                      maximum: Annotated[int, Field(description="Candidate maximum brightness percent, 1 to 100", strict=True)],
                                      curve_type: Annotated[int | None, Field(description="Model-specific curve id, 0 to 255; omit to preserve", strict=True)] = None) -> dict[str, Any]:
        """Set dimming calibration limits on a switch or dimmer. UNVERIFIED threshold units and curve ids. May change lamp behavior."""
        return await run(calibration, device, minimum, maximum, curve_type)

    @ctx.tool(title="Switch low-power mode", tier=Tier.SETTING)
    async def switch_low_power_set(device: Device,
                                    enabled: Annotated[bool, Field(description="True for low-power mode, false for normal mode", strict=True)]) -> dict[str, Any]:
        """Set low-power mode on hardware that reports this setting. Uses low_power_mode 1 or 0."""
        return await run(low_power, device, enabled)

    @ctx.tool(title="Plug LED setting")
    async def plug_led(device: Device) -> dict[str, Any]:
        """Read the indicator LED and its night-mode window on a plug, socket or switch."""
        return await run(read, device, "get_led_info", LED_FIELDS)

    @ctx.tool(title="Plug or switch LED night window", tier=Tier.SETTING)
    async def plug_led_set(device: Device,
                           mode: Annotated[Literal["always", "never", "night_mode"], Field(description="LED mode")],
                           start: Annotated[str | None, Field(description="Night window start HH:MM, device-local")] = None,
                           end: Annotated[str | None, Field(description="Night window end HH:MM, may cross midnight")] = None) -> dict[str, Any]:
        """Set the LED mode or a fixed night window. Also accepts dimmers. UNVERIFIED fixed-window time enum."""
        return await run(led_set, device, mode, start, end)
