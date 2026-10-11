"""Local doorbell observations and chime controls. All hardware behavior is UNVERIFIED."""

import re
from typing import Annotated, Any, Literal

from pydantic import Field
from kasa.protocols.smartcamprotocol import SmartCamProtocol

from .. import policy
from ..policy import Tier
from ..registry import BadRequest, DeviceUnavailable, Forbidden, RegistryError
from ..views import clean

METHODS = [
    policy.method("getLastAlarmInfo", Tier.READ, ["doorbell"], "Read the last doorbell alarm observation."),
    policy.method("getCallStateInfo", Tier.READ, ["doorbell"], "Read the doorbell call state without answering it."),
    policy.method("get_trigger_logs", Tier.READ, ["doorbell"], "Read hub trigger observations through the doorbell child path."),
]

METHODS += [
    policy.method("get_chime_alarm_configure", Tier.READ, ["chime", "hub"], "Read chime settings for a paired doorbell."),
    policy.method("get_support_alarm_type_list", Tier.READ, ["chime", "hub"], "Read the available chime tones."),
    policy.method("set_chime_alarm_configure", Tier.SETTING, ["chime", "hub"], "Change chime tone, volume and duration for a doorbell."),
    policy.method("play_alarm", Tier.SECURITY, ["chime", "hub"], "Play an audible chime or hub sound."),
    policy.method("stop_alarm", Tier.SECURITY, ["chime", "hub"], "Stop a chime or hub sound."),
]

Volume = Literal["mute", "low", "normal", "high"]


def _choice(value, choices, name):
    if value not in choices:
        raise BadRequest(f"Unsupported {name}")
    return value


def _boolean(value, name):
    if type(value) is not bool:
        raise BadRequest(f"{name} must be a boolean")
    return value


def _mac(entry):
    # Read the owner's already authenticated device metadata. Never expose this address.
    value = getattr(entry.device, "mac", None)
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}", value):
        raise BadRequest(f"{clean(entry.slug)} has no usable cached pairing address")
    # UNVERIFIED: chime firmware must be checked for colon versus hyphen address formatting.
    return value


async def _write(registry, caller, entry, method, params=None, confirm=None, changed=None):
    await _call(caller, entry, method, params, confirm)
    await registry.refresh(entry, force=True)
    return {"device": clean(entry.slug), "changed": changed or method, "unverified": True}


async def chime_get(registry, caller, device, doorbell):
    entry = _entry(registry, device, ["chime", "hub"])
    bell = _entry(registry, doorbell, ["doorbell"])
    params = {"mac": _mac(bell)}
    if _smartcam_hub(entry):
        params = {"chime": params}
    value = await _call(caller, entry, "get_chime_alarm_configure", params)
    config = _compact(_section(value, "chime", "config"), ("func_type", "type", "volume", "duration", "on_off"))
    tones = await _available_tones(caller, entry)
    out = {"device": clean(entry.slug), "doorbell": clean(bell.slug), "unverified": True}
    if config:
        out["settings"] = config
    if tones:
        out["tones"] = tones
    return out


def _tone_list(value, field):
    values = value.get(field, []) if isinstance(value, dict) else value
    return [clean(tone) for tone in values[:50] if isinstance(tone, str) and clean(tone)] if isinstance(values, list) else []


async def _check_tone(caller, entry, tone):
    _text(tone, "tone")
    if tone not in await _available_tones(caller, entry):
        raise BadRequest("The chime does not advertise this tone")


async def chime_set(registry, caller, device, doorbell, tone, volume, duration, enabled):
    _choice(volume, ("mute", "low", "normal", "high"), "volume")
    _integer(duration, 1, 600, "duration")
    _boolean(enabled, "enabled")
    _text(tone, "tone")
    entry = _entry(registry, device, ["chime", "hub"])
    bell = _entry(registry, doorbell, ["doorbell"])
    mac = _mac(bell)
    await _check_tone(caller, entry, tone)
    # UNVERIFIED: the audit/model confirms these fields; per-model duration limits need fixtures.
    params = {"mac": mac, "type": tone, "volume": volume, "duration": duration, "on_off": int(enabled)}
    return await _write(registry, caller, entry, "set_chime_alarm_configure", {"chime": params} if _smartcam_hub(entry) else params, changed={k: v for k, v in params.items() if k != "mac"})


