"""The video channel the Tapo app uses, on port 8800 of the camera. It needs the Tapo account password and
nothing else, unlike RTSP, which needs a separate camera account.

How it works, as described by the open source clients that already speak it (go2rtc and pytapo):

1. `POST /stream` on port 8800. The camera answers 401 with an HTTP Digest challenge.
2. Answer as the user `admin`. The "password" in the Digest is the Tapo account password, hashed with SHA-256
   when the challenge says `encrypt_type="3"` and with MD5 otherwise, as upper-case hex.
3. The camera answers 200 and names a nonce in a `Key-Exchange` header. The media is AES-128-CBC encrypted
   with a key and IV derived from that nonce and the same hashed password.
4. The client asks for a live preview with one JSON message. The camera replies with multipart parts; the ones
   typed `video/mp2t` carry encrypted MPEG-TS, which ffmpeg can read as it is.

The same login opens other kinds of session: pictures of past recordings and events, and copies of recordings
(`MediaSession`, `fetch_image`, `download`). The client sends an id of its own, reads the camera's notifications, and
asks the camera to stop a session before it hangs up.

Only the owner's own account password is ever used. Nothing here knows any default or built-in credential, and
nothing here handles the pre-shared keys of cameras that use TLS on their media port: those are named, not served.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .snapshot import GrabError

log = logging.getLogger(__name__)

MEDIA_PORT = 8800
CONNECT_TIMEOUT = 8.0
CLIENT_BOUNDARY = "--client-stream-boundary--"
DEVICE_BOUNDARY = b"--device-stream-boundary--"
RESOLUTIONS = {"low": "VGA", "high": "HD"}
MAX_HEADER = 16 * 1024  # a part header, or the answer to the first request, is far smaller than this
MAX_PART = 8 * 1024 * 1024


def hashed_password(password: str, challenge: str) -> str:
    """Upper-case hex digest of the account password; the camera's challenge says which hash it wants."""
    digest = hashlib.sha256 if 'encrypt_type="3"' in challenge else hashlib.md5
    return digest(password.encode()).hexdigest().upper()


