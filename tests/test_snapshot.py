"""Taking a picture: the ffmpeg wrapper, the opt-in rule and the camera settings. No camera is involved."""

import asyncio
import os
import shutil
import subprocess
import time
from urllib.parse import unquote, urlsplit

import pytest

from tolpo import policy, snapshot
from tolpo.config import Config, load_config

from . import fakes


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("TOLPO_CAMERA_USERNAME", "TOLPO_CAMERA_PASSWORD", "TOLPO_SNAPSHOTS", "TOLPO_FFMPEG", "TOLPO_CONFIG",
                 "TAPO_EMAIL", "TAPO_PASSWORD", "TOLPO_CREDENTIALS_FILE", "FAKE_FFMPEG_MODE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def real_ffmpeg():
    have = shutil.which("ffmpeg") is not None and b"libx264" in subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True).stdout
    if not have:
        if os.environ.get("CI"):
            pytest.fail("the CI job has to install ffmpeg with libx264, otherwise these tests would silently not run")
        pytest.skip("ffmpeg with libx264 is not installed")


# ---- pure functions --------------------------------------------------------------------------


def test_jpeg_size_reads_a_real_picture():
    assert snapshot.jpeg_size(fakes.TINY_JPEG) == (16, 16)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x89PNG\r\n\x1a\n" + b"0" * 60,
        fakes.TINY_JPEG[:-2],  # cut off before the end marker
        fakes.TINY_JPEG[:60] + fakes.TINY_JPEG[-2:],  # the header, then nothing
        b"\xff\xd8\xff\xd9",
    ],
)
def test_jpeg_size_refuses_anything_else(data):
    assert snapshot.jpeg_size(data) is None


def test_rtsp_url_encodes_the_login():
    url = snapshot.rtsp_url("192.0.2.10", "cam user", "p@ss/w:rd+1", "low")
    assert url == "rtsp://cam%20user:p%40ss%2Fw%3Ard%2B1@192.0.2.10:554/stream2"
    parts = urlsplit(url)
    assert (unquote(parts.username), unquote(parts.password), parts.hostname, parts.port) == ("cam user", "p@ss/w:rd+1", "192.0.2.10", 554)


def test_rtsp_url_picks_the_stream():
    assert snapshot.rtsp_url("192.0.2.10", "u", "p", "low").endswith("/stream2")
    assert snapshot.rtsp_url("192.0.2.10", "u", "p", "high").endswith("/stream1")
    assert "@[2001:db8::1]:554/" in snapshot.rtsp_url("2001:db8::1", "u", "p", "low")


def test_the_ffmpeg_command_takes_one_frame_over_tcp_and_never_enlarges():
    argv = snapshot.command("ffmpeg", "rtsp://camera/stream1")
    assert argv[0] == "ffmpeg" and argv[-1] == "pipe:1"
    assert argv[argv.index("-rtsp_transport") + 1] == "tcp"
    assert argv[argv.index("-i") + 1] == "rtsp://camera/stream1"
    assert argv[argv.index("-frames:v") + 1] == "1" and "-an" in argv
    assert any("min(1280,iw)" in arg for arg in argv)
    assert argv.index("-rtsp_transport") < argv.index("-i")  # input options only count before the input


# ---- the opt-in rule -------------------------------------------------------------------------


def test_only_named_cameras_may_take_pictures():
    named = ("Hallway Cam",)
    assert policy.can_snapshot("camera", "Hallway Cam", named)
    assert policy.can_snapshot("camera", "  HALLWAY   cam ", named)  # case and spacing do not matter
    assert policy.can_snapshot("doorbell", "hallway cam", named)
    assert not policy.can_snapshot("camera", "Hallway Cam 2", named)
    assert not policy.can_snapshot("camera", "Hallway", named)  # no partial matches
    assert not policy.can_snapshot("camera", "Hallway Cam", ())  # off by default
    assert not policy.can_snapshot("light", "Hallway Cam", named)
    assert not policy.can_snapshot("sensor", "Hallway Cam", named)


