"""The door and window history that `tolpo record` keeps, because the hub keeps none."""

import asyncio
import json
import stat

import pytest
from mcp import Client

from tolpo import recorder
from tolpo.config import Config
from tolpo.mcp_server import build_server
from tolpo.registry import Registry, RegistryError

from . import fakes


@pytest.fixture
def anyio_backend():
    return "asyncio"


def house(tmp_path, **settings):
    reg = Registry(Config(email="a", password="b", cache_ttl=0.0, cache_file="", history_file=str(tmp_path / "h" / "history.jsonl"), **settings))
    return reg, fakes.populate(reg)


async def run(reg, devices, script, path, every=5):
    """Each reading of the recorder takes the next (time, open) from `script`; it stops when the script ends."""
    stop, now, steps = asyncio.Event(), [script[0][0]], iter(script)

    async def read(entries):
        try:
            now[0], value = next(steps)
        except StopIteration:
            stop.set()
            return {}
        return {entry.slug: value for entry in entries}

    await recorder.record(reg, devices, path, every=0.001, stop=stop, clock=lambda: now[0], read=read)


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.anyio
async def test_changes_are_saved_with_their_time_and_a_gap_is_written_down(tmp_path):
    reg, _ = house(tmp_path)
    path = tmp_path / "h" / "history.jsonl"
    await run(reg, ["main door"], [(1000, False), (1005, False), (1010, True), (1015, True), (1200, False), (1205, True), (1210, False)], str(path))
    saved = lines(path)
    assert saved[0] == {"t": 1000, "event": "start", "devices": ["main-door"], "every": 0.001}
    assert saved[1:] == [
        {"t": 1000, "device": "main-door", "is_open": False, "first": True},
        {"t": 1010, "device": "main-door", "is_open": True},
        {"t": 1200, "device": "main-door", "event": "gap", "from": 1015, "to": 1200},
        {"t": 1200, "device": "main-door", "is_open": False, "first": True},  # after a gap the state is new again
        {"t": 1205, "device": "main-door", "is_open": True},
        {"t": 1210, "device": "main-door", "is_open": False},
        {"t": 1210, "event": "stop"},
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.anyio
async def test_only_door_and_window_sensors_are_recorded(tmp_path):
    reg, _ = house(tmp_path)
    with pytest.raises(RegistryError, match="not a door or window sensor"):
        await recorder.record(reg, ["office-1"], str(tmp_path / "x.jsonl"))
    assert recorder.contact_sensors(reg) == ["main-door"]


def write(path, items):
    path.write_text("".join(json.dumps(item) + "\n" for item in items))


def test_the_summary_counts_only_what_was_seen(tmp_path):
    path = tmp_path / "history.jsonl"
    write(path, [
        {"t": 0, "event": "start", "devices": ["garage"], "every": 5},
        {"t": 0, "device": "garage", "is_open": False, "first": True},
        {"t": 600, "device": "garage", "is_open": True},
        {"t": 900, "device": "garage", "is_open": False},          # 5 minutes open
        {"t": 3600, "device": "garage", "is_open": True},
        {"t": 7200, "device": "garage", "event": "gap", "from": 3700, "to": 7200},
        {"t": 7200, "device": "garage", "is_open": False, "first": True},  # closed sometime in the gap: length unknown
        {"t": 9000, "event": "stop"},
        {"t": 20000, "event": "start", "devices": ["garage"], "every": 5},
        {"t": 20000, "device": "garage", "is_open": True, "first": True},  # already open: not counted as an opening
        {"t": 20300, "device": "garage", "is_open": False},
        {"t": 21000, "device": "garage", "is_open": True},
        {"t": 21600, "device": "garage", "is_open": False},        # 10 minutes open
        {"t": 21600, "device": "other", "is_open": True},
        "not json",
    ])
    path.write_text(path.read_text().replace('"not json"', "not json"))
    saved, broken = recorder.read_history(str(path))
    out = recorder.summarize(saved, "garage", since=0, until=22000)
    assert broken == 1
    assert out["openings"] == 3 and out["openings_with_known_length"] == 2
    assert out["open_minutes_total"] == 15.0 and out["open_minutes_longest"] == 10.0 and out["open_minutes_average"] == 7.5
    assert sum(out["per_day"].values()) == 3 and sum(out["per_hour"].values()) == 3
    assert len(out["not_recorded"]) == 2  # the gap, and the time between the stop and the next start
    assert out["recorded_hours"] == pytest.approx((22000 - 0 - 3500 - 11000) / 3600, abs=0.01)
    assert out["open_now"] is False
    assert [o["closed"] is None for o in out["recent"]] == [False, True, False]


def test_a_recording_that_crashed_leaves_a_hole_until_it_starts_again(tmp_path):
    path = tmp_path / "history.jsonl"
    write(path, [
        {"t": 0, "event": "start", "devices": ["garage"], "every": 5},
        {"t": 0, "device": "garage", "is_open": False, "first": True},
        {"t": 600, "event": "alive"},
        {"t": 5000, "event": "start", "devices": ["garage"], "every": 5},
        {"t": 5000, "device": "garage", "is_open": False, "first": True},
    ])
    out = recorder.summarize(recorder.read_history(str(path))[0], "garage", since=0, until=6000)
    assert len(out["not_recorded"]) == 1 and out["recorded_hours"] == pytest.approx((6000 - 4400) / 3600, abs=0.01)


@pytest.mark.anyio
async def test_an_agent_reads_the_history_over_mcp(tmp_path):
    reg, _ = house(tmp_path)
    path = tmp_path / "h" / "history.jsonl"
    async with Client(build_server(reg, discover=False)) as client:
        nothing = (await client.call_tool("recorded_history", {"device": "main door"})).structured_content
        assert nothing["recorded"] is False and "tolpo record main-door" in nothing["how"]
        await run(reg, ["main door"], [(2_000_000_000, False), (2_000_000_005, True), (2_000_000_065, False)], str(path))
        # the recording above is in the future of this test's clock: read it back with a window that holds it
        out = (await client.call_tool("recorded_history", {"device": "main-door", "days": 366})).structured_content
    assert out["recorded"] is True and out["device"] == "main-door" and out["times_within_seconds"] == 0.001


@pytest.mark.anyio
async def test_a_stop_does_not_wait_for_a_slow_reading(tmp_path):
    reg, _ = house(tmp_path)
    path = tmp_path / "h" / "history.jsonl"
    stop = asyncio.Event()

    async def stuck(entries):
        await asyncio.sleep(3600)

    task = asyncio.create_task(recorder.record(reg, ["main-door"], str(path), stop=stop, read=stuck))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert [line.get("event") for line in lines(path)] == ["start", "stop"]


@pytest.mark.anyio
async def test_one_list_request_per_hub_gives_every_sensor(tmp_path):
    reg, devices = house(tmp_path)
    door = reg.resolve_one("main-door")
    asked = []

    async def query(request):
        asked.append(request)
        (method, params), = request.items()
        return {method: {"start_index": 0, "sum": 1, "child_device_list": [{"device_id": door.device.device_id, "open": True, "status": "online"}]}}

    devices["hub"].protocol = type("P", (), {"query": staticmethod(query)})()
    states = await recorder.light_reader(reg)([door])
    assert states == {"main-door": True}
    assert asked == [{"get_child_device_list": {"start_index": 0}}]  # one light request, no full update