async def chime_sound_action(registry, caller, device, action, tone=None, volume="normal", duration=1, confirm=None):
    _choice(action, ("play", "stop"), "action")
    entry = _entry(registry, device, ["chime", "hub"])
    if _smartcam_hub(entry):
        raise BadRequest("This hub plays its siren through hub_siren")
    if action == "stop":
        return await _write(registry, caller, entry, "stop_alarm", confirm=confirm, changed="sound stopped")
    _choice(volume, ("mute", "low", "normal", "high"), "volume")
    _integer(duration, 1, 600, "duration")
    await _check_tone(caller, entry, tone)
    params = {"alarm_type": tone, "alarm_volume": volume, "alarm_duration": duration}
    return await _write(registry, caller, entry, "play_alarm", params, confirm, "sound played")

# These module/section names come from the local protocol catalog and request wrappers.
# UNVERIFIED: firmware versions use different wrappers and the H200 may not forward all of them.
READS = {
    "ring": [("getRingStatus", "ring", "status", ("enabled", "status")), ("getRingSoundConfig", "ring", "config", ("ring_type", "volume")), ("getRingTypeList", "ring", "ring_type_list", ()), ("getRingPlay", "ring", "play", ("status", "ring_type", "time_left"))],
    "hub_ring": [("getHubRingStatus", "hub_ring", "config", ("enabled", "status")), ("getHubRingSoundConfig", "hub_ring", "config", ("ring_type", "volume")), ("getHubRingTypeList", "hub_ring", "ring_type_list", ()), ("getHubRingPlay", "hub_ring", "play", ("status", "ring_type", "time_left"))],
    "hub_plan": [("getHubRingPlan", "hub_ring_plan", "chn1_hub_ring_plan", ("enabled", "ring_plan_1"))],
    "chime_plan": [("getChimeRingPlan", "chime_ring_plan", "chn1_chime_ring_plan", ("enabled", "ring_plan_1"))],
    "existing_chime": [("getExistingChimeCfg", "existing_chime", "config", ("type", "ring_length"))],
    "auto_response": [("getSmartRespStatus", "quick_response", "smart_resp_status", ("enabled",)), ("getSmartRespConfig", "quick_response", "smart_resp_config", ("id", "play_interval"))],
    "anti_theft": [("getAntiTheftEnable", "anti_theft", "config", ("enabled",)), ("getAntiTheftStatus", "anti_theft", "status", ("status",))],
    "wake_up": [("getWakeUpConfig", "wake_up", "config", ("wake_up_type",))],
    "battery_mode": [("getBatteryOperatingMode", "battery", "operating", ("mode", "follow_config"))],
    "button_led": [("getLedColor", "led", "color", ("name",))],
}
Setting = Literal["ring", "hub_ring", "hub_plan", "chime_plan", "existing_chime", "quick_replies", "auto_response", "anti_theft", "wake_up", "battery_mode", "button_led"]
RingTarget = Literal["doorbell", "hub"]
BatteryMode = Literal["monitor_performance_first", "battery_life_first", "super_power_saving"]
METHODS += [policy.method(method, Tier.READ, ["doorbell"], "Read a doorbell setting.") for specs in READS.values() for method, _, _, _ in specs]
METHODS += [
    policy.method("getQuickRespList", Tier.READ, ["doorbell"], "List stored doorbell voice replies."),
    *[policy.method(method, Tier.SETTING, ["doorbell"], summary) for method, summary in [
        ("setRingSoundConfig", "Change the doorbell ring tone and volume."),
        ("setHubRingSoundConfig", "Change the connected hub ring tone and volume."),
        ("setRingStatus", "Change whether the doorbell rings on a press."),
        ("setHubRingStatus", "Change whether the connected hub rings on a press."),
        ("setHubRingPlan", "Change the connected hub ring schedule."),
        ("setChimeRingPlan", "Change the paired chime ring schedule."),
        ("setSmartRespConfig", "Change the automatic voice reply and delay."),
        ("setSmartRespStatus", "Enable or disable automatic voice replies."),
        ("setWakeUpConfig", "Change the battery doorbell wake-up trigger."),
        ("setBatteryOperatingMode", "Change the doorbell battery operating mode."),
        ("setLedColor", "Change the doorbell button LED color."),
    ]],
    policy.method("setPreWakeup", Tier.SETTING, ["hub"], "Wake a battery doorbell child in advance through its hub."),
    *[policy.method(method, Tier.SECURITY, ["doorbell"], summary) for method, summary in [
        ("setRingPlay", "Preview or stop an audible doorbell ring."),
        ("setHubRingPlay", "Preview or stop an audible connected hub ring."),
        ("setExistingChimeCfg", "Change the electrical configuration of an in-wall chime."),
        # Anti-theft is an alarm with an arm state. Disarming lowers a protection; arming blocks a factory reset
        # by button. Both change what a person can do at the door, so it is level 3.
        ("setAntiTheftEnable", "Arm or disarm the anti-theft alarm. Armed, it blocks a factory reset by button."),
        ("playQuickResp", "Play an audible voice reply to a visitor."),
        ("stopQuickResp", "Stop a doorbell voice reply."),
    ]],
]


