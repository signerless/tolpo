"""The home plan tools: an agent drafts, reads and saves the floor plan of the 3D home view."""

from types import SimpleNamespace

import pytest

from tolpo import home
from tolpo.areas import home_plan


@pytest.mark.anyio
async def test_the_draft_places_every_device_and_saves_nothing(make_house, mcp, tmp_path):
    path = tmp_path / "home.json"
    reg, caller, _ = make_house(home_layout=str(path))
    async with mcp(reg, caller) as client:
        res = await client.call_tool("home_plan_draft", {})
    assert not res.is_error
    draft = res.structured_content
    plan = draft["plan"]
    assert home.validate(plan) == plan
    assert set(plan["devices"]) == {e.slug for e in reg.entries()}
    assert plan["devices"]["office-1"]["room"] == plan["devices"]["office-2"]["room"] == "office"
    rooms = {r["id"]: r for r in plan["rooms"]}
    door = plan["devices"]["main-door"]
    room = rooms[door["room"]]
    assert door["z"] in (room["z"], room["z"] + room["d"]) or door["x"] in (room["x"], room["x"] + room["w"])  # on a wall
    assert "home_plan_set" in draft["next"] and "yaw" in draft["format"]
    assert not path.exists()


@pytest.mark.anyio
async def test_read_save_and_read_again(make_house, mcp, tmp_path):
    path = tmp_path / "home.json"
    reg, caller, _ = make_house(home_layout=str(path))
    async with mcp(reg, caller) as client:
        first = (await client.call_tool("home_plan_get", {})).structured_content
        assert first["plan"] == home.empty()
        assert set(first["unplaced_devices"]) == {e.slug for e in reg.entries()}
        plan = (await client.call_tool("home_plan_draft", {})).structured_content["plan"]
        plan["devices"]["gone-device"] = {"x": 1, "z": 1}
        saved = (await client.call_tool("home_plan_set", {"plan": plan})).structured_content
        assert saved["saved"] and saved["placed_but_not_found"] == ["gone-device"] and saved["unplaced_devices"] == []
        again = (await client.call_tool("home_plan_get", {})).structured_content
        assert again["plan"]["devices"]["office-1"] == plan["devices"]["office-1"]
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.anyio
async def test_a_bad_plan_is_refused_and_the_file_kept(make_house, mcp, tmp_path):
    path = tmp_path / "home.json"
    reg, caller, _ = make_house(home_layout=str(path))
    home.save(str(path), home.empty())
    before = path.read_text()
    async with mcp(reg, caller) as client:
        res = await client.call_tool("home_plan_set", {"plan": {"version": 1, "rooms": [{"id": "<b>"}], "devices": {}}})
    text = " ".join(c.text for c in res.content if c.type == "text")
    assert res.is_error and "not saved" in text and "<b>" not in text
    assert path.read_text() == before


@pytest.mark.anyio
async def test_level_zero_reads_but_cannot_save(make_house, mcp, tmp_path):
    path = tmp_path / "home.json"
    reg, caller, _ = make_house(tier=0, home_layout=str(path))
    async with mcp(reg, caller) as client:
        res = await client.call_tool("home_plan_set", {"plan": home.empty()})
        assert res.is_error and "level 1" in " ".join(c.text for c in res.content if c.type == "text")
        assert not (await client.call_tool("home_plan_draft", {})).is_error
        assert not (await client.call_tool("home_plan_get", {})).is_error
    assert not path.exists()


@pytest.mark.anyio
async def test_the_view_address(make_house, mcp):
    reg, caller, _ = make_house(host="0.0.0.0", port=9000)
    async with mcp(reg, caller) as client:
        out = (await client.call_tool("home_view_url", {})).structured_content
    assert out["url"] == "http://127.0.0.1:9000/home" and "tolpo serve" in out["how"]


def entry(slug, kind, alias="", features=()):
    return SimpleNamespace(slug=slug, kind=kind, device=SimpleNamespace(alias=alias, features={f: None for f in features}))


@pytest.mark.parametrize("item,room", [
    (entry("kueche", "light", "Küche Licht"), "kitchen"),
    (entry("lampara", "plug", "Lámpara salón"), "living"),
    (entry("water-1", "sensor", "Baño", ["water_alert"]), "bathroom"),
    (entry("leak", "sensor", "Leak", ["water_alert"]), "bathroom"),
    (entry("kinder", "sensor", "Kinderzimmer Fenster", ["is_open"]), "kids"),
    (entry("ring", "doorbell", "Ring"), "front"),
    (entry("garten", "camera", "Garten Kamera"), "backyard"),
    (entry("cam-7", "camera", "Cam 7"), "backyard"),
    (entry("back-door", "sensor", "Back door", ["is_open"]), "living"),
    (entry("waschraum", "camera", "Waschraum"), "utility"),
    (entry("thermo", "sensor", "Thermo", ["temperature"]), "living"),
    (entry("mystery", "sensor", "Mystery"), "other"),
])
def test_rooms_are_guessed_from_names_in_three_languages(item, room):
    assert home_plan._guess_room(item) == room