def test_a_blank_name_never_matches_a_blank_entry():
    assert not policy.can_snapshot("camera", None, ("", "  "))
    assert not policy.can_snapshot("camera", "   ", ("", "  "))


# ---- settings --------------------------------------------------------------------------------


def test_camera_settings_come_from_the_file_and_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        '[tapo]\nemail="a@x.com"\npassword="p"\n'
        '[cameras]\nusername="cam"\npassword="camera-secret"\nsnapshots=["Hallway Cam", " ", "Garage"]\nffmpeg="/opt/ffmpeg"\n'
    )
    path.chmod(0o600)
    cfg = load_config(str(path))
    assert (cfg.camera_username, cfg.camera_password, cfg.ffmpeg) == ("cam", "camera-secret", "/opt/ffmpeg")
    assert cfg.snapshots == ("Hallway Cam", "Garage")  # blanks are dropped
    monkeypatch.setenv("TOLPO_SNAPSHOTS", "Porch, Back door")
    monkeypatch.setenv("TOLPO_CAMERA_PASSWORD", "from-env")
    cfg = load_config(str(path))
    assert cfg.snapshots == ("Porch", "Back door") and cfg.camera_password == "from-env"
    monkeypatch.setenv("TOLPO_SNAPSHOTS", "")
    assert load_config(str(path)).snapshots == ()  # an empty variable switches pictures off


def test_pictures_are_off_unless_configured(tmp_path):
    cfg = load_config(str(tmp_path / "missing.toml"))
    assert cfg.snapshots == () and cfg.camera_username == "" and cfg.camera_password == ""


def test_a_lone_string_is_one_camera_name_not_a_list_of_letters(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[cameras]\nsnapshots="Hallway Cam"\n')
    assert load_config(str(path)).snapshots == ("Hallway Cam",)


def test_the_camera_password_is_not_in_repr():
    cfg = Config(camera_username="cam-login", camera_password="camera-secret")
    assert "camera-secret" not in repr(cfg) and "cam-login" not in repr(cfg)


def test_a_config_file_with_a_camera_password_must_be_private(tmp_path, caplog):
    path = tmp_path / "config.toml"
    path.write_text('[cameras]\npassword="camera-secret"\n')
    path.chmod(0o644)
    load_config(str(path))
    assert "chmod 600" in caplog.text and "camera-secret" not in caplog.text


# ---- ffmpeg ----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_grab_returns_the_picture(ffmpeg):
    url = "rtsp://cam:pw@192.0.2.10:554/stream2"
    picture = await snapshot.grab(url, ffmpeg=ffmpeg.path, secrets=("pw",))
    assert (picture.width, picture.height, picture.data) == (16, 16, fakes.TINY_JPEG)
    assert abs(picture.taken_at - time.time()) < 5
    assert ffmpeg.urls() == [url]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mode, reason",
    [
        ("unauthorized", "auth"),
        ("notfound", "stream"),
        ("busy", "busy"),
        ("refused", "refused"),
        ("timeout", "unreachable"),
        ("crash", "failed"),
        ("garbage", "no_picture"),
        ("empty", "no_picture"),
    ],
)
async def test_grab_says_what_went_wrong_without_the_password(ffmpeg, mode, reason):
    ffmpeg.mode(mode)
    url = snapshot.rtsp_url("192.0.2.10", "cam", "p@ss/w:rd", "low")
    with pytest.raises(snapshot.GrabError) as caught:
        await snapshot.grab(url, ffmpeg=ffmpeg.path, secrets=("p@ss/w:rd",))
    error = caught.value
    assert error.reason == reason
    shown = f"{error} {error.detail}"
    for leak in ("p@ss/w:rd", "p%40ss%2Fw%3Ard", "cam:"):  # plain, encoded, and the login in front of the address
        assert leak not in shown
    if mode in ("unauthorized", "notfound", "busy", "refused", "timeout", "crash"):
        assert "rtsp://***@192.0.2.10:554/stream2" in error.detail  # the owner still sees where it failed


