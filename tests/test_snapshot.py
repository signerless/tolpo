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


def test_the_clip_folder_comes_from_the_file_and_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[tapo]\nemail="a@x.com"\npassword="p"\n[cameras]\nclip_dir="/var/clips"\n')
    path.chmod(0o600)
    monkeypatch.delenv("TOLPO_CLIP_DIR", raising=False)
    assert load_config(str(path)).clip_dir == "/var/clips"
    monkeypatch.setenv("TOLPO_CLIP_DIR", "/elsewhere")
    assert load_config(str(path)).clip_dir == "/elsewhere"
    assert load_config(str(tmp_path / "missing.toml")).clip_dir == "/elsewhere"
    monkeypatch.delenv("TOLPO_CLIP_DIR")
    assert load_config(str(tmp_path / "missing.toml")).clip_dir == ""  # no folder, no clips


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


# ---- several frames, and pictures that arrive coded --------------------------------------------------------


def test_several_jpegs_in_a_row_are_cut_apart_and_leftovers_refused():
    jpeg = fakes.TINY_JPEG
    assert snapshot.split_jpegs(jpeg * 3) == [jpeg] * 3
    assert snapshot.split_jpegs(b"") == []
    for broken in (jpeg + jpeg[:50], b"junk" + jpeg, jpeg[:-2]):
        with pytest.raises(ValueError):
            snapshot.split_jpegs(broken)


def test_the_frames_command_asks_for_a_rate_and_a_count():
    args = snapshot.command("ffmpeg", "rtsp://x", frames=4, fps=0.5, max_width=640)
    assert args[args.index("-frames:v") + 1] == "4" and args[args.index("-vf") + 1] == "fps=0.5000,scale='min(640,iw)':-2"
    assert snapshot.command("ffmpeg", "rtsp://x")[-12:] == snapshot.command("ffmpeg", "rtsp://x", frames=1)[-12:]


@pytest.mark.anyio
async def test_a_coded_picture_goes_through_ffmpeg_and_a_jpeg_does_not(ffmpeg):
    picture = await snapshot.decode_still(fakes.TINY_JPEG, "jpeg", ffmpeg=ffmpeg.path)
    assert (picture.width, picture.height) == (16, 16) and ffmpeg.calls() == []
    picture = await snapshot.decode_still(b"\x00\x00\x00\x01\x65" + b"\x01" * 300, "hevc", ffmpeg=ffmpeg.path)
    (args,) = ffmpeg.calls()
    assert picture.data == fakes.TINY_JPEG and args[args.index("-f") + 1] == "hevc"
    for data, fmt in ((b"not a jpeg", "jpeg"), (fakes.TINY_JPEG, "webp")):
        with pytest.raises(snapshot.GrabError) as caught:
            await snapshot.decode_still(data, fmt, ffmpeg=ffmpeg.path)
        assert caught.value.reason == "no_picture"


@pytest.mark.anyio
async def test_frames_from_an_rtsp_address_come_back_as_pictures(ffmpeg):
    pictures = await snapshot.grab_frames("rtsp://u:p@192.0.2.10:554/stream2", count=1, seconds=1, ffmpeg=ffmpeg.path)
    assert [p.data for p in pictures] == [fakes.TINY_JPEG]
    ffmpeg.mode("garbage")
    with pytest.raises(snapshot.GrabError) as caught:
        await snapshot.grab_frames("rtsp://u:p@192.0.2.10:554/stream2", count=2, seconds=1, ffmpeg=ffmpeg.path)
    assert caught.value.reason == "no_picture"


@pytest.mark.anyio
@pytest.mark.parametrize("count, seconds", [(0, 3), (snapshot.MAX_FRAMES + 1, 3), (3, 0), (3, snapshot.MAX_SPAN + 1)])
async def test_frames_stay_within_their_bounds(ffmpeg, count, seconds):
    with pytest.raises(ValueError):
        await snapshot.grab_frames("rtsp://x", count=count, seconds=seconds, ffmpeg=ffmpeg.path)
    assert ffmpeg.calls() == []


