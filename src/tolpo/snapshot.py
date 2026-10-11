"""Still pictures from cameras. ffmpeg decodes one frame of the camera's live video, either from its RTSP stream or
from the video that the Tapo app's channel carries, and returns a JPEG.

This module knows nothing about devices or policy. `registry.py` decides whether a picture may be taken.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import struct
import time
from dataclasses import dataclass
from typing import AsyncIterator, Sequence
from urllib.parse import quote, quote_plus

from .tsvideo import TsVideo

log = logging.getLogger(__name__)

RTSP_PORT = 554
STREAMS = {"low": "stream2", "high": "stream1"}  # the camera's small second stream and its main stream
QUALITIES = tuple(STREAMS)
MAX_WIDTH = 1280  # wider pictures are scaled down: they cost a model more and show it nothing extra
GRAB_TIMEOUT = 20.0
MAX_BYTES = 4_000_000
RTSP_INPUT = ("-rtsp_transport", "tcp")  # UDP drops packets on Wi-Fi and gives smeared frames
# Raw video from a pipe that never ends: stop probing as soon as the picture size is known, instead of reading on for
# seconds. Measured on two real cameras: a first picture after about 1 s instead of 3 to 4 s.
RAW_VIDEO_INPUT = ("-probesize", "32768", "-analyzeduration", "1")


class GrabError(Exception):
    """No picture. `str()` is safe to show an agent; `detail` is ffmpeg's output with the login removed,
    for the owner at a terminal, and is never sent anywhere else."""

    def __init__(self, reason: str, message: str, detail: str = ""):
        super().__init__(message)
        self.reason, self.detail = reason, detail


@dataclass(frozen=True)
class Picture:
    data: bytes
    width: int
    height: int
    taken_at: float  # seconds since the epoch


def rtsp_url(host: str, username: str, password: str, quality: str) -> str:
    """The camera's local stream. Both parts of the login are percent-encoded, so `@` or `/` in a password is safe."""
    where = f"[{host}]" if ":" in host else host
    return f"rtsp://{quote(username, safe='')}:{quote(password, safe='')}@{where}:{RTSP_PORT}/{STREAMS[quality]}"


def command(ffmpeg: str, source: str, input_args: Sequence[str] = RTSP_INPUT, max_width: int = MAX_WIDTH) -> list[str]:
    """No shell is involved and nothing in here comes from an agent: the source is built from a discovered address."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        *input_args, "-i", source,
        "-an", "-frames:v", "1",
        "-vf", f"scale='min({max_width},iw)':-2",  # never enlarge; -2 keeps the height even
        "-q:v", "5", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
    ]


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from the first start-of-frame marker, or None when `data` is not a whole JPEG."""
    if data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # padding
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # markers without a length
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # a start-of-frame marker
            if i + 9 > len(data):
                return None
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return (width, height) if width and height else None
        i += 2 + length
    return None


def scrub(text: str, secrets: Sequence[str]) -> str:
    """ffmpeg repeats the whole URL, password included, in its error messages."""
    text = re.sub(r"(rtsps?://)[^@\s/]*@", r"\1***@", text)
    for secret in secrets:
        if secret:
            for form in {secret, quote(secret, safe=""), quote_plus(secret)}:
                text = text.replace(form, "***")
    return text


# What went wrong, from ffmpeg's last words. The first pattern that matches wins.
_CAUSES = (
    ("auth", r"\b40[13]\b|unauthori[sz]ed|forbidden", "the camera rejected the camera account, so the username or password is wrong"),
    ("stream", r"\b404\b", "the camera has no video stream at the expected address"),
    ("busy", r"\b453\b|\b503\b|not enough bandwidth|service unavailable", "the camera is busy with other video connections"),
    ("refused", r"connection refused", "the camera refused the video connection; it may be asleep, or it has no camera account set up"),
    ("unreachable", r"timed out|timeout|no route to host|unreachable|host is down", "the camera did not answer on the network"),
)


def _explain(stderr: str) -> tuple[str, str]:
    low = stderr.lower()
    for reason, pattern, message in _CAUSES:
        if re.search(pattern, low):
            return reason, message
    return "failed", "ffmpeg could not read a picture from the camera"