async def read_setting(registry, caller, device, setting, legacy=False):
    _choice(setting, (*READS, "quick_replies"), "setting")
    _boolean(legacy, "legacy")
    if setting == "quick_replies":
        return {**await quick_replies(registry, caller, device), "setting": setting}
    entry = _entry(registry, device, ["doorbell"])
    data = {}
    for method, module, section, fields in READS[setting]:
        # The camera fixtures read sections as a list: {"system": {"name": ["last_alarm_info"]}}.
        params = {module: {"name": [section]}}
        if module == "hub_ring" or legacy and setting in ("ring", "anti_theft", "wake_up", "hub_plan"):
            params = {module: {}}
        value = _section(await _call(caller, entry, method, params), module, section)
        if method.endswith("TypeList"):
            item = _tone_list(value, "ring_type_list")
        else:
            item = _compact(value, fields)
        if item:
            data[method] = item
    out = {"device": clean(entry.slug), "setting": setting, "unverified": True}
    if data:
        out["values"] = data
    return out


async def ring_configure(registry, caller, device, target, tone, volume):
    _choice(target, ("doorbell", "hub"), "ring target")
    _text(tone, "tone")
    # UNVERIFIED: older devices use 0/1/2; newer hub firmware has more volume gears.
    _integer(volume, 0, 100, "volume")
    entry = _entry(registry, device, ["doorbell"])
    module, method = ("ring", "setRingSoundConfig") if target == "doorbell" else ("hub_ring", "setHubRingSoundConfig")
    config = {"ring_type": tone, "volume": str(volume)}
    return await _write(registry, caller, entry, method, {module: {"config": config}}, changed=config)


async def ring_enable(registry, caller, device, target, enabled, legacy=False):
    _choice(target, ("doorbell", "hub"), "ring target")
    _boolean(enabled, "enabled")
    _boolean(legacy, "legacy")
    entry = _entry(registry, device, ["doorbell"])
    module, method = ("ring", "setRingStatus") if target == "doorbell" else ("hub_ring", "setHubRingStatus")
    section = "status" if target == "doorbell" and not legacy else "config"
    config = {"enabled": "on" if enabled else "off"}
    return await _write(registry, caller, entry, method, {module: {section: config}}, changed=config)


async def ring_preview(registry, caller, device, target, action, tone, confirm=None, legacy=False):
    _choice(target, ("doorbell", "hub"), "ring target")
    _choice(action, ("play", "stop"), "action")
    _boolean(legacy, "legacy")
    if action == "play":
        _text(tone, "tone")
    entry = _entry(registry, device, ["doorbell"])
    module, method = ("ring", "setRingPlay") if target == "doorbell" else ("hub_ring", "setHubRingPlay")
    value = {"status": "on" if action == "play" else "off"}
    if action == "play":
        value["ring_type"] = tone
    params = {module: {"play": value}} if target == "doorbell" and not legacy else {module: value}
    return await _write(registry, caller, entry, method, params, confirm, f"{target} ring {action}")


async def ring_plan(registry, caller, device, target, enabled, schedule):
    _choice(target, ("hub", "chime"), "ring plan target")
    _boolean(enabled, "enabled")
    # UNVERIFIED: one observed default establishes the syntax, but weekday bit ordering needs hardware.
    _text(schedule, "schedule", 256)
    match = re.fullmatch(r"(\d{2})(\d{2})-(\d{2})(\d{2}),(\d{1,3})", schedule)
    if not match or any(int(match[i]) > maximum for i, maximum in ((1, 23), (2, 59), (3, 23), (4, 59), (5, 127))) or int(match[5]) == 0:
        raise BadRequest("schedule must be HHMM-HHMM,WEEKDAY_MASK with a mask from 1 to 127")
    entry = _entry(registry, device, ["doorbell"])
    module, method = ("hub_ring_plan", "setHubRingPlan") if target == "hub" else ("chime_ring_plan", "setChimeRingPlan")
    section = "chn1_hub_ring_plan" if target == "hub" else "chn1_chime_ring_plan"
    config = {"enabled": "on" if enabled else "off", "ring_plan_1": schedule}
    return await _write(registry, caller, entry, method, {module: {section: config}}, changed=config)


