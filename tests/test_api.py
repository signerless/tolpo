from agentic_home.api import create_app
from agentic_home.config import Config
from agentic_home.registry import Registry
from fastapi.testclient import TestClient

from . import fakes


def by_id(client):
    return {d["id"]: d for d in client.get("/devices").json()}


def test_lists_everything_with_hub_children(client):
    devices = by_id(client)
    assert {"office-1", "office-2", "desk-lamp", "tapo-h200", "main-door", "temp-sensor", "hallway-cam"} <= devices.keys()
    assert devices["main-door"]["parent"] == "tapo-h200"
    assert devices["office-1"]["capabilities"] == ["power", "brightness", "color_temp", "color"]
    assert devices["desk-lamp"]["capabilities"] == ["power", "energy"]
    assert devices["temp-sensor"]["readings"] == {"temperature": 21.5, "humidity": 48}
    assert devices["temp-sensor"]["units"]["temperature"] == "celsius"
    assert devices["main-door"]["battery"] == 80
    assert devices["hallway-cam"]["capabilities"] == []  # cameras are read-only by default


def test_filters(client):
    assert {d["id"] for d in client.get("/devices", params={"kind": "sensor"}).json()} == {"main-door", "temp-sensor"}
    assert {d["id"] for d in client.get("/devices", params={"q": "office"}).json()} == {"office-1", "office-2"}


def test_secrets_never_leave(client):
    text = client.get("/devices").text + client.get("/devices/tapo-h200").text + client.get("/devices/office-1").text
    for secret in ("SECRET-123", "MT:SECRETPAYLOAD", "MyHomeWifi", "matter_setup", "ssid"):
        assert secret not in text


def test_lookup_by_name_slug_and_device_id(client):
    for ref in ("office-1", "Office 1", "OFFICE 1", "b1"):
        assert client.get(f"/devices/{ref}").json()["device_id"] == "b1"
    assert client.get("/devices/nope").status_code == 404


def test_power_roundtrip(build):
    client, devices = build()
    r = client.post("/devices/Office 1/power", json={"on": False})
    assert r.status_code == 200 and r.json()["on"] is False
    assert devices["office1"].features["state"].value is False
    assert client.post("/devices/desk-lamp/toggle").json()["on"] is True


def test_cameras_and_sensors_cannot_be_powered(client):
    assert client.post("/devices/hallway-cam/power", json={"on": False}).status_code == 403
    assert client.post("/devices/main-door/power", json={"on": True}).status_code in (403, 404)


def test_camera_state_can_be_unlocked_explicitly(build):
    client, devices = build(allow_write={"camera.state"})
    assert client.post("/devices/hallway-cam/power", json={"on": False}).status_code == 200
    assert devices["cam"].features["state"].value is False
    assert client.post("/devices/main-door/power", json={"on": True}).status_code in (403, 404)


def test_batch_with_glob(build):
    client, devices = build()
    r = client.post("/batch/power", json={"targets": ["office*", "nope"], "on": False}).json()
    assert {(i["id"], i["ok"]) for i in r if i["id"]} == {("office-1", True), ("office-2", True)}
    assert [i for i in r if i["target"] == "nope"][0]["ok"] is False
    assert devices["office1"].features["state"].value is False and devices["office2"].features["state"].value is False


def test_batch_reports_per_device_failures(build):
    client, _ = build()
    r = client.post("/batch/power", json={"targets": ["hallway-cam", "desk-lamp"], "on": True}).json()
    assert {i["id"]: i["ok"] for i in r} == {"hallway-cam": False, "desk-lamp": True}


def test_ambiguous_names_return_candidates(build):
    client, _ = build(extra=[fakes.bulb("b3", "Office 1")])
    r = client.get("/devices/office 1")
    assert r.status_code == 409 and set(r.json()["candidates"]) == {"office-1", "office-1-2"}
    assert client.get("/devices/office-1-2").json()["device_id"] == "b3"


def test_light_settings(build):
    client, devices = build()
    light = devices["office1"].modules["Light"]
    assert client.post("/devices/office-1/light", json={"brightness": 40}).status_code == 200
    assert client.post("/devices/office-1/light", json={"color_temp": 4000, "brightness": 60}).status_code == 200
    assert client.post("/devices/office-1/light", json={"hue": 120, "saturation": 80, "brightness": 50}).status_code == 200
    assert light.calls == [("brightness", 40), ("color_temp", 4000, 60), ("hsv", 120, 80, 50)]


def test_light_rejects_bad_input(client):
    post = lambda body, ref="office-1": client.post(f"/devices/{ref}/light", json=body).status_code
    assert post({"hue": 120}) == 400
    assert post({"color_temp": 4000, "hue": 1, "saturation": 1}) == 400
    assert post({"color_temp": 1500}) == 400  # below this bulb's range
    assert post({"on": False, "brightness": 20}) == 400
    assert post({}) == 400
    assert post({"brightness": 0}) == 422
    assert post({"brightness": 50}, "desk-lamp") == 400  # a plug is not a light


def test_feature_writes_are_deny_by_default(build):
    client, devices = build()
    put = lambda ref, fid, value=None: client.put(f"/devices/{ref}/features/{fid}", json={"value": value}).status_code
    assert put("office-1", "reboot") == 403
    assert put("tapo-h200", "test_alarm") == 403
    assert put("hallway-cam", "pan_left") == 403
    assert put("office-1", "ssid", "x") == 404
    assert put("office-1", "brightness", 30) == 200
    assert put("office-1", "brightness", 500) == 400
    assert put("office-1", "brightness", True) == 400
    assert put("office-1", "light_preset", "Nope") == 400
    assert put("office-1", "light_preset", "Warm") == 200
    assert devices["office1"].features["reboot"].calls == []


def test_allow_write_opens_only_what_is_listed(build):
    client, devices = build(allow_write={"reboot", "unpair", "pair"})
    put = lambda ref, fid: client.put(f"/devices/{ref}/features/{fid}", json={}).status_code
    assert put("office-1", "reboot") == 200 and devices["office1"].features["reboot"].calls == [None]
    assert put("hallway-cam", "pan_left") == 403  # still not listed
    assert put("main-door", "unpair") == 403  # never writable
    assert put("tapo-h200", "pair") == 403


def test_token_is_enforced(build):
    client, _ = build(token="s3cret")
    assert client.get("/health").status_code == 200
    assert client.get("/devices").status_code == 401
    assert client.get("/devices", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/devices", headers={"Authorization": "Basic s3cret"}).status_code == 401
    assert client.get("/devices", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.post("/batch/power", json={"targets": ["*"], "on": False}).status_code == 401


def test_foreign_host_header_is_refused_without_token():
    cfg = Config(email="a", password="b")
    reg = Registry(cfg)
    reg._adopt(fakes.bulb("b1", "Office 1"))
    with TestClient(create_app(cfg, reg, discover=False), base_url="http://localhost") as c:
        assert c.get("/devices").status_code == 200
        assert c.get("/devices", headers={"host": "evil.example"}).status_code == 400
