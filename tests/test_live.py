"""Live video: JPEG pictures one after the other from a camera's stream, through the registry and the HTTP API."""

import asyncio
import os
import time

import anyio
import pytest

from tolpo import registry, snapshot
from tolpo.registry import Busy, DeviceUnavailable, Forbidden, NotEnabled

from . import fakes
from .fake_camera import FakeCamera
from .test_pictures import PASSWORD, SETTINGS, STREAM, TAPO_PASSWORD, app_channel, house


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


@pytest.mark.anyio
@pytest.mark.parametrize("covered", [True, False])
async def test_the_video_starts_while_the_privacy_switch_is_read_and_waits_for_it(ffmpeg, tmp_path, monkeypatch, covered):
    ffmpeg.mode("live")
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("FAKE_FFMPEG_PID", str(pidfile))
    reg, devices = house(ffmpeg)
    cam = reg.resolve_one("hallway-cam")
    answer = asyncio.Event()

    async def slow_camera():
        await answer.wait()
        devices["cam"].features["state"].value = not covered

    devices["cam"].update = slow_camera
    starting = asyncio.create_task(reg.live_pictures(cam))
    for _ in range(300):
        if ffmpeg.calls():
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)  # time for a first picture, which must wait for the answer
    assert ffmpeg.calls() and not starting.done()
    answer.set()
    if covered:
        with pytest.raises(Forbidden, match="privacy"):
            await starting
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)  # the camera's video is closed again
    else:
        stream = await starting
        assert await stream.__anext__() == fakes.TINY_JPEG
        await stream.aclose()
    assert not cam.picture_lock.locked() and not reg.lives.locked()


@pytest.mark.anyio
async def test_a_camera_that_cannot_be_asked_about_its_lens_gives_no_live_video(ffmpeg):
    ffmpeg.mode("live")
    reg, devices = house(ffmpeg)
    cam = reg.resolve_one("hallway-cam")

    async def broken():
        raise TimeoutError

    devices["cam"].update = broken
    with pytest.raises(DeviceUnavailable, match="not reachable"):
        await reg.live_pictures(cam)
    assert not cam.picture_lock.locked() and not reg.lives.locked()


@pytest.mark.anyio
@pytest.mark.parametrize("camera", ["silent", "sending"])
async def test_a_failed_lens_check_frees_the_camera_whatever_its_video_does(ffmpeg, monkeypatch, camera):
    ffmpeg.mode("live")
    async with FakeCamera(TAPO_PASSWORD, [STREAM] * 50, silent=camera == "silent") as fake:
        app_channel(fake, monkeypatch)
        reg, devices = house(ffmpeg, camera_username="", camera_password="")
        cam = reg.resolve_one("hallway-cam")

        async def no_answer():
            await asyncio.sleep(0.3)
            raise TimeoutError

        devices["cam"].update = no_answer
        started = time.monotonic()
        with pytest.raises(DeviceUnavailable, match="not reachable"):
            await asyncio.wait_for(reg.live_pictures(cam), 15)
        assert time.monotonic() - started < 3
        assert not cam.picture_lock.locked() and not reg.lives.locked()


@pytest.mark.anyio
@pytest.mark.parametrize("video", ["rtsp", "app-silent", "app-sending"])
@pytest.mark.parametrize("leave_after", [0.05, 0.2, 0.45])
async def test_a_viewer_who_leaves_while_the_video_starts_frees_the_camera(ffmpeg, monkeypatch, leave_after, video):
    """The web server cancels a request whose viewer has left, at every await from then on (anyio cancel scopes)."""
    ffmpeg.mode("live")
    async with FakeCamera(TAPO_PASSWORD, [STREAM] * 50, silent=video == "app-silent") as fake:
        if video == "rtsp":
            reg, devices = house(ffmpeg)
        else:
            app_channel(fake, monkeypatch)
            reg, devices = house(ffmpeg, camera_username="", camera_password="")
        cam = reg.resolve_one("hallway-cam")

        async def slow_answer():
            await asyncio.sleep(0.3)

        devices["cam"].update = slow_answer
        with anyio.move_on_after(leave_after):
            stream = await reg.live_pictures(cam)
            await stream.aclose()
        for _ in range(250):  # the camera is free once its video has closed
            if not cam.picture_lock.locked() and not reg.lives.locked():
                break
            await asyncio.sleep(0.02)
        assert not cam.picture_lock.locked() and not reg.lives.locked()


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


@pytest.mark.anyio
async def test_a_picture_of_a_busy_camera_says_so_instead_of_waiting(ffmpeg, monkeypatch):
    monkeypatch.setattr(registry, "CAMERA_WAIT", 0.2)
    reg, _ = house(ffmpeg)
    cam = reg.resolve_one("hallway-cam")
    await cam.picture_lock.acquire()  # busy with a clip or a live view
    started = time.monotonic()
    with pytest.raises(Busy, match="busy"):
        await reg.snapshot(cam)
    assert time.monotonic() - started < 2
    cam.picture_lock.release()


@pytest.mark.anyio
@pytest.mark.parametrize("leaves", [False, True])
async def test_live_video_of_a_busy_camera_gives_its_slot_back(ffmpeg, monkeypatch, leaves):
    monkeypatch.setattr(registry, "CAMERA_WAIT", 0.3)
    reg, _ = house(ffmpeg)
    cam = reg.resolve_one("hallway-cam")
    await cam.picture_lock.acquire()
    if leaves:  # the viewer leaves while the request waits for the camera
        with anyio.move_on_after(0.1):
            await reg.live_pictures(cam)
    else:
        with pytest.raises(Busy):
            await reg.live_pictures(cam)
    cam.picture_lock.release()
    assert reg.lives._value == registry.LIVE_STREAMS and not cam.picture_lock.locked()


def test_a_web_page_on_another_site_cannot_wake_a_camera(build, ffmpeg):
    ffmpeg.mode("live")
    client, _ = build(ffmpeg=ffmpeg.path, **SETTINGS)
    res = client.get("/devices/hallway-cam/live", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "image"})
    assert res.status_code == 403 and ffmpeg.calls() == []
