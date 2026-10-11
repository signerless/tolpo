"""The Tapo app's video channel (port 8800) against a fake camera. No real device is involved."""

import asyncio
import hashlib
import os
import shutil
import socket
import subprocess
import time

import pytest

from tolpo import media, snapshot

from . import fakes
from .fake_camera import FakeCamera, fake_ts, mpegts_with_unknown_track

PASSWORD = "My-Tapo-P@ssw0rd"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def real_ffmpeg():
    have = shutil.which("ffmpeg") is not None and b"libx264" in subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True).stdout
    if not have:
        if os.environ.get("CI"):
            pytest.fail("the CI job has to install ffmpeg with libx264, otherwise these tests would silently not run")
        pytest.skip("ffmpeg with libx264 is not installed")


def ts(n: int, fill: int = 0x47) -> bytes:
    """Stand-in for MPEG-TS: whole 188-byte packets that ffmpeg would see as a stream."""
    return bytes([fill]) * 188 * n


async def collect(feed, limit):
    got = []
    async for chunk in feed:
        got.append(chunk)
        if len(got) == limit:
            break
    await feed.aclose()
    return got


# ---- the maths -------------------------------------------------------------------------------


def test_the_digest_matches_the_example_in_the_rfc():
    """RFC 2617 section 3.5: the same answer any correct Digest client gives."""
    challenge = 'Digest realm="testrealm@host.com", qop="auth,auth-int", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"'
    header = media.digest_header(challenge, "Circle Of Life", username="Mufasa", method="GET", uri="/dir/index.html", cnonce="0a4f113b")
    assert 'response="6629fae49393a05397450978507c4ef1"' in header and 'opaque="5ccc069c403ebaf9f0171e9517f40e41"' in header


def test_the_hash_of_the_password_follows_the_challenge():
    new = 'Digest realm="TP-Link IP-Camera",algorithm="MD5",encrypt_type="3",qop="auth"'
    old = 'Digest realm="TP-Link IP-Camera",algorithm="MD5",qop="auth"'
    assert media.hashed_password("secret", new) == hashlib.sha256(b"secret").hexdigest().upper()
    assert media.hashed_password("secret", old) == hashlib.md5(b"secret").hexdigest().upper()


def test_the_decryptor_reads_what_the_camera_sends_and_rejects_the_rest():
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    hashed = media.hashed_password("secret", 'encrypt_type="3"')
    key, iv = hashlib.md5(f"n0nce:{hashed}".encode()).digest(), hashlib.md5(b"admin:n0nce").digest()
    padder = padding.PKCS7(128).padder()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    sealed = encryptor.update(padder.update(b"video bytes") + padder.finalize()) + encryptor.finalize()
    decrypt = media.Decryptor("admin", "n0nce", hashed)
    assert decrypt(sealed) == decrypt(sealed) == b"video bytes"  # every part starts again from the same IV
    with pytest.raises(ValueError):
        media.Decryptor("admin", "n0nce", media.hashed_password("wrong", 'encrypt_type="3"'))(sealed)
    with pytest.raises(ValueError):
        decrypt(sealed[:-3])
    with pytest.raises(ValueError):
        decrypt(b"")


# ---- the conversation ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_stream_arrives_decrypted_and_the_login_happens_once():
    chunks = [ts(2, 0x11), ts(3, 0x22), ts(1, 0x33)]
    async with FakeCamera(PASSWORD, chunks) as camera:
        got = await collect(media.live_chunks("127.0.0.1", PASSWORD, "high", port=camera.port), 3)
        await asyncio.wait_for(camera.disconnected.wait(), 5)  # closing the feed hangs up on the camera
    assert got == chunks
    assert (camera.logins, camera.failed_logins) == (1, 0)
    (request,) = camera.requests
    assert request["params"]["preview"]["resolutions"] == ["HD"] and request["params"]["preview"]["channels"] == [0]
    assert request["params"]["method"] == "get" and request["type"] == "request"


