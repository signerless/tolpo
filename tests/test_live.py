"""Live video: JPEG pictures one after the other from a camera's stream, through the registry and the HTTP API."""

import os
import time

import pytest

from tolpo import registry, snapshot
from tolpo.registry import Busy, NotEnabled

from . import fakes
from .test_pictures import PASSWORD, SETTINGS, house


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("FAKE_FFMPEG_MODE", raising=False)


@pytest.mark.anyio
async def test_pictures_keep_coming_and_ffmpeg_stops_with_the_viewer(ffmpeg, tmp_path, monkeypatch):
    ffmpeg.mode("live")
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("FAKE_FFMPEG_PID", str(pidfile))
    frames = snapshot.live_jpegs("rtsp://192.0.2.10:554/stream2", seconds=30, ffmpeg=ffmpeg.path)
    assert [await frames.__anext__() for _ in range(3)] == [fakes.TINY_JPEG] * 3
    await frames.aclose()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)  # ffmpeg is gone as soon as nobody watches


@pytest.mark.anyio
async def test_a_live_view_ends_at_its_time_limit(ffmpeg):
    ffmpeg.mode("live")
    started, count = time.monotonic(), 0
    async for _ in snapshot.live_jpegs("rtsp://192.0.2.10:554/stream2", seconds=0.4, ffmpeg=ffmpeg.path):
        count += 1
    assert count >= 1 and time.monotonic() - started < 4


@pytest.mark.anyio
async def test_a_camera_without_video_is_a_clear_error(ffmpeg):
    ffmpeg.mode("empty")
    with pytest.raises(snapshot.GrabError):
        await snapshot.live_jpegs("rtsp://192.0.2.10:554/stream2", seconds=5, ffmpeg=ffmpeg.path).__anext__()
    ffmpeg.mode("hang")
    with pytest.raises(snapshot.GrabError, match="no video"):
        await snapshot.live_jpegs("rtsp://192.0.2.10:554/stream2", seconds=5, ffmpeg=ffmpeg.path, timeout=0.5).__anext__()


@pytest.mark.anyio
async def test_one_live_view_per_camera_and_the_camera_is_freed_after(ffmpeg):
    ffmpeg.mode("live")
    reg, _ = house(ffmpeg)
    cam = reg.resolve_one("hallway-cam")
    first = await reg.live_pictures(cam)
    assert await first.__anext__() == fakes.TINY_JPEG
    with pytest.raises(Busy):
        await reg.live_pictures(cam)
    await first.aclose()
    second = await reg.live_pictures(cam)
    assert await second.__anext__() == fakes.TINY_JPEG
    await second.aclose()
    assert not cam.picture_lock.locked() and not reg.lives.locked()


@pytest.mark.anyio
async def test_live_video_follows_the_picture_opt_in(ffmpeg):
    reg, _ = house(ffmpeg, snapshots=())
    with pytest.raises(NotEnabled):
        await reg.live_pictures(reg.resolve_one("hallway-cam"))
    assert ffmpeg.calls() == []


@pytest.mark.anyio
async def test_a_failed_start_frees_the_camera(ffmpeg):
    ffmpeg.mode("empty")
    reg, _ = house(ffmpeg)
    cam = reg.resolve_one("hallway-cam")
    with pytest.raises(registry.DeviceUnavailable):
        await reg.live_pictures(cam)
    assert not cam.picture_lock.locked() and not reg.lives.locked()


@pytest.mark.anyio
async def test_a_battery_camera_gets_the_short_limit(ffmpeg, monkeypatch):
    asked = []
    real = snapshot.live_jpegs

    def spy(*args, **kwargs):
        asked.append(kwargs["seconds"])
        return real(*args, **kwargs)

    monkeypatch.setattr("tolpo.registry.live_jpegs", spy)
    ffmpeg.mode("live")
    reg, _ = house(ffmpeg)
    cam = reg.resolve_one("hallway-cam")
    stream = await reg.live_pictures(cam)
    await stream.aclose()
    cam.device.features["battery_level"] = fakes.FakeFeature("battery_level", fakes.T.Sensor, fakes.C.Info, 80)
    stream = await reg.live_pictures(cam)
    await stream.aclose()
    assert asked == [registry.LIVE_SECONDS, registry.LIVE_BATTERY_SECONDS]


# ---- over HTTP ----------------------------------------------------------------------------------------------------------

def test_the_endpoint_streams_pictures_nobody_may_keep(build, ffmpeg):
    ffmpeg.mode("live")
    client, _ = build(ffmpeg=ffmpeg.path, **SETTINGS)
    with client.stream("GET", "/devices/hallway-cam/live") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("multipart/x-mixed-replace; boundary=frame")
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["cross-origin-resource-policy"] == "same-origin" and r.headers["x-content-type-options"] == "nosniff"
        data = b""
        for chunk in r.iter_bytes():
            data += chunk
            if data.count(b"--frame") >= 3:
                break
    assert b"Content-Type: image/jpeg\r\nContent-Length: " + str(len(fakes.TINY_JPEG)).encode() in data
    assert fakes.TINY_JPEG in data


def test_the_endpoint_says_no_like_the_picture_endpoint(build, ffmpeg):
    client, _ = build(ffmpeg=ffmpeg.path, camera_username="cam-login", camera_password=PASSWORD, snapshots=())
    denied = client.get("/devices/hallway-cam/live")
    assert denied.status_code == 403 and PASSWORD not in denied.text
    client, _ = build(ffmpeg=ffmpeg.path, **SETTINGS)
    assert client.get("/devices/office-1/live").status_code == 400
    assert client.get("/devices/hallway-cam/live", params={"quality": "ultra"}).status_code == 422
    client, _ = build(token="s3cret", ffmpeg=ffmpeg.path, **SETTINGS)
    assert client.get("/devices/hallway-cam/live").status_code == 401
    assert ffmpeg.calls() == []