@pytest.mark.anyio
async def test_a_password_that_ffmpeg_prints_on_its_own_is_removed_too(tmp_path):
    chatty = tmp_path / "ffmpeg"
    chatty.write_text(f"#!{__import__('sys').executable}\nimport sys\nprint('login failed for hunter2', file=sys.stderr)\nsys.exit(1)\n")
    chatty.chmod(0o755)
    with pytest.raises(snapshot.GrabError) as caught:
        await snapshot.grab("rtsp://x", ffmpeg=str(chatty), secrets=("hunter2",))
    assert "hunter2" not in caught.value.detail and "login failed for ***" in caught.value.detail


@pytest.mark.anyio
async def test_an_ffmpeg_that_fails_after_printing_a_picture_is_still_a_failure(ffmpeg):
    ffmpeg.mode("failafter")
    with pytest.raises(snapshot.GrabError) as caught:
        await snapshot.grab("rtsp://cam:pw@192.0.2.10:554/stream2", ffmpeg=ffmpeg.path)
    assert caught.value.reason == "failed" and "Invalid data" in caught.value.detail


@pytest.mark.anyio
async def test_a_missing_ffmpeg_is_reported(tmp_path):
    with pytest.raises(snapshot.GrabError) as caught:
        await snapshot.grab("rtsp://x", ffmpeg=str(tmp_path / "nope"))
    assert caught.value.reason == "missing" and "not installed" in str(caught.value)


@pytest.mark.anyio
async def test_a_picture_that_is_too_large_is_refused(ffmpeg, monkeypatch):
    monkeypatch.setattr(snapshot, "MAX_BYTES", 100)
    with pytest.raises(snapshot.GrabError) as caught:
        await snapshot.grab("rtsp://x", ffmpeg=ffmpeg.path)
    assert caught.value.reason == "no_picture"


async def _wait_for(path, seconds=5.0):
    end = time.monotonic() + seconds
    while not (path.exists() and path.read_text()):
        assert time.monotonic() < end, "the fake ffmpeg never started"
        await asyncio.sleep(0.02)
    return int(path.read_text())


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.anyio
async def test_a_hanging_ffmpeg_is_stopped(ffmpeg, monkeypatch, tmp_path):
    ffmpeg.mode("hang")
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("FAKE_FFMPEG_PID", str(pidfile))
    with pytest.raises(snapshot.GrabError) as caught:
        await snapshot.grab("rtsp://x", ffmpeg=ffmpeg.path, timeout=2.0)
    assert caught.value.reason == "timeout"
    assert _gone(int(pidfile.read_text()))  # killed, not left holding a stream open


@pytest.mark.anyio
async def test_giving_up_stops_ffmpeg_too(ffmpeg, monkeypatch, tmp_path):
    ffmpeg.mode("hang")
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("FAKE_FFMPEG_PID", str(pidfile))
    task = asyncio.create_task(snapshot.grab("rtsp://x", ffmpeg=ffmpeg.path, timeout=60))
    pid = await _wait_for(pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _gone(pid)


# ---- the real ffmpeg, on a synthetic video instead of a camera -------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("size, expected", [("1920x1080", (1280, 720)), ("640x360", (640, 360))])
async def test_real_ffmpeg_scales_down_but_never_up(real_ffmpeg, size, expected):
    picture = await snapshot.grab(f"testsrc=size={size}:rate=5", input_args=("-f", "lavfi"))
    assert (picture.width, picture.height) == expected == snapshot.jpeg_size(picture.data)


@pytest.mark.anyio
async def test_real_ffmpeg_failure_names_no_password(real_ffmpeg):
    """Real ffmpeg repeats the whole address, login included, when it cannot connect. Nothing listens on port 1."""
    password = "Sup3r/S3cret@pw"
    url = snapshot.rtsp_url("127.0.0.1", "cam", password, "low").replace(":554/", ":1/")
    with pytest.raises(snapshot.GrabError) as caught:
        await snapshot.grab(url, secrets=(password,), timeout=15)
    error = caught.value
    assert error.reason == "refused"
    assert "Sup3r" not in f"{error} {error.detail}" and "cam:" not in error.detail
    assert "rtsp://***@127.0.0.1:1/stream2" in error.detail  # ffmpeg did echo the address, and the scrub caught it
