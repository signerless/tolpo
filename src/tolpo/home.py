"""The floor plan behind the 3D home view at /home: rooms, and where each device sits in them.

The plan lives in the owner's own file (`server.home_layout`, by default ~/.config/tolpo/home.json) and never ships with
tolpo, because it holds the owner's rooms and device names. Every value is checked before it is used or written, so a
damaged or hostile file cannot put markup, huge numbers or unknown keys in front of the browser.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

VERSION = 1
MAX_ROOMS = 64
MAX_DEVICES = 512
REACH = 200.0  # metres from the origin; a house fits easily
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
ROOM_KEYS = {"id", "name", "x", "z", "w", "d", "color", "outside"}
DEVICE_KEYS = {"room", "x", "z", "y", "yaw"}


class LayoutError(ValueError):
    """The plan is not valid. The message says which value and why, and never repeats the value itself."""


def empty() -> dict[str, Any]:
    return {"version": VERSION, "rooms": [], "devices": {}}


def _number(value: Any, name: str, low: float = -REACH, high: float = REACH) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise LayoutError(f"{name} must be a number from {low:g} to {high:g}")
    return float(value)


def _text(value: Any, name: str, limit: int = 48) -> str:
    if not isinstance(value, str):
        raise LayoutError(f"{name} must be text")
    out = " ".join(value.split())[:limit]  # one line, no control characters, bounded
    out = "".join(ch for ch in out if ch.isprintable())
    if not out:
        raise LayoutError(f"{name} must not be empty")
    return out


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise LayoutError(f"{name} must be a short id: lower case letters, digits and hyphens")
    return value


def validate(data: Any) -> dict[str, Any]:
    """A clean copy of the plan, or LayoutError."""
    if not isinstance(data, dict):
        raise LayoutError("the plan must be an object")
    if set(data) - {"version", "rooms", "devices"}:
        raise LayoutError("the plan has unknown fields")
    if data.get("version", VERSION) != VERSION:
        raise LayoutError(f"the plan version must be {VERSION}")
    rooms = data.get("rooms", [])
    if not isinstance(rooms, list) or len(rooms) > MAX_ROOMS:
        raise LayoutError(f"rooms must be a list of at most {MAX_ROOMS}")
    clean_rooms: list[dict[str, Any]] = []
    for i, room in enumerate(rooms):
        where = f"rooms[{i}]"
        if not isinstance(room, dict) or set(room) - ROOM_KEYS:
            raise LayoutError(f"{where} must be an object with the room fields only")
        rid = _identifier(room.get("id"), f"{where}.id")
        if any(r["id"] == rid for r in clean_rooms):
            raise LayoutError(f"{where}.id is used twice")
        out = {
            "id": rid,
            "name": _text(room.get("name"), f"{where}.name"),
            "x": _number(room.get("x"), f"{where}.x"),
            "z": _number(room.get("z"), f"{where}.z"),
            "w": _number(room.get("w"), f"{where}.w", 0.5, 100),
            "d": _number(room.get("d"), f"{where}.d", 0.5, 100),
        }
        if "color" in room:
            if not isinstance(room["color"], str) or not COLOR.fullmatch(room["color"]):
                raise LayoutError(f"{where}.color must look like #a1b2c3")
            out["color"] = room["color"].lower()
        if "outside" in room:
            if type(room["outside"]) is not bool:
                raise LayoutError(f"{where}.outside must be true or false")
            out["outside"] = room["outside"]
        clean_rooms.append(out)
    devices = data.get("devices", {})
    if not isinstance(devices, dict) or len(devices) > MAX_DEVICES:
        raise LayoutError(f"devices must be an object with at most {MAX_DEVICES} entries")
    known_rooms = {r["id"] for r in clean_rooms}
    clean_devices: dict[str, dict[str, Any]] = {}
    for key, spot in devices.items():
        did = _identifier(key, "a device id")
        if not isinstance(spot, dict) or set(spot) - DEVICE_KEYS:
            raise LayoutError(f"the place of {did} must be an object with room, x, z, y and yaw only")
        out = {"x": _number(spot.get("x"), f"{did}.x"), "z": _number(spot.get("z"), f"{did}.z")}
        if "y" in spot:
            out["y"] = _number(spot["y"], f"{did}.y", 0, 20)
        if "yaw" in spot:
            out["yaw"] = _number(spot["yaw"], f"{did}.yaw", -360, 360)
        if "room" in spot:
            room = _identifier(spot["room"], f"{did}.room")
            if room not in known_rooms:
                raise LayoutError(f"{did}.room names a room that is not in the plan")
            out["room"] = room
        clean_devices[did] = out
    return {"version": VERSION, "rooms": clean_rooms, "devices": clean_devices}


def load(path: str) -> dict[str, Any] | None:
    """The owner's plan, checked, or None when there is no file yet."""
    file = Path(path).expanduser()
    try:
        raw = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise LayoutError("the plan file is not valid JSON") from None
    return validate(data)


def save(path: str, data: Any) -> dict[str, Any]:
    """Check the plan and write it in one step, readable by the owner only. Refuses to write through a symbolic link."""
    clean = validate(data)
    file = Path(path).expanduser()
    if file.is_symlink():
        raise LayoutError("the plan file is a symbolic link; tolpo will not write through it")
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=".home-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(clean, handle, indent=1, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return clean