async def existing_chime(registry, caller, device, chime_type, ring_length, confirm=None):
    _choice(chime_type, ("mechanical", "digital"), "in-wall chime type")
    # UNVERIFIED: the electrical duration limit varies by wired doorbell/chime model. Zero is used in app setup.
    _integer(ring_length, 0, 10, "ring_length")
    entry = _entry(registry, device, ["doorbell"])
    config = {"type": chime_type, "ring_length": ring_length}
    return await _write(registry, caller, entry, "setExistingChimeCfg", {"existing_chime": {"config": config}}, confirm, config)


async def quick_replies(registry, caller, device):
    entry = _entry(registry, device, ["doorbell"])
    value = _section(await _call(caller, entry, "getQuickRespList", {"quick_response": {"table": ["quick_resp_audio"]}}), "quick_response", "quick_resp_audio")
    rows = value.get("quick_resp_audio", []) if isinstance(value, dict) else value
    replies = []
    if isinstance(rows, list):
        for row in rows[:50]:
            if not isinstance(row, dict):
                continue
            items = [row] if "id" in row else list(row.values())[:1]
            for item in items:
                reply = _compact(item, ("id", "index", "name", "duration", "read_only"))
                if reply:
                    replies.append(reply)
    out = {"device": clean(entry.slug), "unverified": True}
    if replies:
        out["replies"] = replies
    return out


async def quick_reply_action(registry, caller, device, action, reply_id=None, confirm=None):
    _choice(action, ("play", "stop"), "action")
    entry = _entry(registry, device, ["doorbell"])
    if action == "stop":
        return await _write(registry, caller, entry, "stopQuickResp", {"quick_response": {"stop_quick_resp_audio": {}}}, confirm, "voice reply stopped")
    _text(reply_id, "reply_id")
    replies = await quick_replies(registry, caller, device)
    if reply_id not in [reply.get("id") for reply in replies.get("replies", [])]:
        raise BadRequest("The doorbell does not list this voice reply")
    params = {"quick_response": {"play_quick_resp_audio": {"id": reply_id, "force": "0"}}}
    return await _write(registry, caller, entry, "playQuickResp", params, confirm, {"voice_reply_played": reply_id})


async def auto_response_configure(registry, caller, device, reply_id, delay):
    _text(reply_id, "reply_id")
    # UNVERIFIED: actual model interval range needs fixtures. This is a conservative API bound in seconds.
    _integer(delay, 0, 600, "delay")
    entry = _entry(registry, device, ["doorbell"])
    replies = await quick_replies(registry, caller, device)
    if reply_id not in [reply.get("id") for reply in replies.get("replies", [])]:
        raise BadRequest("The doorbell does not list this voice reply")
    config = {"id": reply_id, "play_interval": str(delay)}
    return await _write(registry, caller, entry, "setSmartRespConfig", {"quick_response": {"smart_resp_config": config}}, changed=config)


async def auto_response(registry, caller, device, enabled=None, reply_id=None, delay=None):
    """One write per call: switch auto response on or off, or choose its reply and delay. Never both."""
    if enabled is None:
        if reply_id is None or delay is None:
            raise BadRequest("Give enabled alone, or reply_id with delay")
        return await auto_response_configure(registry, caller, device, reply_id, delay)
    _boolean(enabled, "enabled")
    if reply_id is not None or delay is not None:
        raise BadRequest("Give enabled alone, or reply_id with delay. One change per call")
    entry = _entry(registry, device, ["doorbell"])
    config = {"enabled": "on" if enabled else "off"}
    return await _write(registry, caller, entry, "setSmartRespStatus", {"quick_response": {"smart_resp_status": config}}, changed={"auto_response": enabled})


async def anti_theft(registry, caller, device, enabled, confirm=None):
    _boolean(enabled, "enabled")
    entry = _entry(registry, device, ["doorbell"])
    config = {"enabled": "on" if enabled else "off"}
    return await _write(registry, caller, entry, "setAntiTheftEnable", {"anti_theft": {"config": config}}, confirm, {"anti_theft": enabled})


async def wake_up(registry, caller, device, trigger):
    _choice(trigger, ("detection", "doorbell"), "wake-up trigger")
    entry = _entry(registry, device, ["doorbell"])
    config = {"wake_up_type": trigger}
    return await _write(registry, caller, entry, "setWakeUpConfig", {"wake_up": {"config": config}}, changed=config)


