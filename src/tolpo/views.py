"""Presentation shared by the HTTP API and the MCP server: one place decides what a device looks like."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from typing import Any, Awaitable, Callable

from kasa import Feature
from pydantic import BaseModel, Field, JsonValue

from . import policy
from .registry import Entry, Registry, RegistryError

LIGHT_SETTINGS = ("brightness", "color_temperature", "hsv")
LOW_BATTERY_PERCENT = 20


class FeatureOut(BaseModel):
    id: str
    name: str
    type: str
    category: str
    value: JsonValue = None
    unit: str | None = None
    writable: bool
    min: float | None = None
    max: float | None = None
    choices: list[str] | None = None


class DeviceSummary(BaseModel):
    id: str = Field(description="Short name usable in URLs, for example `office-1`")
    device_id: str
    name: str | None
    model: str
    kind: str = Field(description="light, plug, switch, sensor, hub, camera, doorbell")
    host: str | None
    parent: str | None = Field(description="`id` of the hub this sensor is paired to")
    online: bool
    on: bool | None = Field(description="Power state for lights and plugs; null when not applicable")
    capabilities: list[str] = Field(description="What an agent can do here: power, brightness, color_temp, color")
    settings: dict[str, JsonValue] = Field(description="Current light settings: brightness, color_temperature, hsv")
    readings: dict[str, JsonValue]
    units: dict[str, str]
    battery: int | None


class DeviceDetail(DeviceSummary):
    features: list[FeatureOut]


class BatchItem(BaseModel):
    target: str
    id: str | None = None
    ok: bool
    error: str | None = None
    device: DeviceSummary | None = None


def summary(reg: Registry, entry: Entry) -> DeviceSummary:
    dev = entry.device
    feats = dev.features
    readings, units = reg.readings(entry)
    state = feats.get("state")
    battery = feats.get("battery_level")
    on = reg.feature_value(state) if state is not None else None
    return DeviceSummary(
        id=entry.slug,
        device_id=dev.device_id,
        name=dev.alias,
        model=dev.model,
        kind=entry.kind,
        host=None if entry.root else dev.host,
        parent=entry.root.slug if entry.root else None,
        online=entry.online,
        on=on if isinstance(on, bool) else None,
        capabilities=reg.capabilities(entry),
        settings={fid: reg.feature_value(feats[fid]) for fid in LIGHT_SETTINGS if fid in feats},
        readings=readings,
        units=units,
        battery=reg.feature_value(battery) if battery is not None else None,
    )


def detail(reg: Registry, entry: Entry) -> DeviceDetail:
    out = []
    for fid, feat in entry.device.features.items():
        if fid in policy.HIDDEN:
            continue
        has_range = feat.type is Feature.Type.Number
        out.append(
            FeatureOut(
                id=fid,
                name=feat.name,
                type=feat.type.name,
                category=feat.category.name,
                value=reg.feature_value(feat),
                unit=str(feat.unit) if feat.unit else None,
                writable=policy.can_write(entry.kind, fid, reg.cfg.allow_write),
                min=feat.minimum_value if has_range else None,
                max=feat.maximum_value if has_range else None,
                choices=list(feat.choices) if feat.type is Feature.Type.Choice and feat.choices else None,
            )
        )
    return DeviceDetail(**summary(reg, entry).model_dump(), features=out)


async def run_batch(
    reg: Registry, targets: list[str], op: Callable[[Entry], Awaitable[None]]
) -> list[BatchItem]:
    """Apply `op` to every device the targets match; one failure does not stop the others."""
    entries: list[Entry] = []
    results: list[BatchItem] = []
    for target in targets:
        found = reg.resolve(target)
        if not found:
            results.append(BatchItem(target=target, ok=False, error="no device matches"))
        for entry in found:
            if entry not in entries:
                entries.append(entry)
                results.append(BatchItem(target=target, id=entry.slug, ok=True))
    by_slug = {r.id: r for r in results if r.id}

    async def one(entry: Entry) -> None:
        item = by_slug[entry.slug]
        try:
            await op(entry)
            item.device = summary(reg, entry)
        except RegistryError as exc:
            item.ok, item.error = False, str(exc)

    await asyncio.gather(*(one(e) for e in entries))
    return results


# ---- compact forms for language models ----------------------------------------------------


def clean(text: str, limit: int = 64) -> str:
    """Device names are free text typed by whoever set the device up, so keep them short and flat."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", text)).strip()[:limit]


def missing_view(reg: Registry) -> list[dict[str, str]]:
    """Known devices that are unreachable right now, by name and never by address."""
    return [{"name": clean(m["name"]), "model": m["model"], "reason": m["reason"]} for m in reg.not_connected()]


def compact(reg: Registry, entry: Entry) -> dict[str, Any]:
    """Like `summary` but without ids, hosts and empty fields, because every token costs context."""
    s = summary(reg, entry)
    out: dict[str, Any] = {"id": s.id, "name": clean(s.name or s.id), "kind": s.kind, "model": s.model, "online": s.online}
    for key, value in (
        ("parent", s.parent),
        ("on", s.on),
        ("can", s.capabilities),
        ("settings", s.settings),
        ("readings", s.readings),
        ("units", s.units),
        ("battery", s.battery),
    ):
        if value is not None and value != [] and value != {}:
            out[key] = value
    return out


def compact_batch(items: list[BatchItem]) -> list[dict[str, Any]]:
    out = []
    for item in items:
        row: dict[str, Any] = {"target": item.target, "ok": item.ok}
        if item.id:
            row["id"] = item.id
        if item.error:
            row["error"] = item.error
        if item.device is not None:
            if item.device.on is not None:
                row["on"] = item.device.on
            if item.device.settings:
                row["settings"] = item.device.settings
        out.append(row)
    return out


def overview(reg: Registry) -> dict[str, Any]:
    """One glance at the house: what is on, open, wet, offline or running low on battery."""
    entries = reg.entries()
    missing = missing_view(reg)
    summaries = [(e, summary(reg, e)) for e in entries]
    triggered = []
    low_battery = []
    climate = []
    for entry, s in summaries:
        for reading, value in s.readings.items():
            if value is True and reading not in ("is_open", "water_alert"):
                triggered.append({"id": s.id, "reading": reading})
        feats = entry.device.features
        low = feats.get("battery_low")
        flagged = low is not None and reg.feature_value(low) is True
        if flagged or (s.battery is not None and s.battery < LOW_BATTERY_PERCENT):
            low_battery.append({"id": s.id, **({"battery": s.battery} if s.battery is not None else {})})
        if "temperature" in s.readings:
            row = {"id": s.id, **{k: s.readings[k] for k in ("temperature", "humidity") if k in s.readings}}
            if "temperature" in s.units:
                row["unit"] = s.units["temperature"]
            climate.append(row)
    return {
        "devices": len(entries),
        "by_kind": dict(Counter(s.kind for _, s in summaries)),
        "offline": [s.id for _, s in summaries if not s.online],
        "powered_on": [s.id for _, s in summaries if s.on is True and s.kind in policy.POWER_KINDS],
        "open": [s.id for _, s in summaries if s.readings.get("is_open") is True],
        "water_alerts": [s.id for _, s in summaries if s.readings.get("water_alert") is True],
        "other_triggered": triggered,
        "low_battery": low_battery,
        "climate": climate,
        "complete": not missing,
        "not_connected": missing,
    }
