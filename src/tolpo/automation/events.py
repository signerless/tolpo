"""Local event sources. Initial observations establish a baseline, not an event."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from tolpo import policy
from tolpo.registry import DeviceUnavailable


@dataclass
class Sample:
    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    logs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)


class EventSource(Protocol):
    async def sample(self, rules) -> Sample: ...


class RegistrySource:
    def __init__(self, registry, caller):
        self.registry, self.caller = registry, caller

    async def sample(self, rules):
        sample = Sample()
        refs, log_refs = set(), set()
        for rule in rules:
            if not rule.enabled:
                continue
            for predicate in rule.conditions:
                refs.add(predicate.device)
            for trigger in rule.triggers:
                if trigger.predicate:
                    refs.add(trigger.predicate.device)
                if trigger.device:
                    refs.add(trigger.device)
                    if trigger.kind == "sensor":
                        log_refs.add(trigger.device)
        entries = {}
        for ref in sorted(refs):
            try:
                entries[ref] = self.registry.by_slug(ref)
            except Exception as exc:
                sample.errors.append({"device": ref, "error": type(exc).__name__})
        tops = set()
        for ref, entry in entries.items():
            try:
                if entry.top not in tops:
                    await self.registry.refresh(entry, force=True)
                    tops.add(entry.top)
                if not entry.online:
                    raise DeviceUnavailable("device is offline")
                values, _ = self.registry.readings(entry)
                # Switches and brightness are not Sensor features in registry.readings.
                for name in ("state", "brightness", "color_temperature", "hsv", "last_alert_timestamp", "last_alert_type"):
                    feature = entry.device.features.get(name)
                    if feature is not None and name not in policy.HIDDEN:
                        values[name] = self.registry.feature_value(feature)
                sample.values[ref] = values
                if ref in log_refs:
                    data = await self.caller.call(entry, "get_trigger_logs", {"start_id": 0})
                    logs = data.get("logs", [])
                    if not isinstance(logs, list):
                        raise ValueError("invalid trigger log response")
                    sample.logs[ref] = logs
            except Exception as exc:
                sample.values.pop(ref, None)
                sample.errors.append({"device": ref, "error": type(exc).__name__})
        return sample


class FakeSource:
    """Mutable fake observations; tests advance these and the fake clock directly."""
    def __init__(self, sample=None):
        self.current = sample or Sample()

    async def sample(self, rules):
        import copy
        return copy.deepcopy(self.current)
