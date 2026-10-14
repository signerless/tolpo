"""A history of what door and window sensors do. A Tapo hub keeps none that it shares on the network (the app's
history comes from TP-Link's cloud), so `tolpo record` asks the sensors every few seconds and saves each change with
its time, in a file on this machine. The file is the owner's: mode 0600, never sent anywhere.

One JSON object per line, `t` in Unix seconds:
    {"t": ..., "event": "start", "devices": [...], "every": 5}    recording began
    {"t": ..., "device": "garage", "is_open": true, "first": true} the state at the first reading
    {"t": ..., "device": "garage", "is_open": false}               a change, seen within `every` seconds
    {"t": ..., "device": "garage", "event": "gap", "from": ..., "to": ...}  no reading in between
    {"t": ..., "event": "alive"}                                    still recording, every ten minutes
    {"t": ..., "event": "stop"}                                     recording ended
Nothing is known about the time between a stop (or the last line of a run that crashed) and the next start, or inside
a gap; the summary says so instead of guessing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from kasa.smartcam import SmartCamDevice

from .calls import Caller
from .registry import Entry, Registry, RegistryError

GAP = 60.0  # no reading of a device for this long is written down as a gap
ALIVE = 600.0  # how often a quiet recording says it is still running
MAX_LINE = 4096
READ_TIMEOUT = 15.0  # one reading of a hub; past this it counts as missed


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as out:
        out.write(json.dumps(record, separators=(",", ":")) + "\n")


def contact_sensors(registry: Registry) -> list[str]:
    return sorted(e.slug for e in registry.entries() if "is_open" in e.device.features)


async def _children(caller: Caller, hub: Entry) -> dict[str, dict[str, Any]]:
    """The hub's own list of its devices, page by page, keyed by device id."""
    camera_style = isinstance(hub.device, SmartCamDevice)
    method = "getChildDeviceList" if camera_style else "get_child_device_list"
    found: dict[str, dict[str, Any]] = {}
    start = 0
    for _ in range(20):
        page = await caller.call(hub, method, {"childControl": {"start_index": start}} if camera_style else {"start_index": start})
        kids = page.get("child_device_list") if isinstance(page, dict) else None
        if not isinstance(kids, list):
            raise RegistryError(f"{hub.slug} returned an invalid list of its devices")
        for kid in kids:
            if isinstance(kid, dict) and isinstance(kid.get("device_id"), str):
                found[kid["device_id"]] = kid
        start += len(kids)
        total = page.get("sum")
        if not kids or type(total) is not int or start >= total:
            break
    return found


def light_reader(registry: Registry) -> Callable[[list[Entry]], Awaitable[dict[str, bool | None]]]:
    """Read whether each sensor is open with one list request per hub. A full update of a hub and all its sensors
    every few seconds, next to another program that also updated it, made an H200 time out for both."""
    caller = Caller(registry)

    async def read(entries: list[Entry]) -> dict[str, bool | None]:
        states: dict[str, bool | None] = {}
        hubs: dict[int, tuple[Entry, list[Entry]]] = {}
        for entry in entries:
            if entry.root is None:
                await registry.refresh(entry, force=True)
                value = registry.feature_value(entry.device.features["is_open"]) if entry.online else None
                states[entry.slug] = value if isinstance(value, bool) else None
            else:
                hubs.setdefault(id(entry.root), (entry.root, []))[1].append(entry)
        for hub, sensors in hubs.values():
            try:
                listing = await asyncio.wait_for(_children(caller, hub), READ_TIMEOUT)
            except Exception:
                listing = {}  # a missed reading; a long run of them becomes a gap
            for entry in sensors:
                info = listing.get(entry.device.device_id) or {}
                value = info.get("open")
                states[entry.slug] = value if isinstance(value, bool) and info.get("status", "online") == "online" else None
        return states

    return read


async def record(registry: Registry, devices: list[str], path: str, *, every: float = 5.0,
                 stop: asyncio.Event | None = None, clock: Callable[[], float] = time.time,
                 read: Callable[[list[Entry]], Awaitable[dict[str, bool | None]]] | None = None) -> None:
    """Read the sensors every `every` seconds until `stop` is set, and append what changed to `path`. A stop does
    not wait for a reading that is still under way, so the stop line is always written."""
    entries = [registry.resolve_one(name) for name in devices]
    for entry in entries:
        if "is_open" not in entry.device.features:
            raise RegistryError(f"{entry.slug} is not a door or window sensor")
    file = Path(path).expanduser()
    stop = stop or asyncio.Event()
    read = read or light_reader(registry)
    state: dict[str, bool] = {}
    seen: dict[str, float] = {}
    _append(file, {"t": int(clock()), "event": "start", "devices": [e.slug for e in entries], "every": every})
    alive = clock()
    try:
        while not stop.is_set():
            reading = asyncio.ensure_future(read(entries))
            stopping = asyncio.ensure_future(stop.wait())
            await asyncio.wait({reading, stopping}, return_when=asyncio.FIRST_COMPLETED)
            stopping.cancel()
            if not reading.done():
                reading.cancel()
                with contextlib.suppress(BaseException):
                    await reading
                break
            states = reading.result()
            now = clock()
            for entry in entries:
                value = states.get(entry.slug)
                if not isinstance(value, bool):
                    continue
                if entry.slug in seen and now - seen[entry.slug] > GAP:
                    _append(file, {"t": int(now), "device": entry.slug, "event": "gap", "from": int(seen[entry.slug]), "to": int(now)})
                    state.pop(entry.slug, None)  # what happened in the gap is unknown
                seen[entry.slug] = now
                if state.get(entry.slug) != value:
                    line = {"t": int(now), "device": entry.slug, "is_open": value}
                    if entry.slug not in state:
                        line["first"] = True
                    _append(file, line)
                    state[entry.slug] = value
            if now - alive >= ALIVE:
                _append(file, {"t": int(now), "event": "alive"})
                alive = now
            try:
                await asyncio.wait_for(stop.wait(), every)
            except TimeoutError:
                pass
    finally:
        _append(file, {"t": int(clock()), "event": "stop"})