@pytest.mark.anyio
async def test_real_ffmpeg_takes_frames_at_the_asked_rate(real_ffmpeg, tmp_path):
    clip = tmp_path / "clip.ts"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-t", "4",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-f", "mpegts", str(clip)],
        check=True,
    )
    pictures = await snapshot.grab_frames(str(clip), count=3, seconds=2, input_args=(), timeout=20)
    assert len(pictures) == 3 and all((p.width, p.height) == (320, 240) for p in pictures)


# ---- the three survivors of the first mutation run, and their twins in grab_frames ---------------------------------


class HeldFeed:
    """A feed that lets go of the camera only when it is closed. Cancelling a read does not end it, as with a reader
    that sits on a socket. `chunk` is what every read returns."""

    def __init__(self, chunk: bytes):
        self.chunk, self.closed = chunk, False

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        await asyncio.sleep(0.01)
        return self.chunk

    async def aclose(self) -> None:
        self.closed = True


NO_VIDEO = b"\x47\x1f\xff\x10" + b"\xff" * 184  # a padding packet: a transport stream that never names a video track


def coded_stream(kind: int) -> bytes:
    from .fake_camera import fake_ts

    return fake_ts(b"\x00\x00\x00\x01\x65" + b"\x11" * 3000, kind=kind)


@pytest.mark.anyio
@pytest.mark.parametrize("take", ["grab", "frames"])
async def test_a_stream_that_never_shows_video_lets_the_camera_go_on_timeout(ffmpeg, take):
    feed = HeldFeed(NO_VIDEO)
    with pytest.raises(snapshot.GrabError) as caught:
        if take == "grab":
            await snapshot.grab(feed=feed, ffmpeg=ffmpeg.path, timeout=1.0)
        else:
            await snapshot.grab_frames(feed=feed, count=2, seconds=1, ffmpeg=ffmpeg.path, timeout=1.0)
    assert caught.value.reason == "timeout" and feed.closed and ffmpeg.calls() == []


@pytest.mark.anyio
async def test_giving_up_while_waiting_for_video_lets_the_camera_go(ffmpeg):
    feed = HeldFeed(NO_VIDEO)
    task = asyncio.create_task(snapshot.grab(feed=feed, ffmpeg=ffmpeg.path, timeout=60))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert feed.closed


@pytest.mark.anyio
@pytest.mark.parametrize("take", ["grab", "frames"])
async def test_a_missing_ffmpeg_lets_the_camera_go_after_the_video_showed(tmp_path, take):
    feed = HeldFeed(coded_stream(0x1B))
    with pytest.raises(snapshot.GrabError) as caught:
        if take == "grab":
            await snapshot.grab(feed=feed, ffmpeg=str(tmp_path / "nope"))
        else:
            await snapshot.grab_frames(feed=feed, count=2, seconds=1, ffmpeg=str(tmp_path / "nope"))
    assert caught.value.reason == "missing" and feed.closed


@pytest.mark.anyio
async def test_frames_from_a_camera_that_sends_hevc_are_read_as_hevc(ffmpeg):
    feed = HeldFeed(coded_stream(0x24))
    pictures = await snapshot.grab_frames(feed=feed, count=1, seconds=1, ffmpeg=ffmpeg.path)
    (args,) = ffmpeg.calls()
    assert args[args.index("-f") + 1] == "hevc" and len(pictures) == 1 and feed.closed


@pytest.mark.anyio
async def test_frames_from_an_ffmpeg_that_fails_after_printing_a_picture_are_a_failure(ffmpeg):
    ffmpeg.mode("failafter")  # a whole JPEG on stdout, then an error and a nonzero exit
    with pytest.raises(snapshot.GrabError) as caught:
        await snapshot.grab_frames("rtsp://cam:pw@192.0.2.10:554/stream2", count=1, seconds=1, ffmpeg=ffmpeg.path)
    assert caught.value.reason == "failed" and "Invalid data" in caught.value.detail
