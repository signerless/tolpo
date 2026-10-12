"""The 3D home view: the floor plan file and the routes that serve the page and the plan."""

import base64
import hashlib
import json
import math
import os
import re

import pytest

from tolpo import home

PLAN = {
    "version": 1,
    "rooms": [
        {"id": "office", "name": "Office", "x": -8, "z": -5.5, "w": 5, "d": 5},
        {"id": "garden", "name": "Garden", "x": -10, "z": -13, "w": 25, "d": 7.5, "outside": True, "color": "#2F4A33"},
    ],
    "devices": {
        "office-1": {"room": "office", "x": -6.75, "z": -4.25},
        "hallway-cam": {"x": 0.5, "z": -5.9, "yaw": -30, "y": 0},
    },
}


def test_a_good_plan_is_kept_and_cleaned():
    plan = json.loads(json.dumps(PLAN))
    plan["rooms"][0]["name"] = "  Office\n\twith\u0007 a bell  "
    out = home.validate(plan)
    assert out["rooms"][0]["name"] == "Office with a bell"
    assert out["rooms"][1]["color"] == "#2f4a33"
    assert out["devices"]["office-1"] == {"x": -6.75, "z": -4.25, "room": "office"}
    assert out["devices"]["hallway-cam"] == {"x": 0.5, "z": -5.9, "y": 0.0, "yaw": -30.0}
    long = {**PLAN, "rooms": [{**PLAN["rooms"][0], "name": "x" * 500}]}
    assert len(home.validate(long)["rooms"][0]["name"]) == 48


@pytest.mark.parametrize("change", [
    lambda p: p.update(extra=1),
    lambda p: p.update(version=2),
    lambda p: p["rooms"].append(dict(p["rooms"][0])),                      # the same room id twice
    lambda p: p["rooms"][0].update(id="Office"),                          # not a short id
    lambda p: p["rooms"][0].update(id="<script>"),
    lambda p: p["rooms"][0].update(name=""),
    lambda p: p["rooms"][0].update(name=7),
    lambda p: p["rooms"][0].update(x=math.nan),
    lambda p: p["rooms"][0].update(x=math.inf),
    lambda p: p["rooms"][0].update(x=True),
    lambda p: p["rooms"][0].update(x=1e9),
    lambda p: p["rooms"][0].update(w=0),
    lambda p: p["rooms"][0].update(color="red"),
    lambda p: p["rooms"][0].update(color="#12345g"),
    lambda p: p["rooms"][0].update(outside="yes"),
    lambda p: p["rooms"][0].update(script="x"),
    lambda p: p.update(rooms={"office": {}}),
    lambda p: p.update(rooms=[{**p["rooms"][0], "id": f"r{i}"} for i in range(65)]),
    lambda p: p["devices"].update({"Bad Id": {"x": 0, "z": 0}}),
    lambda p: p["devices"].update({"lamp": {"x": 0}}),
    lambda p: p["devices"].update({"lamp": {"x": 0, "z": 0, "room": "attic"}}),
    lambda p: p["devices"].update({"lamp": {"x": 0, "z": 0, "onclick": "x"}}),
    lambda p: p["devices"].update({"lamp": {"x": 0, "z": 0, "y": 50}}),
    lambda p: p["devices"].update({"lamp": {"x": 0, "z": 0, "yaw": 720}}),
    lambda p: p["devices"].update({"lamp": [0, 0]}),
    lambda p: p.update(devices=[]),
])
def test_a_bad_plan_is_refused(change):
    plan = json.loads(json.dumps(PLAN))
    change(plan)
    with pytest.raises(home.LayoutError):
        home.validate(plan)


def test_refusals_never_repeat_the_value():
    plan = json.loads(json.dumps(PLAN))
    plan["rooms"][0]["id"] = "<img src=x onerror=alert(1)>"
    with pytest.raises(home.LayoutError) as err:
        home.validate(plan)
    assert "<img" not in str(err.value)