@pytest.mark.anyio
async def test_the_small_stream_is_asked_for_by_name():
    async with FakeCamera(PASSWORD, [ts(1)]) as camera:
        await collect(media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port), 1)
    assert camera.requests[0]["params"]["preview"]["resolutions"] == ["VGA"]


@pytest.mark.anyio
async def test_the_older_hash_is_used_when_the_camera_asks_for_it():
    async with FakeCamera(PASSWORD, [ts(1)], encrypt_type="") as camera:
        assert await collect(media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port), 1) == [ts(1)]
    assert camera.logins == 1


@pytest.mark.anyio
async def test_parts_that_are_not_video_are_skipped():
    async with FakeCamera(PASSWORD, [ts(1, 0x55)], extra_parts=[("audio/g711", b"sound"), ("application/json", b"{}")]) as camera:
        assert await collect(media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port), 1) == [ts(1, 0x55)]


@pytest.mark.anyio
async def test_a_wrong_password_is_reported_after_one_try():
    async with FakeCamera(PASSWORD, [ts(1)]) as camera:
        with pytest.raises(snapshot.GrabError) as caught:
            await collect(media.live_chunks("127.0.0.1", "not-the-password", "low", port=camera.port), 1)
    assert caught.value.reason == "auth" and "rejected" in str(caught.value)
    assert (camera.logins, camera.failed_logins) == (0, 1)  # one failed login, no retry
    assert "not-the-password" not in str(caught.value)


@pytest.mark.anyio
async def test_an_unencrypted_session_is_refused():
    """A camera can offer a session without a real key. tolpo only talks to cameras over the encrypted one."""
    async with FakeCamera(PASSWORD, [ts(1)], exchange_user="none") as camera:
        with pytest.raises(snapshot.GrabError) as caught:
            await collect(media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port), 1)
    assert caught.value.reason == "failed" and "unencrypted" in str(caught.value)


@pytest.mark.anyio
async def test_a_missing_key_exchange_is_reported():
    async with FakeCamera(PASSWORD, [ts(1)], send_key_exchange=False) as camera:
        with pytest.raises(snapshot.GrabError) as caught:
            await collect(media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port), 1)
    assert caught.value.reason == "failed" and "encrypted" in str(caught.value)


@pytest.mark.anyio
async def test_a_camera_that_does_not_ask_for_a_login_is_not_trusted():
    async with FakeCamera(PASSWORD, [], answer=b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n") as camera:
        with pytest.raises(snapshot.GrabError) as caught:
            await collect(media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port), 1)
    assert caught.value.reason == "failed" and "200" in str(caught.value)


@pytest.mark.anyio
async def test_something_that_is_not_http_is_reported():
    async with FakeCamera(PASSWORD, [], answer=b"SSH-2.0-OpenSSH\r\n\r\n") as camera:
        with pytest.raises(snapshot.GrabError) as caught:
            await collect(media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port), 1)
    assert caught.value.reason == "failed"


@pytest.mark.anyio
async def test_a_closed_port_means_the_camera_is_asleep_or_gone():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # nothing listens here once the socket is closed
    with pytest.raises(snapshot.GrabError) as caught:
        await collect(media.live_chunks("127.0.0.1", PASSWORD, "low", port=port), 1)
    assert caught.value.reason == "refused"


@pytest.mark.anyio
async def test_a_camera_that_goes_quiet_is_given_up_on():
    async with FakeCamera(PASSWORD, [], silent=True) as camera:
        with pytest.raises(snapshot.GrabError) as caught:
            await collect(media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port, timeout=0.5), 1)
    assert caught.value.reason == "unreachable"


@pytest.mark.anyio
async def test_a_part_of_an_impossible_size_is_refused():
    async with FakeCamera(PASSWORD, [ts(1)], part_size=media.MAX_PART + 1) as camera:
        with pytest.raises(snapshot.GrabError) as caught:
            await collect(media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port), 1)
    assert caught.value.reason == "failed" and "size" in str(caught.value)


