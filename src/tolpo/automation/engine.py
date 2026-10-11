"""Local scheduler with guarded actions and durable delay cursors."""
from __future__ import annotations

import asyncio
import copy
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from tolpo import policy
from tolpo.registry import Forbidden
from tolpo.views import clean

from .events import RegistrySource
from .graph import check_rule_graph
from .models import Rule
from .store import Run, Store
from .sun import sun_time

log = logging.getLogger(__name__)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self):
        return datetime.now(timezone.utc)


class FakeClock:
    def __init__(self, now):
        self.value = now

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class ToolExecutor(Protocol):
    """Opt-in adapter. Plan must return level and allowed; execute must recheck policy.

    Security/lifecycle plans also return a per-call confirm phrase. No confirmations
    are persisted. Only a direct tick may supply one for an immediate action.
    """
    def plan(self, name: str, params: dict) -> dict: ...
    async def execute(self, name: str, params: dict, *, confirm: str | None) -> None: ...


def in_window(window, now):
    minute = now.strftime("%H:%M")
    if window.start <= window.end:
        return now.weekday() in window.weekdays and window.start <= minute < window.end
    # The early morning portion belongs to the weekday on which the window began.
    day = now.weekday() if minute >= window.start else (now.weekday() - 1) % 7
    return day in window.weekdays and (minute >= window.start or minute < window.end)


def matches(predicate, values):
    reading = values.get(predicate.device, {})
    if predicate.field not in reading or reading[predicate.field] is None:
        return False
    value, wanted = reading[predicate.field], predicate.value
    try:
        return {"eq": lambda: value == wanted, "ne": lambda: value != wanted,
                "gt": lambda: value > wanted, "ge": lambda: value >= wanted,
                "lt": lambda: value < wanted, "le": lambda: value <= wanted}[predicate.op]()
    except TypeError:
        return False


def public_action(action):
    result = {"kind": action.kind}
    for field in ("device", "method", "tool"):
        value = getattr(action, field)
        if value:
            result[field] = clean(value)
    if action.kind == "power":
        result["on"] = action.on
    elif action.kind == "light":
        result["light"] = action.light.model_dump(exclude_none=True)
    elif action.kind == "delay":
        result["seconds"] = action.seconds
    return result


def public_rule(rule):
    # Device method parameters and condition values are intentionally not echoed.
    result = {"id": rule.id, "name": clean(rule.name), "enabled": rule.enabled,
              "triggers": [t.kind for t in rule.triggers], "minimum_interval": rule.minimum_interval,
              "actions": [public_action(a) for a in rule.actions]}
    if rule.conditions:
        result["conditions"] = len(rule.conditions)
        result["condition_mode"] = rule.condition_mode
    return result


