"""MCP tool for the door and window history that `tolpo record` saves on this machine.

A Tapo hub keeps no event history of its own for sensors that are connected to TP-Link's cloud: asked the way the app
asks (get_trigger_logs), an H200 answers with an empty list and a sum of 0 for a T110 door sensor, and the app then
reads that history from the cloud. tolpo stays local, so the only history is the one it records itself.
"""

# Do NOT add `from __future__ import annotations` here: the MCP SDK reads the tool signatures at runtime.
import time
from typing import Annotated, Any

from pydantic import Field

from .. import recorder
from ..registry import RegistryError

METHODS = []  # no device method: the history is tolpo's own file


def _slug(registry, device: str) -> str:
    try:
        return registry.resolve_one(device).slug
    except RegistryError:
        return device.strip().lower()  # a sensor that is offline now may still have a history


def tools(ctx):
    @ctx.tool(title="Recorded door and window history")
    async def recorded_history(
        device: Annotated[str, Field(description="Door or window sensor id or name, for example garage", min_length=1, max_length=64)],
        days: Annotated[float, Field(description="How many days back to look, up to 366", gt=0, le=366)] = 7,
    ) -> dict[str, Any]:
        """How often a door or window sensor opened and for how long, per day and per hour, from what `tolpo record`
        saved on this machine. Only openings the recording saw are counted. Times when nothing was recorded (the
        computer asleep, the sensor out of reach, the recording stopped) are listed in not_recorded and never
        guessed. Times are local. The hub keeps no such history itself. No device is contacted."""
        with ctx.translated():
            slug = _slug(ctx.registry, device)
            lines, broken = recorder.read_history(ctx.registry.cfg.history_file)
            starts = [line for line in lines if line.get("event") == "start" and slug in line.get("devices", [])]
            if not starts:
                return {"device": slug, "recorded": False,
                        "how": f"Nothing was recorded for {slug} yet. `tolpo record {slug}` records it while it runs, "
                               "and a background service keeps it running."}
            now = time.time()
            out = {"recorded": True, **recorder.summarize(lines, slug, since=now - days * 86400, until=now)}
            out["times_within_seconds"] = max(float(line.get("every", 0)) for line in starts)
            if broken:
                out["unreadable_lines"] = broken
            return out