@pytest.mark.anyio
async def test_a_stream_that_cannot_be_decrypted_blames_the_password_not_the_video():
    async with FakeCamera(PASSWORD, [ts(1)], stream_password="something else") as camera:
        with pytest.raises(snapshot.GrabError) as caught:
            await collect(media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port), 1)
    assert caught.value.reason == "failed" and "decrypted" in str(caught.value)


# ---- into ffmpeg -----------------------------------------------------------------------------


VIDEO = b"\x00\x00\x00\x01\x65" + b"\x11" * 3000  # looks like the start of an H.264 picture; the fake ffmpeg does not decode it


@pytest.mark.anyio
async def test_ffmpeg_gets_the_raw_video_and_not_the_transport_stream(ffmpeg):
    async with FakeCamera(PASSWORD, [fake_ts(VIDEO)] * 3) as camera:
        feed = media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port)
        picture = await snapshot.grab(feed=feed, ffmpeg=ffmpeg.path, secrets=(PASSWORD,))
        await asyncio.wait_for(camera.disconnected.wait(), 5)  # ffmpeg left early and the camera was let go
    assert picture.data == fakes.TINY_JPEG
    (args,) = ffmpeg.calls()
    assert args[args.index("-i") + 1] == "pipe:0" and args[args.index("-f") + 1] == "h264"
    assert ffmpeg.stdin_heads()[0].startswith("0000000165")  # the video itself, with no transport stream around it


@pytest.mark.anyio
async def test_the_cameras_own_complaint_beats_ffmpegs(ffmpeg):
    ffmpeg.mode("empty")  # ffmpeg alone would only say that nothing came out
    async with FakeCamera(PASSWORD, [fake_ts(VIDEO)]) as camera:
        feed = media.live_chunks("127.0.0.1", "wrong", "low", port=camera.port)
        with pytest.raises(snapshot.GrabError) as caught:
            await snapshot.grab(feed=feed, ffmpeg=ffmpeg.path)
    assert caught.value.reason == "auth"


@pytest.mark.anyio
async def test_a_stream_that_breaks_after_it_started_is_blamed_on_the_camera_not_on_ffmpeg(ffmpeg):
    ffmpeg.mode("hang")  # ffmpeg waits for more video that never comes
    async with FakeCamera(PASSWORD, [fake_ts(VIDEO), fake_ts(VIDEO)], part_size=media.MAX_PART + 1, lie_from=1) as camera:
        feed = media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port)
        with pytest.raises(snapshot.GrabError) as caught:
            await snapshot.grab(feed=feed, ffmpeg=ffmpeg.path, timeout=3.0)
    assert caught.value.reason == "failed" and "size" in str(caught.value)


@pytest.mark.anyio
async def test_a_stream_without_video_is_given_up_on(ffmpeg):
    async with FakeCamera(PASSWORD, [ts(5)]) as camera:  # bytes that are no transport stream at all
        feed = media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port)
        with pytest.raises(snapshot.GrabError) as caught:
            await snapshot.grab(feed=feed, ffmpeg=ffmpeg.path, timeout=1.5)
        await asyncio.wait_for(camera.disconnected.wait(), 5)
    assert caught.value.reason == "timeout" and ffmpeg.calls() == []  # ffmpeg was never started


@pytest.mark.anyio
async def test_a_hung_ffmpeg_lets_the_camera_go(ffmpeg):
    ffmpeg.mode("hang")
    async with FakeCamera(PASSWORD, [fake_ts(VIDEO)]) as camera:
        feed = media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port)
        with pytest.raises(snapshot.GrabError) as caught:
            await snapshot.grab(feed=feed, ffmpeg=ffmpeg.path, timeout=2.5)
        await asyncio.wait_for(camera.disconnected.wait(), 5)
    assert caught.value.reason == "timeout"


@pytest.mark.anyio
async def test_a_missing_ffmpeg_still_lets_the_camera_go(tmp_path):
    async with FakeCamera(PASSWORD, [fake_ts(VIDEO)]) as camera:
        feed = media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port)
        with pytest.raises(snapshot.GrabError) as caught:
            await snapshot.grab(feed=feed, ffmpeg=str(tmp_path / "nope"))
        await asyncio.wait_for(camera.disconnected.wait(), 5)
    assert caught.value.reason == "missing"