async def pre_wake(registry, caller, device):
    entry = _entry(registry, device, ["doorbell"])
    if not entry.root or entry.root.kind != "hub":
        raise BadRequest("Pre-wake needs a doorbell connected through a hub")
    # UNVERIFIED: the app sends this child list to a hub; verify it wakes a D230 through H200.
    params = {"preWakeUp": {"child_device_list": [{"device_id": entry.device.device_id}]}}
    return await _write(registry, caller, entry.root, "setPreWakeup", params, changed={"pre_wake_requested": clean(entry.slug)})


async def battery_mode(registry, caller, device, mode, follow_config):
    _choice(mode, ("monitor_performance_first", "battery_life_first", "super_power_saving"), "battery mode")
    _boolean(follow_config, "follow_config")
    entry = _entry(registry, device, ["doorbell"])
    config = {"mode": mode, "follow_config": follow_config}
    return await _write(registry, caller, entry, "setBatteryOperatingMode", {"battery": {"operating": config}}, changed=config)


async def button_color(registry, caller, device, color):
    _text(color, "color", 32)
    entry = _entry(registry, device, ["doorbell"])
    # UNVERIFIED: the name wrapper is known, but available color names vary by model.
    return await _write(registry, caller, entry, "setLedColor", {"led": {"color": {"name": color}}}, changed={"button_led_color": color})

# The H200 lists its tones with getSirenTypeList (same level as the hub area). Its siren itself is the hub area's
# job (hub_alarm_config, hub_alarm_set, hub_siren), so this area does not send setSirenConfig or setSirenStatus.
METHODS += [
    policy.method("getSirenTypeList", Tier.READ, ["hub"], "Read the hub's available ring and alarm tones."),
]


def _smartcam_hub(entry):
    return entry.kind == "hub" and isinstance(getattr(entry.device, "protocol", None), SmartCamProtocol)


async def _available_tones(caller, entry):
    if _smartcam_hub(entry):
        result = await _call(caller, entry, "getSirenTypeList", {"siren": {}})
        return _tone_list(result, "siren_type_list")
    result = await _call(caller, entry, "get_support_alarm_type_list")
    return _tone_list(result, "alarm_type_list")


Device = Annotated[str, Field(description="Device id or name")]
Confirm = Annotated[str | None, Field(description="Exact method and device id confirmation phrase")]


def _entry(registry, ref, kinds):
    entry = registry.resolve_one(ref)
    if entry.kind not in kinds:
        raise BadRequest(f"{clean(entry.slug)} is not a {' or '.join(kinds)}")
    return entry


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise BadRequest(f"{name} must be an integer from {low} to {high}")
    return value


def _text(value, name, limit=64):
    if not isinstance(value, str) or not value or clean(value, limit) != value:
        raise BadRequest(f"{name} must be nonempty plain text of at most {limit} characters")
    return value


def _section(value, module, section):
    if not isinstance(value, dict):
        return {}
    value = value.get(module, value)
    return value.get(section, value) if isinstance(value, dict) else {}