class Engine:
    def __init__(self, registry, caller, *, store=None, source=None, clock=None, tool_executor=None):
        self.registry, self.caller = registry, caller
        self.store = store or Store()
        self.state = self.store.load()
        self.source = source or RegistrySource(registry, caller)
        self.clock = clock or SystemClock()
        self.tool_executor = tool_executor
        self.previous = {}
        self.log_ids = {}
        self.holds = {}
        self.lock = asyncio.Lock()
        self.task = None
        self.closed = False
        self.halted = False  # set by the kill switch at once, before it waits for a running tick
        self.lock_warned = False

    def rule(self, ref):
        for rule in self.state.rules:
            if rule.id == ref:
                return rule
        raise ValueError("automation rule was not found")

    def save(self):
        self.store.save(self.state)

    def create(self, rule: Rule):
        if any(r.id == rule.id for r in self.state.rules):
            raise ValueError("automation rule id already exists")
        if len(self.state.rules) >= 200:
            raise ValueError("automation rule limit reached")
        check_rule_graph(self.state.rules + [rule])
        if any(t.kind in ("sunrise", "sunset") for t in rule.triggers) and self.state.settings.latitude is None:
            raise ValueError("sun trigger needs configured latitude and longitude")
        self.state.rules.append(rule)
        try:
            self.save()
        except Exception:
            self.state.rules.pop()
            raise
        if rule.enabled:
            self.start()
        return public_rule(rule)

    def enabled(self, ref, enabled):
        rule = self.rule(ref)
        rule.enabled = enabled
        if not enabled:
            self.state.runs = [run for run in self.state.runs if run.rule != ref]
            self.holds = {key: val for key, val in self.holds.items() if key[0] != ref}
        self.save()
        if enabled:
            self.start()
        return {"id": ref, "enabled": enabled}

    def delete(self, ref):
        self.rule(ref)
        self.state.runs = [run for run in self.state.runs if run.rule != ref]
        self.holds = {key: val for key, val in self.holds.items() if key[0] != ref}
        self.state.rules = [r for r in self.state.rules if r.id != ref]
        self.state.last_run.pop(ref, None)
        self.state.clock_marks = {key: value for key, value in self.state.clock_marks.items()
                                  if not key.startswith(ref + ":")}
        self.save()
        return {"deleted": ref}

    def start(self):
        if not self.closed and not self.state.settings.killed and any(r.enabled for r in self.state.rules):
            if self.task is None or self.task.done():
                # One engine per state directory. A second tolpo process would fire every action twice.
                if not self.store.lock():
                    if not self.lock_warned:
                        log.warning("another tolpo process runs the automations in this state directory; this one does not")
                        self.lock_warned = True
                    return
                self.task = asyncio.create_task(self._loop(), name="tolpo-automation")

    def kill(self):
        """Stop everything: no pending runs, no baselines, no ticks until the switch is cleared."""
        self.halted = True
        self.state.settings.killed = True
        self.state.runs = []
        self.previous, self.holds, self.log_ids = {}, {}, {}

    async def halt(self):
        """Stop the background loop now, even in the middle of a tick. A reserved cursor is never replayed."""
        self.halted = True
        task, self.task = self.task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def stop(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        self.store.unlock()

    async def _loop(self):
        try:
            await self.registry.ready.wait()
            while not self.closed and not self.state.settings.killed and any(r.enabled for r in self.state.rules):
                try:
                    await self.tick()
                except Exception as exc:
                    # Raw exception text can contain device secrets.
                    log.error("automation tick failed: %s", type(exc).__name__)
                    with suppress(Exception):
                        self.store.alert({"time": self.clock.now().isoformat(), "error": type(exc).__name__})
                await asyncio.sleep(self.state.settings.poll_seconds)
        finally:
            self.store.unlock()

    def _held(self, rule, key, predicate, values, timestamp):
        token = (rule.id, key)
        if not matches(predicate, values):
            self.holds.pop(token, None)
            return False
        since = self.holds.setdefault(token, timestamp)
        return timestamp - since >= predicate.hold_seconds

    def _conditions(self, rule, values, now):
        results = [self._held(rule, f"condition-{i}", p, values, now.timestamp()) for i, p in enumerate(rule.conditions)]
        if rule.windows and not any(in_window(w, now) for w in rule.windows):
            return False
        return not results or (all(results) if rule.condition_mode == "all" else any(results))

    def _time_trigger(self, trigger, now):
        if trigger.kind == "clock":
            return now.weekday() in trigger.weekdays and now.strftime("%H:%M") == trigger.time
        settings = self.state.settings
        if settings.latitude is None:
            return False
        # Offsets can cross midnight. Examine both adjacent solar dates.
        for delta in (-1, 0, 1):
            day = now.date() + timedelta(days=delta)
            if day.weekday() not in trigger.weekdays:
                continue
            moment = sun_time(day, settings.latitude, settings.longitude, trigger.kind == "sunset")
            if moment is not None:
                moment = (moment + timedelta(minutes=trigger.offset_minutes)).astimezone(now.tzinfo)
                if moment.replace(second=0, microsecond=0) == now.replace(second=0, microsecond=0):
                    return True
        return False

    def plan(self, action):
        if action.kind == "method":
            entry = self.registry.resolve_one(action.device)
            return self.caller.plan(entry, action.method)
        if action.kind == "tool":
            if self.tool_executor is None:
                return {"allowed": False, "reason": "tool action has no owner-approved adapter"}
            result = self.tool_executor.plan(action.tool, action.params)
            level = result.get("level")
            allowed = isinstance(level, int) and 0 <= level <= 4 and bool(result.get("allowed"))
            # `allow_methods` names device methods, not tools. A tool runs only within the owner's level.
            allowed = allowed and level <= self.registry.cfg.tier
            return {"level": level, "allowed": allowed,
                    **({"confirm": clean(result["confirm"])} if result.get("confirm") else {}),
                    **({"needs_confirmation": True} if result.get("needs_confirmation") else {})}
        if action.kind in ("power", "light"):
            entry = self.registry.resolve_one(action.device)
            fields = ["state"] if action.kind == "power" else [
                {"on": "state", "brightness": "brightness", "color_temp": "color_temperature", "hue": "hsv", "saturation": "hsv"}[key]
                for key in action.light.model_dump(exclude_none=True)]
            allowed = entry.kind in policy.POWER_KINDS and self.registry.cfg.tier >= 1 and all(
                fid in entry.device.features and policy.can_write(entry.kind, fid, self.registry.cfg.allow_write, self.registry.cfg.tier)
                for fid in fields)
            return {"level": 1, "allowed": allowed}
        return {"level": 0, "allowed": True}

    async def _action(self, action, rule, now, confirm):
        """Run one action. Returns its level."""
        plan = self.plan(action)
        if not plan.get("allowed"):
            raise Forbidden(f"{action.kind} action is blocked by the owner's policy")
        if plan.get("level", 0) >= 3 and (not confirm or (plan.get("confirm") is not None and confirm != plan["confirm"])):
            raise Forbidden("security or lifecycle automation action needs confirmation for this call")
        if action.kind == "message":
            quiet = self.state.settings.quiet_hours
            # Quiet messages stay in the outbox. Consumers defer delivery until after quiet hours.
            self.store.alert({"time": now.isoformat(), "rule": rule.id, "message": clean(action.message, 512),
                              **({"quiet": True} if quiet and in_window(quiet, now) else {})})
        elif action.kind == "tool":
            await self.tool_executor.execute(action.tool, action.params, confirm=confirm)
        else:
            entry = self.registry.resolve_one(action.device)
            if action.kind == "method":
                await self.caller.call(entry, action.method, action.params, confirm=confirm)
            elif action.kind == "power":
                await self.registry.set_power(entry, action.on)
            elif action.kind == "light":
                await self.registry.set_light(entry, **action.light.model_dump(exclude_none=True))
            await self.registry.refresh(entry, force=True)
            if not entry.online:
                raise DeviceUnavailableAfterWrite()
        return plan.get("level", 0)

    async def tick(self, *, dry_run=False, confirm=None):
        async with self.lock:
            if not dry_run and not self.state.settings.killed and self.store.killed_on_disk():
                # Another tolpo process pulled the kill switch. Obey it here too.
                self.kill()
                self.save()
            if self.state.settings.killed:
                return {"killed": True, "fired": []}
            if dry_run:
                state, previous, logs, holds = self.state, self.previous, self.log_ids, self.holds
                self.state, self.previous, self.log_ids, self.holds = copy.deepcopy((state, previous, logs, holds))
                try:
                    return await self._tick(True, None)
                finally:
                    self.state, self.previous, self.log_ids, self.holds = state, previous, logs, holds
            return await self._tick(False, confirm)

    async def _tick(self, dry, confirm):
        now = self.clock.now().astimezone(ZoneInfo(self.state.settings.timezone))
        stamp = now.timestamp()
        sample = await self.source.sample(self.state.rules)
        events = set()
        for ref, logs in sample.logs.items():
            ids = {row.get("id") for row in logs if isinstance(row, dict) and isinstance(row.get("id"), int)}
            known = self.log_ids.get(ref)
            if known is not None:
                for row in logs:
                    if isinstance(row, dict) and isinstance(row.get("id"), int) and row["id"] > known:
                        events.add((ref, str(row.get("event", ""))))
            # Log ids increase on the device. A reset re-establishes a baseline after restart.
            self.log_ids[ref] = max(ids | {known if known is not None else -1})
        fired, errors, plans = [], list(sample.errors), []
        condition_ok = {rule.id: self._conditions(rule, sample.values, now)
                        for rule in self.state.rules if rule.enabled}
        def queue(rule, ancestors):
            if rule.id in ancestors or any(run.rule == rule.id for run in self.state.runs):
                return False
            if stamp - self.state.last_run.get(rule.id, float("-inf")) < rule.minimum_interval:
                return False
            if not condition_ok.get(rule.id, False):
                return False
            self.state.last_run[rule.id] = stamp
            self.state.runs.append(Run(rule=rule.id, due=stamp, ancestors=ancestors + [rule.id]))
            fired.append(rule.id)
            return True
        for rule in self.state.rules:
            if not rule.enabled:
                continue
            triggered = False
            for i, trigger in enumerate(rule.triggers):
                if trigger.kind in ("clock", "sunrise", "sunset"):
                    mark = f"{now.date()}:{i}:{now.strftime('%H:%M')}"
                    mark_key = f"{rule.id}:{i}"
                    if self._time_trigger(trigger, now) and self.state.clock_marks.get(mark_key) != mark:
                        self.state.clock_marks[mark_key] = mark
                        triggered = True
                elif trigger.kind == "state":
                    p = trigger.predicate
                    key = (rule.id, f"state-{i}")
                    old = self.previous.get(p.device, {})
                    edge = p.field in old and old.get(p.field) != sample.values.get(p.device, {}).get(p.field)
                    held = False
                    if edge or key in self.holds:
                        held = self._held(rule, key[1], p, sample.values, stamp)
                    if held:
                        self.holds.pop(key, None)
                        triggered = True
                elif trigger.kind == "sensor":
                    triggered |= any(ref == trigger.device and (trigger.event is None or event == trigger.event) for ref, event in events)
                elif trigger.kind == "camera":
                    current = sample.values.get(trigger.device, {})
                    old = self.previous.get(trigger.device, {})
                    triggered |= bool(old and current.get("last_alert_timestamp") is not None
                                      and current.get("last_alert_timestamp") != old.get("last_alert_timestamp")
                                      and (trigger.event is None
                                           or trigger.event.casefold() == str(current.get("last_alert_type")).casefold()))
            if triggered:
                queue(rule, [])
        self.previous = sample.values
        # Durable scheduling happens before actions. Cursor reservation gives at-most-once attempts after a crash.
        if not dry:
            self.save()
        count = 0
        for run in list(self.state.runs):
            if count >= 200 or self.halted:
                break
            count += 1
            rule = self.rule(run.rule)
            if not rule.enabled:
                self.state.runs.remove(run)
                continue
            if run.due > stamp:
                continue
            failed = False
            while run.cursor < len(rule.actions) and not self.halted:
                action = rule.actions[run.cursor]
                run.cursor += 1
                if action.kind == "delay":
                    run.due = stamp + action.seconds
                    if not dry:
                        self.save()
                    if run.due > stamp:
                        break
                    continue
                if dry:
                    try:
                        plans.append({"rule": rule.id, "action": run.cursor - 1,
                                      **public_action(action), **self.plan(action)})
                    except Exception as exc:
                        errors.append({"rule": rule.id, "action": run.cursor - 1, "error": type(exc).__name__})
                    continue
                self.save()
                # Only a fresh confirmation supplied to this tick reaches a due action.
                try:
                    level = await self._action(action, rule, now, confirm)
                    if level >= 3:
                        confirm = None  # one confirmation, one security or lifecycle action
                    plans.append({"rule": rule.id, "action": run.cursor - 1,
                                  **public_action(action), "ok": True})
                except Exception as exc:
                    row = {"rule": rule.id, "action": run.cursor - 1, "error": type(exc).__name__}
                    if action.device:
                        row["device"] = clean(action.device)
                    errors.append(row)
                    self.store.alert({"time": now.isoformat(), **row})
                    failed = True
                    break
            if failed or run.cursor >= len(rule.actions):
                self.state.runs.remove(run)
                if not failed:
                    for follower in self.state.rules:
                        if follower.enabled and any(t.kind == "rule" and t.rule == rule.id for t in follower.triggers):
                            # Followers execute on the next tick with their ancestor chain.
                            queue(follower, run.ancestors)
            if not dry:
                self.save()
        output = {"fired": fired}
        if errors:
            output["errors"] = [{k: clean(v, 128) if isinstance(v, str) else v for k, v in e.items()} for e in errors]
        if plans:
            output["actions"] = plans
        if self.state.runs:
            output["pending"] = len(self.state.runs)
        return output


class DeviceUnavailableAfterWrite(Exception):
    """The write succeeded but fresh state is unavailable."""