def read_history(path: str) -> tuple[list[dict[str, Any]], int]:
    """The saved lines in time order, and how many lines could not be read."""
    file = Path(path).expanduser()
    if not file.is_file():
        return [], 0
    lines, broken = [], 0
    with file.open(encoding="utf-8", errors="replace") as source:
        for raw in source:
            try:
                item = json.loads(raw) if len(raw) <= MAX_LINE else None
            except ValueError:
                item = None
            if not isinstance(item, dict) or type(item.get("t")) is not int:
                broken += 1
                continue
            lines.append(item)
    lines.sort(key=lambda item: item["t"])
    return lines, broken


def _local(t: float) -> datetime:
    return datetime.fromtimestamp(t).astimezone()


def summarize(lines: list[dict[str, Any]], device: str, *, since: float, until: float) -> dict[str, Any]:
    """Openings of one sensor between `since` and `until`, counted only when the change was seen. An opening whose
    closing fell in a gap or a pause of the recording has no known length, and the summary keeps those apart."""
    openings: list[dict[str, Any]] = []
    unknown: list[tuple[float, float]] = []  # times nothing was recorded about this device
    state: bool | None = None
    opened: float | None = None
    running = False
    last: float | None = None  # the last moment the recording was known to run

    def broken(at: float) -> None:
        nonlocal state, opened
        if opened is not None:
            openings.append({"opened": opened, "closed": None})
        state, opened = None, None

    for item in lines:
        t = item["t"]
        event = item.get("event")
        if event == "start":
            if running and last is not None:
                unknown.append((last, t))  # the previous run ended without saying so
            elif last is not None:
                unknown.append((last, t))
            broken(t)
            running, last = device in item.get("devices", []), t
            continue
        if not running:
            continue
        if event in ("stop", "alive"):
            last = t
            if event == "stop":
                broken(t)
                running = False
            continue
        if item.get("device") != device:
            continue
        last = t
        if event == "gap":
            unknown.append((item.get("from", t), item.get("to", t)))
            broken(t)
            continue
        value = item.get("is_open")
        if not isinstance(value, bool):
            continue
        if state is False and value:
            opened = t
        elif state is True and not value and opened is not None:
            openings.append({"opened": opened, "closed": t})
            opened = None
        state = value
    open_now = state if running else None
    if opened is not None:
        openings.append({"opened": opened, "closed": None, "still_open": True})

    first_start = next((item["t"] for item in lines if item.get("event") == "start" and device in item.get("devices", [])), None)
    window = [o for o in openings if since <= o["opened"] <= until]
    lengths = [(o["closed"] - o["opened"]) / 60 for o in window if o["closed"] is not None]
    per_day = Counter(_local(o["opened"]).strftime("%Y-%m-%d") for o in window)
    per_hour = Counter(_local(o["opened"]).strftime("%H") for o in window)
    gaps = [(max(a, since), min(b, until)) for a, b in unknown if b > since and a < until and b - a > 0]
    start = max(since, first_start) if first_start is not None else None
    end = min(until, last if not running else until) if last is not None else None
    recorded = max(0.0, (end - start) - sum(b - a for a, b in gaps)) if start is not None and end is not None and end > start else 0.0
    iso = lambda t: _local(t).isoformat(timespec="seconds")
    return {
        "device": device,
        "recorded_from": iso(start) if start is not None else None,
        "recorded_to": iso(end) if end is not None else None,
        "recorded_hours": round(recorded / 3600, 2),
        "not_recorded": [{"from": iso(a), "to": iso(b)} for a, b in gaps[-50:]],
        "openings": len(window),
        "openings_with_known_length": len(lengths),
        "open_minutes_total": round(sum(lengths), 1),
        "open_minutes_longest": round(max(lengths), 1) if lengths else None,
        "open_minutes_average": round(sum(lengths) / len(lengths), 1) if lengths else None,
        "per_day": dict(sorted(per_day.items())),
        "per_hour": dict(sorted(per_hour.items())),
        "open_now": open_now,
        "recent": [{"opened": iso(o["opened"]), "closed": iso(o["closed"]) if o["closed"] is not None else None,
                    **({"still_open": True} if o.get("still_open") else {})} for o in window[-20:]],
    }