def test_save_and_load_keep_the_file_private(tmp_path):
    path = tmp_path / "conf" / "home.json"
    assert home.load(str(path)) is None
    saved = home.save(str(path), PLAN)
    assert home.load(str(path)) == saved
    assert path.stat().st_mode & 0o777 == 0o600
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".home-")]  # no temporary file left behind


def test_save_refuses_to_write_through_a_symbolic_link(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    link = tmp_path / "home.json"
    os.symlink(target, link)
    with pytest.raises(home.LayoutError, match="symbolic link"):
        home.save(str(link), PLAN)
    assert target.read_text() == "{}"


def test_a_bad_plan_is_never_written(tmp_path):
    path = tmp_path / "home.json"
    home.save(str(path), PLAN)
    before = path.read_text()
    with pytest.raises(home.LayoutError):
        home.save(str(path), {**PLAN, "rooms": "nope"})
    assert path.read_text() == before


def test_a_damaged_file_is_reported(tmp_path):
    path = tmp_path / "home.json"
    path.write_text("{not json")
    with pytest.raises(home.LayoutError, match="not valid JSON"):
        home.load(str(path))


# ---- the routes ----------------------------------------------------------------------------------------------------------

def test_the_page_may_run_only_its_own_code_and_pinned_three_js(build, tmp_path):
    client, _ = build(home_layout=str(tmp_path / "home.json"))
    page = client.get("/home")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    policy = page.headers["content-security-policy"]
    importmap = re.search(r'<script type="importmap">(.*?)</script>', page.text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(importmap.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in policy
    assert "script-src 'self' https://cdn.jsdelivr.net/npm/three@0.170.0/ " in policy
    assert "connect-src 'self'" in policy and "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    # every outside address the page names is the pinned three.js path
    outside = set(re.findall(r'https?://[^"\s]+', page.text))
    assert outside and all(url.startswith("https://cdn.jsdelivr.net/npm/three@0.170.0/") for url in outside)
    assert page.headers["x-content-type-options"] == "nosniff"
    script = client.get("/home/home.js")
    assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")
    assert client.get("/home/home.css").headers["content-type"].startswith("text/css")


def test_the_script_never_writes_device_text_as_markup(build, tmp_path):
    client, _ = build(home_layout=str(tmp_path / "home.json"))
    script = client.get("/home/home.js").text
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script and "outerHTML" not in script
    # the page talks to this server only
    assert not re.search(r"fetch\(\s*['\"`]https?:", script)


def test_layout_starts_empty_then_round_trips(build, tmp_path):
    path = tmp_path / "home.json"
    client, _ = build(home_layout=str(path))
    assert client.get("/home/layout").json() == {"version": 1, "rooms": [], "devices": {}}
    saved = client.put("/home/layout", json=PLAN)
    assert saved.status_code == 200
    assert client.get("/home/layout").json() == saved.json()
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_bad_layout_is_refused_and_the_file_kept(build, tmp_path):
    path = tmp_path / "home.json"
    client, _ = build(home_layout=str(path))
    client.put("/home/layout", json=PLAN)
    before = path.read_text()
    bad = client.put("/home/layout", json={**PLAN, "rooms": [{"id": "<b>", "name": "x", "x": 0, "z": 0, "w": 1, "d": 1}]})
    assert bad.status_code == 422 and "<b>" not in bad.text
    assert path.read_text() == before
    path.write_text("[1, 2")
    assert client.get("/home/layout").status_code == 422


def test_the_plan_needs_the_token_when_one_is_set(build, tmp_path):
    client, _ = build(token="s3cret", home_layout=str(tmp_path / "home.json"))
    assert client.get("/home").status_code == 200  # the page itself holds no data
    assert client.get("/home/layout").status_code == 401
    assert client.put("/home/layout", json=PLAN).status_code == 401
    ok = {"Authorization": "Bearer s3cret"}
    assert client.put("/home/layout", json=PLAN, headers=ok).status_code == 200
    assert client.get("/home/layout", headers=ok).json()["devices"]["office-1"]["room"] == "office"