@pytest.mark.anyio
async def test_a_real_picture_through_the_whole_chain(real_ffmpeg, tmp_path):
    """A real video clip, encrypted and served like the camera does, decoded by the real ffmpeg."""
    clip = tmp_path / "clip.ts"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=10", "-t", "2",
         "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-bf", "0", "-g", "10", "-pix_fmt", "yuv420p",
         "-f", "mpegts", str(clip)],
        check=True,
    )
    data = clip.read_bytes()
    chunks = [data[i : i + 188 * 20] for i in range(0, len(data), 188 * 20)]
    async with FakeCamera(PASSWORD, chunks) as camera:
        feed = media.live_chunks("127.0.0.1", PASSWORD, "high", port=camera.port)
        picture = await snapshot.grab(feed=feed, secrets=(PASSWORD,), timeout=20)
    assert (picture.width, picture.height) == (1280, 720) == snapshot.jpeg_size(picture.data)


@pytest.mark.anyio
async def test_a_second_track_nothing_can_name_does_not_stall_the_picture(real_ffmpeg):
    """Found on a real camera: it sends a private second track. A live feed never ends, so ffmpeg, left to guess what
    is in the pipe, waits for that track for minutes. The camera here keeps its connection open, like the real one."""
    clip = mpegts_with_unknown_track()
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "stream=codec_type", "-of", "compact", "-f", "mpegts", "-i", "pipe:0"],
        input=clip, capture_output=True,
    )
    assert b"codec_type=video" in probe.stdout and b"codec_type=unknown" in probe.stdout  # a track nothing can name
    chunks = [clip[i : i + 188 * 20] for i in range(0, len(clip), 188 * 20)]
    async with FakeCamera(PASSWORD, chunks) as camera:
        feed = media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port)
        started = time.monotonic()
        picture = await snapshot.grab(feed=feed, secrets=(PASSWORD,), timeout=15)
    assert (picture.width, picture.height) == (640, 360)
    assert time.monotonic() - started < 10


@pytest.mark.anyio
async def test_a_stream_that_starts_with_padding_still_gives_a_picture(real_ffmpeg):
    """Found on a second real camera: its stream begins with padding packets, before the tables ffmpeg looks for."""
    clip = mpegts_with_unknown_track()
    padded = b"\x47\x1f\xff\x10" + b"\xff" * 184
    stream = padded * 200 + clip
    chunks = [stream[i : i + 188 * 20] for i in range(0, len(stream), 188 * 20)]
    async with FakeCamera(PASSWORD, chunks) as camera:
        feed = media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port)
        picture = await snapshot.grab(feed=feed, secrets=(PASSWORD,), timeout=15)
    assert (picture.width, picture.height) == (640, 360)


@pytest.mark.anyio
@pytest.mark.parametrize("kind, name", [(0x1B, "h264"), (0x24, "hevc")])
async def test_ffmpeg_is_told_which_kind_of_video_the_tables_announce(kind, name):
    async def feed():
        yield fake_ts(VIDEO, kind=kind)

    fmt, raw = await snapshot.elementary_video(feed(), 5)
    assert fmt == name and [chunk async for chunk in raw] == [VIDEO]


@pytest.mark.anyio
@pytest.mark.parametrize("kind, name", [(0x1B, "h264"), (0x24, "hevc")])
async def test_ffmpeg_is_started_for_the_kind_of_video_the_stream_carries(ffmpeg, kind, name):
    async with FakeCamera(PASSWORD, [fake_ts(VIDEO, kind=kind)]) as camera:
        feed = media.live_chunks("127.0.0.1", PASSWORD, "low", port=camera.port)
        await snapshot.grab(feed=feed, ffmpeg=ffmpeg.path)
    (args,) = ffmpeg.calls()
    assert args[args.index("-f") + 1] == name