async def _stop(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


async def pump(feed: AsyncIterator[bytes], stdin: asyncio.StreamWriter) -> GrabError | None:
    """Hand the camera's video to ffmpeg until ffmpeg has what it needs. Returns the camera's own complaint, if any."""
    try:
        async for chunk in feed:
            stdin.write(chunk)
            await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        return None  # ffmpeg took its frame and left
    except GrabError as exc:
        return exc
    finally:
        with contextlib.suppress(Exception):
            stdin.close()
        with contextlib.suppress(Exception):
            await feed.aclose()
    return None


async def _release(proc: asyncio.subprocess.Process, pumping: asyncio.Task | None) -> GrabError | None:
    """Stop ffmpeg and the video feed, whatever state they are in, and return what the feed complained about."""
    await _stop(proc)
    if pumping is None:
        return None
    pumping.cancel()
    result = await asyncio.gather(pumping, return_exceptions=True)
    return result[0] if isinstance(result[0], GrabError) else None


async def _run(
    source: str,
    feed: AsyncIterator[bytes] | None,
    input_args: Sequence[str],
    *,
    ffmpeg: str,
    max_width: int,
    timeout: float,
    secrets: Sequence[str],
) -> Picture:
    """Start ffmpeg on `source`, or on `feed` through its stdin, and return the first frame it decodes."""
    if feed is not None:
        source = "pipe:0"
    try:
        proc = await asyncio.create_subprocess_exec(
            *command(ffmpeg, source, input_args, max_width),
            stdin=asyncio.subprocess.PIPE if feed is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:  # not installed, not executable, not a file
        if feed is not None:
            await feed.aclose()
        raise GrabError("missing", "ffmpeg is not installed on the machine that runs tolpo") from None

    pumping = asyncio.create_task(pump(feed, proc.stdin)) if feed is not None else None
    try:
        out, err, _ = await asyncio.wait_for(asyncio.gather(proc.stdout.read(), proc.stderr.read(), proc.wait()), timeout)
    except TimeoutError:
        failure = await _release(proc, pumping)
        raise failure or GrabError("timeout", f"the camera sent no picture within {timeout:.0f} seconds") from None
    except BaseException:  # the caller gave up: do not leave ffmpeg or the camera connection open
        await _release(proc, pumping)
        raise
    failure = await _release(proc, pumping)

    detail = scrub(err.decode("utf-8", "replace"), secrets).strip()[-1500:]
    size = jpeg_size(out) if proc.returncode == 0 else None
    if size is None or len(out) > MAX_BYTES:
        if failure is not None:  # the camera said why; that beats what ffmpeg made of an empty input
            raise failure
        if proc.returncode != 0:
            reason, message = _explain(detail)
            raise GrabError(reason, message, detail)
        raise GrabError("no_picture", "the camera connected but no usable picture came out of the stream", detail)
    return Picture(out, size[0], size[1], time.time())


async def elementary_video(feed: AsyncIterator[bytes], timeout: float) -> tuple[str, AsyncIterator[bytes]]:
    """Read the camera's transport stream until its tables name the video track. Returns the raw video format and
    an iterator over the raw video, which carries on from the same connection. Closes the feed if it fails."""
    video, first = TsVideo(), b""

    async def find() -> None:
        nonlocal first
        async for chunk in feed:
            first = video.feed(chunk)
            if video.format:
                return
        raise GrabError("no_picture", "the camera's stream ended before it showed any video")

    try:
        await asyncio.wait_for(find(), timeout)
    except TimeoutError:
        await feed.aclose()
        raise GrabError("timeout", f"the camera sent no video within {timeout:.0f} seconds") from None
    except BaseException:
        await feed.aclose()
        raise

    async def raw() -> AsyncIterator[bytes]:
        try:
            if first:
                yield first
            async for chunk in feed:
                if data := video.feed(chunk):
                    yield data
        finally:
            await feed.aclose()

    return video.format, raw()


async def grab(
    source: str | None = None,
    *,
    feed: AsyncIterator[bytes] | None = None,
    ffmpeg: str = "ffmpeg",
    input_args: Sequence[str] = RTSP_INPUT,
    max_width: int = MAX_WIDTH,
    timeout: float = GRAB_TIMEOUT,
    secrets: Sequence[str] = (),
) -> Picture:
    """Take one frame from `source` (an address ffmpeg can open), or from `feed`, which yields MPEG-TS.
    `secrets` are removed from anything ffmpeg printed."""
    if feed is None:
        return await _run(source or "", None, input_args, ffmpeg=ffmpeg, max_width=max_width, timeout=timeout, secrets=secrets)
    started = time.monotonic()
    fmt, raw = await elementary_video(feed, timeout)
    try:
        left = max(1.0, timeout - (time.monotonic() - started))
        return await _run("", raw, (*RAW_VIDEO_INPUT, "-f", fmt), ffmpeg=ffmpeg, max_width=max_width, timeout=left, secrets=secrets)
    finally:
        await feed.aclose()  # also when ffmpeg never started