def digest_header(
    challenge: str, hashed: str, *, username: str = "admin", method: str = "POST", uri: str = "/stream", cnonce: str | None = None
) -> str:
    """The `Authorization` header that answers an HTTP Digest challenge (RFC 7616, MD5, qop=auth)."""
    field = lambda name: (m.group(1) if (m := re.search(rf'{name}="([^"]*)"', challenge)) else "")
    realm, nonce, opaque = field("realm"), field("nonce"), field("opaque")
    cnonce = cnonce or os.urandom(12).hex()
    nc = "00000001"
    ha1 = hashlib.md5(f"{username}:{realm}:{hashed}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
    header = (
        f'Digest username="{username}",realm="{realm}",uri="{uri}",algorithm=MD5,nonce="{nonce}",'
        f'nc={nc},cnonce="{cnonce}",qop=auth,response="{response}"'
    )
    return header + (f',opaque="{opaque}"' if opaque else "")


class Decryptor:
    """AES-128-CBC with a key and IV from the camera's nonce. Every part starts again from the same IV."""

    def __init__(self, username: str, nonce: str, hashed: str):
        self._key = hashlib.md5(f"{nonce}:{hashed}".encode()).digest()
        self._iv = hashlib.md5(f"{username}:{nonce}".encode()).digest()

    def __call__(self, data: bytes) -> bytes:
        if not data or len(data) % 16:
            raise ValueError("the encrypted part is not a whole number of blocks")
        plain = Cipher(algorithms.AES(self._key), modes.CBC(self._iv)).decryptor()
        padded = plain.update(data) + plain.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()


def _headers(block: bytes | str) -> dict[str, str]:
    out = {}
    text = block.decode("latin1") if isinstance(block, bytes) else block
    for line in text.strip().split("\r\n"):
        name, _, value = line.partition(":")
        if value:
            out[name.strip().lower()] = value.strip()
    return out


def _parse_head(head: bytes) -> tuple[int, dict[str, str]]:
    lines = head.decode("latin1").strip().split("\r\n", 1)
    match = re.match(r"HTTP/\S+\s+(\d{3})", lines[0])
    if not match:
        raise GrabError("failed", "the camera's video port did not speak HTTP")
    return int(match.group(1)), _headers(lines[1] if len(lines) > 1 else "")




# ---- sessions on the media port ---------------------------------------------------------------
#
# Every kind of session (live preview, thumbnails and event pictures, clip download) is one TCP connection with the
# same login, one JSON request, and then multipart parts until one side stops. Control messages are JSON parts with a
# `type` (request, response, notification, error), a `seq` and `params`. Notes on the fields are in docs/protocols.md and
# docs/areas/camera_media.md, written from the audit in our own words.

# UNVERIFIED: from the app's code only; none of the owner's cameras uses TLS-PSK.
PSK_PORT = 28800  # cameras that protect their media with TLS and a pre-shared key serve it here instead of on 8800
CLIENT_UUID = str(uuid.uuid4())  # the app sends one per install; tolpo sends one per process, never anything about the host
# UNVERIFIED: the window and the acknowledgement come from the app's code; whether a camera really waits for them is
# not tested on a real one.
WINDOW_SIZE = 50  # parts the camera may send before it waits for an acknowledgement (download and picture sessions)
ACK_EVERY = 25  # the app acknowledges every 25th part number
MAX_IMAGE = 2 * 1024 * 1024  # a thumbnail or event picture; the app's own buffer for one is 500 KB
STOP_TIMEOUT = 1.0
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
IMAGE_TYPES = {"image/jpeg": "jpeg", "image/avc": "h264", "image/hevc": "hevc"}

PSK_MESSAGE = (
    "the camera serves its video only over TLS with a pre-shared key (port 28800). tolpo does not support that mode, "
    "because it needs a key the camera hands out to the Tapo app"
)
LAT_HINT = (
    " If the password is right, the camera may be in the local access token mode of newer firmware, "
    "where its video password comes from the Tapo cloud; tolpo cannot use that mode"
)

# Why the camera ended a stream (`stream_finish`). Only these known reasons are shown; the camera's own text is not.
FINISH_REASONS = {
    "interrupt": "it interrupted the stream",
    "internal_error": "it hit an internal error",
    "channel_offline": "its video channel is offline",
    "channel_invalid": "it has no such video channel",
    "permission_deny": "it refused the stream for this account",
    "password_changed": "its password changed",
    "media_encrypt_changed": "its video encryption setting changed",
    "share_finish": "a shared stream ended",
}
CLOSE_REASONS = {
    "data_timeout": "it waited too long for data",
    "request_timeout": "it waited too long for a request",
    "exit": "it closed the session",
    "use_high_priority_connection": "another client with priority took over its video",
}


@dataclass(frozen=True)
class Part:
    """One multipart part from the camera. `body` is as received; `media()` decrypts it when it is marked encrypted."""

    mime: str
    headers: dict[str, str]
    body: bytes
    decrypt: Callable[[bytes], bytes] | None = None

    @property
    def encrypted(self) -> bool:
        return self.headers.get("x-if-encrypt", "1") != "0"

    @property
    def seq(self) -> int | None:
        value = self.headers.get("x-data-sequence", "")
        return int(value) if re.fullmatch(r"[0-9]{1,10}", value) else None  # ASCII digits only: "\xb2".isdigit() is True

    @property
    def session(self) -> str | None:
        return self.headers.get("x-session-id")

    def media(self) -> bytes:
        if not self.encrypted or self.decrypt is None:
            return self.body
        try:
            return self.decrypt(self.body)
        except ValueError:
            raise GrabError("failed", "the video could not be decrypted; the account password may not match this camera") from None

    def message(self) -> dict | None:
        """The JSON in a control part, or None when it is not readable JSON."""
        try:
            data = self.media() if self.encrypted and self.decrypt is not None else self.body
            value = json.loads(data)
        except (GrabError, ValueError, UnicodeDecodeError):
            return None
        return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class Notice:
    """A control message that is not an error: the response to our request, or a notification."""

    event: str  # "response", or the notification's event_type
    params: dict[str, Any] = field(default_factory=dict)


def _code(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def notice_failure(event: str, params: dict[str, Any]) -> GrabError | None:
    """A notification that means the session cannot go on, as an error an agent may read. None for the others."""
    # UNVERIFIED: event names and field names (`reason`, `close_reason`, `status`) are from the app's classes; no real
    # camera has been seen sending them to tolpo yet.
    if event == "device_locked":
        return GrabError("locked", "the camera locked its video login after failed attempts; wait several minutes before trying again")
    if event == "stream_finish":
        reason = params.get("reason")
        if reason == "finish":
            return None  # the normal end of a recording or picture; the session decides what that means
        why = FINISH_REASONS.get(reason) if isinstance(reason, str) else None
        return GrabError("finished", f"the camera ended the stream: {why or 'it gave a reason tolpo does not know'}")
    if event == "connection_closed":
        reason = params.get("close_reason", params.get("reason"))
        why = CLOSE_REASONS.get(reason) if isinstance(reason, str) else None
        return GrabError("closed", f"the camera closed the video session: {why or 'it gave a reason tolpo does not know'}")
    if event == "internal_error":
        return GrabError("failed", "the camera reported an internal error on its video channel")
    if event == "disk_status" and params.get("status") == "offline":
        return GrabError("storage", "the camera's memory card is offline")
    if event == "channel_stream_status":
        status = params.get("status")
        status = status if isinstance(status, list) else [status]
        if "offline" in status:
            return GrabError("finished", "the camera's video channel is offline")
        if "lack_bandwidth" in status:
            return GrabError("busy", "the camera has no bandwidth left for another video stream")
    return None


async def _port_open(host: str, port: int, timeout: float) -> bool:
    """Whether something accepts a TCP connection. Nothing is sent: no TLS handshake, no login."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


def _looks_like_tls(data: bytes) -> bool:
    """A TLS record starts with its type (handshake 0x16 or alert 0x15) and the major version 3."""
    return len(data) >= 2 and data[0] in (0x15, 0x16) and data[1] == 0x03


class MediaSession:
    """One connection to the camera's media port. It logs in exactly once: a wrong password is reported, never
    retried, because repeated failures lock the camera's video login for a while (it says so with `device_locked`)."""

    def __init__(
        self, host: str, password: str, *, port: int = MEDIA_PORT, psk_port: int = PSK_PORT,
        timeout: float = CONNECT_TIMEOUT, what: str = "video",
    ):
        self.host, self.port, self.psk_port, self.timeout, self.what = host, port, psk_port, timeout, what
        self._password = password
        self.session_id: str | None = None
        self._seq = 0
        self._parts = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._decrypt: Decryptor | None = None
        self._boundary = DEVICE_BOUNDARY

    async def __aenter__(self) -> MediaSession:
        await self.open()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ---- login ---------------------------------------------------------------------------------

    async def _psk_or(self, failure: GrabError) -> GrabError:
        if self.psk_port and await _port_open(self.host, self.psk_port, min(self.timeout, 3.0)):
            log.info("video channel of %s: port %d is closed but port %d answers", self.host, self.port, self.psk_port)
            return GrabError("tls_psk", PSK_MESSAGE)
        return failure

    async def _read_head(self, *, first: bool = False) -> bytes:
        try:
            return await asyncio.wait_for(self._reader.readuntil(b"\r\n\r\n"), self.timeout)
        except asyncio.IncompleteReadError as exc:
            if first and _looks_like_tls(exc.partial):
                raise GrabError("tls_psk", PSK_MESSAGE) from None
            if first and not exc.partial:
                raise await self._psk_or(GrabError("unreachable", "the camera's video port stopped answering")) from None
            raise GrabError("unreachable", "the camera's video port stopped answering") from None
        except (TimeoutError, asyncio.LimitOverrunError, ConnectionError):
            raise GrabError("unreachable", "the camera's video port stopped answering") from None

    async def open(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
        except ConnectionRefusedError:
            raise await self._psk_or(GrabError("refused", "the camera refused the video connection; it may be asleep")) from None
        except (TimeoutError, OSError):
            raise GrabError("unreachable", "the camera did not answer on the network") from None
        try:
            await self._login()
        except BaseException:
            self._drop()
            raise

    async def _login(self) -> None:
        host = self.host
        request = (
            f"POST /stream HTTP/1.1\r\nContent-Type: multipart/mixed;boundary={CLIENT_BOUNDARY}\r\n"
            f"Connection: keep-alive\r\nContent-Length: -1\r\nX-Client-UUID: {CLIENT_UUID}\r\n"
        )
        log.debug("video channel of %s: connected", host)
        self._writer.write((request + "\r\n").encode())
        head = await self._read_head(first=True)
        if _looks_like_tls(head):
            raise GrabError("tls_psk", PSK_MESSAGE)
        status, headers = _parse_head(head)
        challenge = headers.get("www-authenticate", "")
        log.debug("video channel of %s: first answer %s, challenge fields %s", host, status, sorted(re.findall(r"(\w+)=", challenge)))
        if status != 401 or not challenge.lower().startswith("digest"):
            raise GrabError("failed", f"the camera's video port answered {status} instead of asking for a login")

        hashed = hashed_password(self._password, challenge)
        self._writer.write((request + f"Authorization: {digest_header(challenge, hashed)}\r\n\r\n").encode())
        status, headers = _parse_head(await self._read_head())
        log.debug("video channel of %s: login answer %s, headers %s", host, status, sorted(headers))
        if status in (401, 403):
            hint = LAT_HINT if 'encrypt_type="3"' in challenge else ""
            raise GrabError("auth", "the camera rejected the Tapo account password on its video port." + hint)
        if status != 200:
            raise GrabError("failed", f"the camera's video port answered {status}")
        exchange = headers.get("key-exchange", "")
        log.debug("video channel of %s: key exchange fields %s, content type %r", host, sorted(re.findall(r"(\w+)=", exchange)), headers.get("content-type"))
        nonce = (m.group(1) if (m := re.search(r'nonce="?([^"\s]+)"?', exchange)) else "")
        who = (m.group(1) if (m := re.search(r'username="?([^"\s]+)"?', exchange)) else "admin")
        if not nonce:
            raise GrabError("failed", "the camera did not start an encrypted video session")
        if who == "none":
            raise GrabError("failed", "the camera offered an unencrypted video session, which tolpo does not use")
        self._decrypt = Decryptor(who, nonce, hashed)
        if m := re.search(r"boundary=([^;\s]+)", headers.get("content-type", "")):
            self._boundary = m.group(1).encode()

    # ---- what we send --------------------------------------------------------------------------

    async def _send_part(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        extra = "".join(f"{name}: {value}\r\n" for name, value in (headers or {}).items())
        self._writer.write(
            b"--" + CLIENT_BOUNDARY.encode() + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\n" + extra.encode() + b"\r\n" + body + b"\r\n"
        )
        await self._writer.drain()

    async def request(self, params: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        """Send one request, for example {"preview": {...}, "method": "get"}."""
        self._seq += 1
        body = json.dumps({"params": params, "seq": self._seq, "type": "request"}, separators=(",", ":")).encode()
        await self._send_part(body, headers)

    async def ack(self, seq: int) -> None:
        """Tell the camera that parts up to `seq` arrived, so it keeps sending past its window."""
        headers = {"X-Data-Received": str(seq)}
        if self.session_id:
            headers["X-Session-Id"] = self.session_id
        await self._send_part(b'{"type":"notification","params":{"event_type":"stream_sequence"}}', headers)

    async def stop(self) -> None:
        """Ask the camera to end the session, like the app does, instead of only hanging up. Never raises."""
        if self._writer is None or self._writer.is_closing() or not self.session_id:
            return
        self._seq += 1
        body = json.dumps({"params": {"method": "do", "stop": "null"}, "seq": self._seq, "type": "request"}, separators=(",", ":"))
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._send_part(body.encode(), {"X-Session-Id": self.session_id}), STOP_TIMEOUT)

    def _drop(self) -> None:
        if self._writer is not None:
            self._writer.close()

    async def close(self) -> None:
        try:
            await self.stop()
        finally:
            self._drop()

    # ---- what the camera sends -----------------------------------------------------------------

    async def read_part(self) -> Part:
        stopped = f"the camera stopped sending {self.what}"
        try:
            await asyncio.wait_for(self._reader.readuntil(self._boundary), timeout=self.timeout * 2)
            headers = _headers(await asyncio.wait_for(self._reader.readuntil(b"\r\n\r\n"), self.timeout))
            size = int(headers.get("content-length", "-1"))
            if not 0 <= size <= MAX_PART:
                raise GrabError("failed", "the camera sent a video part of an impossible size")
            body = await asyncio.wait_for(self._reader.readexactly(size), self.timeout * 2)
        except asyncio.IncompleteReadError:
            raise GrabError("ended", stopped) from None
        except (TimeoutError, asyncio.LimitOverrunError, ConnectionError, ValueError):
            raise GrabError("timeout", stopped) from None
        self._parts += 1
        if self._parts <= 8:
            log.debug("video channel of %s: part %d is %s, %d bytes, encrypted=%s", self.host, self._parts, headers.get("content-type"), size, headers.get("x-if-encrypt"))
        return Part(headers.get("content-type", ""), headers, body, self._decrypt)

    async def events(self, *, ack: bool = False) -> AsyncIterator[Part | Notice]:
        """Media parts and notices, in order. A refused request or a notification that ends the session raises a
        GrabError. With `ack`, every 25th numbered part is acknowledged, as the app does for downloads."""
        while True:
            part = await self.read_part()
            seq = part.seq
            if ack and seq and seq % ACK_EVERY == 0:
                await self.ack(seq)
            if part.mime != "application/json":
                yield part
                continue
            message = part.message()
            if message is None:
                continue
            kind, params = message.get("type"), message.get("params")
            params = params if isinstance(params, dict) else {}
            if kind == "response":
                code = _code(params.get("error_code", message.get("error_code", 0)))
                if code:
                    raise GrabError("rejected", f"the camera refused the request (error {code})")
                session = params.get("session_id")
                if session is not None and SESSION_ID.fullmatch(str(session)):
                    self.session_id = str(session)  # it goes back into our own headers, so only a plain token is kept
                yield Notice("response", params)
            elif kind == "notification":
                event = str(params.get("event_type", ""))
                if failure := notice_failure(event, params):
                    raise failure
                yield Notice(event, params)
            elif kind == "error":
                code = _code(params.get("error_code", message.get("error_code")))
                raise GrabError("rejected", "the camera refused the request" + (f" (error {code})" if code else ""))


async def live_chunks(
    host: str, password: str, quality: str = "low", *, port: int = MEDIA_PORT, timeout: float = CONNECT_TIMEOUT,
    psk_port: int = PSK_PORT,
) -> AsyncIterator[bytes]:
    """Yield decrypted MPEG-TS from the camera's live preview until the caller stops. Closes the connection when
    the generator is closed, after asking the camera to stop. Logs in exactly once."""
    session = MediaSession(host, password, port=port, psk_port=psk_port, timeout=timeout)
    await session.open()
    try:
        await session.request({"preview": {"audio": ["disable"], "channels": [0], "resolutions": [RESOLUTIONS[quality]]}, "method": "get"})
        async for item in session.events():
            if isinstance(item, Notice):
                if item.event == "stream_finish":
                    raise GrabError("finished", "the camera ended the live stream")
                continue
            if item.mime != "video/mp2t":
                continue  # audio, or a part this client does not use
            yield item.media()
    finally:
        await session.close()


def client_id() -> int:
    """Checked on a C465: a playback or download gets video for a client id up to 255 and none for 65535 or more."""
    return secrets.randbelow(200) + 1


OPEN_BYTES = 1024 * 1024  # how much video a playback must deliver before a download follows
OPEN_SECONDS = 6.0


async def open_playback(
    host: str, password: str, start: int, *, port: int = MEDIA_PORT, psk_port: int = PSK_PORT, client: int | None = None,
) -> None:
    """Play a recording for a moment. Checked on a C465: it answers a download with a response and no video until a
    playback has been opened, and playing only the first part was not enough, while about a megabyte (four seconds of
    video) was: after that the downloads work for a while. The download that follows uses the same `client` id."""
    got = 0
    feed = playback_chunks(host, password, start, start + 1, port=port, psk_port=psk_port, client=client)

    async def play() -> None:
        nonlocal got
        async for chunk in feed:
            got += len(chunk)
            if got >= OPEN_BYTES:
                return

    try:
        await asyncio.wait_for(play(), OPEN_SECONDS)
    except TimeoutError:
        pass  # a short recording may hold less than that; the download decides whether it worked
    finally:
        await feed.aclose()


async def playback_chunks(
    host: str, password: str, start: int, end: int, *, port: int = MEDIA_PORT, timeout: float = CONNECT_TIMEOUT,
    psk_port: int = PSK_PORT, client: int | None = None,
) -> AsyncIterator[bytes]:
    """Yield decrypted MPEG-TS of a recording from the camera's memory card, from `start` on. Checked on a C465: the
    camera answers, then streams faster than real time, and it ignores `end` and plays on into the next recordings, so
    the caller stops reading when it has what it needs. Closes the connection when the generator is closed, after asking
    the camera to stop. Logs in exactly once."""
    session = MediaSession(host, password, port=port, psk_port=psk_port, timeout=timeout, what="the recording")
    await session.open()
    try:
        request = {"playback": {"client_id": client if client is not None else client_id(), "scale": "1/1", "start_time": str(start), "end_time": str(end)}, "method": "get"}
        await session.request(request, {"X-Data-Window-Size": str(WINDOW_SIZE)})
        async for item in session.events(ack=True):
            if isinstance(item, Notice):
                if item.event == "stream_finish" or (item.event == "stream_status" and item.params.get("status") == "finished"):
                    return
                continue  # for example change_aov_type, which only says that the recording changed its frame rate mode
            if item.mime == "video/mp2t":
                yield item.media()
    finally:
        await session.close()


async def fetch_image(
    host: str, password: str, request: dict[str, Any], *, port: int = MEDIA_PORT, psk_port: int = PSK_PORT,
    timeout: float = CONNECT_TIMEOUT, deadline: float = 20.0, max_bytes: int = MAX_IMAGE,
) -> tuple[str, bytes]:
    """One picture from a picture session: `request` is {"image": {...}} (a recording's cover picture) or
    {"download": {...}} (an event picture). Returns the format ("jpeg", "h264" or "hevc") and the bytes."""
    # UNVERIFIED: the end of a picture is a `stream_status` notice with status finished or image_ended, as in the app.
    acks = "download" in request  # the app acknowledges parts on download sessions; it was not seen doing so for covers
    session = MediaSession(host, password, port=port, psk_port=psk_port, timeout=timeout, what="the picture")
    await session.open()
    data, fmt = bytearray(), ""

    async def run() -> None:
        nonlocal fmt
        try:
            async for item in session.events(ack=acks):
                if isinstance(item, Notice):
                    status = item.params.get("status") if item.event == "stream_status" else None
                    if status in ("finished", "image_ended") or item.event == "stream_finish":
                        if data:
                            return
                        raise GrabError("no_picture", "the camera has no picture for that time or event")
                    continue
                kind = IMAGE_TYPES.get(item.mime)
                if kind is None or (session.session_id and item.session and item.session != session.session_id):
                    continue
                if fmt and kind != fmt:
                    raise GrabError("failed", "the camera mixed two picture formats in one answer")
                fmt = kind
                data.extend(item.media())
                if len(data) > max_bytes:
                    raise GrabError("too_big", "the camera sent a picture larger than tolpo accepts")
        except GrabError as exc:
            if exc.reason == "ended" and data:
                return  # the camera hung up after the picture; what arrived is checked by the caller
            raise

    try:
        await session.request({**request, "method": "get"}, {"X-Data-Window-Size": str(WINDOW_SIZE)})
        await asyncio.wait_for(run(), deadline)
    except TimeoutError:
        raise GrabError("timeout", f"the camera sent no picture within {deadline:.0f} seconds") from None
    finally:
        await session.close()
    return fmt, bytes(data)


@dataclass
class Download:
    bytes: int = 0
    parts: int = 0
    complete: bool = False  # the camera said the recording ended, or hung up after sending it
    stopped: str = ""  # why tolpo stopped early: "size" or "time"


async def download(
    host: str, password: str, request: dict[str, Any], write: Callable[[bytes], Any], *, port: int = MEDIA_PORT,
    psk_port: int = PSK_PORT, timeout: float = CONNECT_TIMEOUT, max_bytes: int, max_seconds: float,
) -> Download:
    """Copy a recording from the camera's memory card: `request` is {"download": {...}}. Every video part goes to
    `write` as decrypted MPEG-TS. Parts are acknowledged every 25th sequence number with a window of 50, as the app
    does. Stops politely at `max_bytes` or after `max_seconds`, whichever comes first."""
    # UNVERIFIED: how a real camera ends a download (a `stream_status` finished notice, `stream_finish`, or hanging up).
    session = MediaSession(host, password, port=port, psk_port=psk_port, timeout=timeout, what="the recording")
    await session.open()
    result = Download()

    async def run() -> None:
        try:
            async for item in session.events(ack=True):
                if isinstance(item, Notice):
                    status = item.params.get("status") if item.event == "stream_status" else None
                    if status == "finished" or item.event == "stream_finish":
                        result.complete = True
                        return
                    continue
                if item.mime != "video/mp2t":
                    continue
                data = item.media()
                if result.bytes + len(data) > max_bytes:
                    result.stopped = "size"
                    return
                write(data)
                result.bytes += len(data)
                result.parts += 1
        except GrabError as exc:
            if exc.reason == "ended" and result.bytes:
                result.complete = True  # some cameras may simply hang up at the end of the recording
                return
            raise

    try:
        await session.request({**request, "method": "get"}, {"X-Data-Window-Size": str(WINDOW_SIZE)})
        await asyncio.wait_for(run(), max_seconds)
    except TimeoutError:
        result.stopped = "time"
    finally:
        await session.close()
    return result