def _compact(value, fields):
    """Only named scalar fields cross the model boundary. Unknown nested data is discarded."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for field in fields:
        item = value.get(field)
        if isinstance(item, str):
            item = clean(item)
        if type(item) in (str, int, bool) and item != "":
            out[field] = item
    return out


async def _call(caller, entry, method, params=None, confirm=None):
    try:
        return await caller.call(entry, method, params, confirm=confirm)
    except DeviceUnavailable as exc:
        # Device error text can echo pairing keys or credentials. Never forward it.
        reason = "device rejected or could not complete the request"
        if "UNSUPPORTED_METHOD" in str(exc) or "-40106" in str(exc):
            reason = "device does not support this method"
        elif "did not answer in time" in str(exc):
            reason = "device did not answer in time"
        elif "rejected the account credentials" in str(exc):
            reason = "device rejected the account credentials"
        raise DeviceUnavailable(f"{clean(entry.slug)}: {method}: {reason}") from None


def _timestamp(value):
    try:
        return int(value) if not isinstance(value, bool) and int(value) >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


async def events_recent(registry, caller, device, since=0, limit=10, hub=None):
    """One bounded poll. Call state is a snapshot, never a synthesized ring event."""
    _integer(since, 0, 2**63 - 1, "since")
    _integer(limit, 1, 50, "limit")
    entry = _entry(registry, device, ["doorbell"])
    parent = _entry(registry, hub, ["hub"]) if hub else entry.root
    if parent and parent is not entry.root:
        raise BadRequest("The selected hub is not this doorbell's parent")
    requests = [
        # Request and reply shapes of getLastAlarmInfo match the C225 and C610 fixtures. The D230 has no fixture for it.
        (entry, "getLastAlarmInfo", {"system": {"name": ["last_alarm_info"]}}),
        # UNVERIFIED: the call_manager module and call_state_info section come from the app catalog, not a fixture.
        (entry, "getCallStateInfo", {"call_manager": {"name": ["call_state_info"]}}),
    ]
    if parent:
        # Sensor fixtures confirm this child-scoped request. Direct H200 requests are rejected (UNSUPPORTED_METHOD).
        # UNVERIFIED: the D230 fixture lists no trigger_log component, so this read may well fail on a D230.
        # A failure lands in "errors" and does not hide the other observations.
        requests.append((entry, "get_trigger_logs", {"start_id": 0, "page_size": limit}))
    events, errors, state = [], [], {}
    for target, method, params in requests:
        try:
            result = await _call(caller, target, method, params)
        except Forbidden:
            raise
        except RegistryError as exc:
            errors.append({"device": clean(target.slug), "method": method, "reason": clean(str(exc), 160)})
            continue
        if method == "getLastAlarmInfo":
            value = _compact(_section(result, "system", "last_alarm_info"), ("last_alarm_time", "last_alarm_type"))
            timestamp = _timestamp(value.get("last_alarm_time"))
            if timestamp is not None and timestamp > since:
                events.append({"source": "last_alarm", "timestamp": timestamp, **{k: v for k, v in value.items() if k != "last_alarm_time"}})
        elif method == "getCallStateInfo":
            state = _compact(_section(result, "call_manager", "call_state_info"), ("call_state", "video_call_camera_state"))
        elif isinstance(result, dict) and isinstance(result.get("logs"), list):
            for log in result["logs"][:limit]:
                value = _compact(log, ("id", "timestamp", "event"))
                timestamp = _timestamp(value.get("timestamp"))
                if timestamp is not None and timestamp > since:
                    events.append({**value, "source": "hub_trigger", "timestamp": timestamp})
    # UNVERIFIED: neither alarm types nor call states are known to prove a physical ring press.
    unique = {tuple(sorted(event.items())): event for event in events}
    events = sorted(unique.values(), key=lambda event: event["timestamp"])[:limit]
    out = {"device": clean(entry.slug), "unverified": True, "cursor": max([since, *(event["timestamp"] for event in events)])}
    if events:
        out["events"] = events
    if state:
        out["call_state"] = state
    if errors:
        out["errors"] = errors
    return out


def tools(ctx):
    @ctx.tool(title="Recent doorbell observations")
    async def doorbell_events_recent(
        device: Device,
        since: Annotated[int, Field(description="Only observations after this Unix timestamp", ge=0, strict=True)] = 0,
        limit: Annotated[int, Field(description="Maximum observations in one bounded poll", ge=1, le=50, strict=True)] = 10,
        hub: Annotated[str | None, Field(description="Optional hub id or name; defaults to the parent hub")] = None,
    ) -> dict[str, Any]:
        """Poll last alarm, call state and parent hub logs. UNVERIFIED: observations do not prove a ring press. Use cursor as since; equal-second events may be missed."""
        await ctx.ready_for([device, *([hub] if hub else [])])
        with ctx.translated():
            return await events_recent(ctx.registry, ctx.caller, device, since, limit, hub)

    @ctx.tool(title="Chime settings")
    async def chime_settings_get(device: Device, doorbell: Annotated[str, Field(description="Paired doorbell id or name")]) -> dict[str, Any]:
        """Read per-doorbell chime settings and available tones, including an H100 or H200 as chime. UNVERIFIED on hardware."""
        await ctx.ready_for([device, doorbell])
        with ctx.translated():
            return await chime_get(ctx.registry, ctx.caller, device, doorbell)

    @ctx.tool(title="Change chime settings", tier=Tier.SETTING)
    async def chime_settings_set(
        device: Device,
        doorbell: Annotated[str, Field(description="Paired doorbell id or name")],
        tone: Annotated[str, Field(description="Tone advertised by chime_settings_get", min_length=1, max_length=64)],
        volume: Annotated[Volume, Field(description="Chime sound volume")],
        duration: Annotated[int, Field(description="Ring duration in seconds", ge=1, le=600, strict=True)],
        enabled: Annotated[bool, Field(description="Whether this chime rings for the doorbell", strict=True)],
    ) -> dict[str, Any]:
        """Change chime tone, volume, duration and ring enable. Level 2. UNVERIFIED per-model duration limits."""
        await ctx.ready_for([device, doorbell])
        with ctx.translated():
            return await chime_set(ctx.registry, ctx.caller, device, doorbell, tone, volume, duration, enabled)

    @ctx.tool(title="Play or stop chime", tier=Tier.SECURITY)
    async def chime_sound(
        device: Device,
        action: Annotated[Literal["play", "stop"], Field(description="Play an audible sound or stop it")],
        tone: Annotated[str | None, Field(description="Advertised tone; required for play")] = None,
        volume: Annotated[Volume, Field(description="Sound volume")] = "normal",
        duration: Annotated[int, Field(description="Sound duration in seconds", ge=1, le=600, strict=True)] = 1,
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """Play or stop sound on a chime or an H100. An H200 uses hub_siren. Level 3. Confirm play_alarm DEVICE or stop_alarm DEVICE. UNVERIFIED on hardware."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await chime_sound_action(ctx.registry, ctx.caller, device, action, tone, volume, duration, confirm)

    @ctx.tool(title="Doorbell settings")
    async def doorbell_settings_get(
        device: Device,
        setting: Annotated[Setting, Field(description="Which doorbell setting to read")],
        legacy: Annotated[bool, Field(description="Use older read wrappers for ring, anti-theft and wake-up", strict=True)] = False,
    ) -> dict[str, Any]:
        """Read ring, hub/chime plan, in-wall chime, stored voice replies, auto response, anti-theft, wake-up, battery mode or button LED. UNVERIFIED on hardware."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await read_setting(ctx.registry, ctx.caller, device, setting, legacy)

    @ctx.tool(title="Change ring sound", tier=Tier.SETTING)
    async def doorbell_ring_sound_set(
        device: Device,
        target: Annotated[RingTarget, Field(description="Sound at the doorbell or its connected hub")],
        tone: Annotated[str, Field(description="Ring type returned by doorbell_settings_get")],
        volume: Annotated[int, Field(description="Device volume gear. UNVERIFIED range varies by model", ge=0, le=100, strict=True)],
    ) -> dict[str, Any]:
        """Set ring tone and volume at level 2. Use the model's supported volume gear. UNVERIFIED on hardware."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await ring_configure(ctx.registry, ctx.caller, device, target, tone, volume)

    @ctx.tool(title="Enable doorbell or hub ring", tier=Tier.SETTING)
    async def doorbell_ring_enabled_set(
        device: Device,
        target: Annotated[RingTarget, Field(description="Ring at the doorbell or connected hub")],
        enabled: Annotated[bool, Field(description="Whether a press rings this target", strict=True)],
        legacy: Annotated[bool, Field(description="Use the older doorbell ring config wrapper", strict=True)] = False,
    ) -> dict[str, Any]:
        """Enable or disable the doorbell or hub ring at level 2. UNVERIFIED on hardware."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await ring_enable(ctx.registry, ctx.caller, device, target, enabled, legacy)

    @ctx.tool(title="Preview doorbell ring", tier=Tier.SECURITY)
    async def doorbell_ring_preview(
        device: Device,
        target: Annotated[RingTarget, Field(description="Preview at the doorbell or connected hub")],
        action: Annotated[Literal["play", "stop"], Field(description="Audible preview or stop")],
        tone: Annotated[str | None, Field(description="Ring type; required for play")] = None,
        confirm: Confirm = None,
        legacy: Annotated[bool, Field(description="Use the older doorbell preview wrapper", strict=True)] = False,
    ) -> dict[str, Any]:
        """Play or stop an audible ring at level 3. Confirm setRingPlay DEVICE or setHubRingPlay DEVICE. UNVERIFIED on hardware."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await ring_preview(ctx.registry, ctx.caller, device, target, action, tone, confirm, legacy)

    @ctx.tool(title="Change ring plan", tier=Tier.SETTING)
    async def doorbell_ring_plan_set(
        device: Device,
        target: Annotated[Literal["hub", "chime"], Field(description="Schedule the hub or paired chimes")],
        enabled: Annotated[bool, Field(description="Whether the plan is active", strict=True)],
        schedule: Annotated[str, Field(description="Single app-format window HHMM-HHMM,WEEKDAY_MASK; UNVERIFIED weekday bits")],
    ) -> dict[str, Any]:
        """Set one hub or chime ring window at level 2. UNVERIFIED weekday bits; preserve a schedule read from the device."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await ring_plan(ctx.registry, ctx.caller, device, target, enabled, schedule)

    @ctx.tool(title="Configure in-wall chime", tier=Tier.SECURITY)
    async def doorbell_existing_chime_set(
        device: Device,
        chime_type: Annotated[Literal["mechanical", "digital"], Field(description="Installed in-wall chime type")],
        ring_length: Annotated[int, Field(description="Electrical ring length. UNVERIFIED units and model limits", ge=0, le=10, strict=True)],
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """Change electrical chime configuration at level 3. Wrong values can strain its coil. Confirm setExistingChimeCfg DEVICE. UNVERIFIED duration limits."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await existing_chime(ctx.registry, ctx.caller, device, chime_type, ring_length, confirm)

    @ctx.tool(title="Play or stop quick reply", tier=Tier.SECURITY)
    async def doorbell_quick_reply(
        device: Device,
        action: Annotated[Literal["play", "stop"], Field(description="Audible voice reply or stop")],
        reply_id: Annotated[str | None, Field(description="Listed reply id; required for play")] = None,
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """Play or stop a visitor voice reply at level 3. Confirm playQuickResp DEVICE or stopQuickResp DEVICE. UNVERIFIED on hardware."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await quick_reply_action(ctx.registry, ctx.caller, device, action, reply_id, confirm)

    @ctx.tool(title="Configure auto response", tier=Tier.SETTING)
    async def doorbell_auto_response_set(
        device: Device,
        enabled: Annotated[bool | None, Field(description="Switch automatic replies on or off. Give it alone", strict=True)] = None,
        reply_id: Annotated[str | None, Field(description="Listed voice reply id. Give it with delay")] = None,
        delay: Annotated[int | None, Field(description="Response delay. UNVERIFIED seconds and per-model range", ge=0, le=600, strict=True)] = None,
    ) -> dict[str, Any]:
        """Switch automatic voice replies on or off, or choose the reply and its delay, at level 2. One change per call. UNVERIFIED on hardware."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await auto_response(ctx.registry, ctx.caller, device, enabled, reply_id, delay)

    @ctx.tool(title="Arm or disarm anti-theft", tier=Tier.SECURITY)
    async def doorbell_anti_theft_set(
        device: Device,
        enabled: Annotated[bool, Field(description="True arms the anti-theft alarm, false disarms it", strict=True)],
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """Arm or disarm the anti-theft alarm at level 3. Armed, it blocks a factory reset by button. Confirm setAntiTheftEnable DEVICE. UNVERIFIED on hardware."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await anti_theft(ctx.registry, ctx.caller, device, enabled, confirm)

    @ctx.tool(title="Change doorbell wake-up", tier=Tier.SETTING)
    async def doorbell_wake_up_set(
        device: Device,
        trigger: Annotated[Literal["detection", "doorbell"], Field(description="Wake for detection events or the ring button")],
    ) -> dict[str, Any]:
        """Change the battery doorbell wake-up trigger at level 2. UNVERIFIED on hardware."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await wake_up(ctx.registry, ctx.caller, device, trigger)

    @ctx.tool(title="Pre-wake doorbell", tier=Tier.SETTING)
    async def doorbell_pre_wake(device: Device) -> dict[str, Any]:
        """Request pre-wake of a battery doorbell through its parent hub at level 2. UNVERIFIED on H200/D230."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await pre_wake(ctx.registry, ctx.caller, device)

    @ctx.tool(title="Change battery operating mode", tier=Tier.SETTING)
    async def doorbell_battery_mode_set(
        device: Device,
        mode: Annotated[BatteryMode, Field(description="Favor performance, battery life or maximum power saving")],
        follow_config: Annotated[bool, Field(description="Use the existing per-mode configuration", strict=True)] = False,
    ) -> dict[str, Any]:
        """Change the doorbell battery operating mode at level 2. UNVERIFIED support and follow_config meaning on hardware."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await battery_mode(ctx.registry, ctx.caller, device, mode, follow_config)

    @ctx.tool(title="Change button LED color", tier=Tier.SETTING)
    async def doorbell_button_color_set(
        device: Device,
        color: Annotated[str, Field(description="Button color name supported by this doorbell", min_length=1, max_length=32)],
    ) -> dict[str, Any]:
        """Change the button LED color at level 2. UNVERIFIED available color names; unsupported names may be refused by the device."""
        await ctx.ready_for([device])
        with ctx.translated():
            return await button_color(ctx.registry, ctx.caller, device, color)
