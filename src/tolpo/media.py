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

Only the owner's own account password is ever used. Nothing here knows any default or built-in credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import AsyncIterator

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


async def live_chunks(
    host: str, password: str, quality: str = "low", *, port: int = MEDIA_PORT, timeout: float = CONNECT_TIMEOUT
) -> AsyncIterator[bytes]:
    """Yield decrypted MPEG-TS from the camera's live preview until the caller stops. Closes the connection when
    the generator is closed. Logs in exactly once: a wrong password is reported, never retried, because repeated
    failures can lock a camera out for a while."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except ConnectionRefusedError:
        raise GrabError("refused", "the camera refused the video connection; it may be asleep") from None
    except (TimeoutError, OSError):
        raise GrabError("unreachable", "the camera did not answer on the network") from None

    try:
        request = (
            f"POST /stream HTTP/1.1\r\nContent-Type: multipart/mixed;boundary={CLIENT_BOUNDARY}\r\n"
            "Connection: keep-alive\r\nContent-Length: -1\r\n"
        )

        async def read_head() -> bytes:
            try:
                return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
            except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                raise GrabError("unreachable", "the camera's video port stopped answering") from None

        log.debug("video channel of %s: connected", host)
        writer.write((request + "\r\n").encode())
        status, headers = _parse_head(await read_head())
        challenge = headers.get("www-authenticate", "")
        log.debug("video channel of %s: first answer %s, challenge fields %s", host, status, sorted(re.findall(r"(\w+)=", challenge)))
        if status != 401 or not challenge.lower().startswith("digest"):
            raise GrabError("failed", f"the camera's video port answered {status} instead of asking for a login")

        hashed = hashed_password(password, challenge)
        writer.write((request + f"Authorization: {digest_header(challenge, hashed)}\r\n\r\n").encode())
        status, headers = _parse_head(await read_head())
        log.debug("video channel of %s: login answer %s, headers %s", host, status, sorted(headers))
        if status in (401, 403):
            raise GrabError("auth", "the camera rejected the Tapo account password on its video port")
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
        decrypt = Decryptor(who, nonce, hashed)
        boundary = DEVICE_BOUNDARY
        if m := re.search(r"boundary=([^;\s]+)", headers.get("content-type", "")):
            boundary = m.group(1).encode()

        preview = {"params": {"preview": {"audio": ["disable"], "channels": [0], "resolutions": [RESOLUTIONS[quality]]}, "method": "get"}, "seq": 1, "type": "request"}
        body = json.dumps(preview, separators=(",", ":")).encode()
        writer.write(
            b"--" + CLIENT_BOUNDARY.encode() + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\n\r\n" + body + b"\r\n"
        )
        await writer.drain()

        parts = 0
        while True:
            try:
                await asyncio.wait_for(reader.readuntil(boundary), timeout=timeout * 2)
                part = _headers(await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout))
                size = int(part.get("content-length", "-1"))
                if not 0 <= size <= MAX_PART:
                    raise GrabError("failed", "the camera sent a video part of an impossible size")
                data = await asyncio.wait_for(reader.readexactly(size), timeout * 2)
            except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                raise GrabError("timeout", "the camera stopped sending video") from None
            parts += 1
            if parts <= 8:
                log.debug("video channel of %s: part %d is %s, %d bytes, encrypted=%s", host, parts, part.get("content-type"), size, part.get("x-if-encrypt"))
            if part.get("content-type") != "video/mp2t":
                continue  # the answer to our request, audio, or a notice
            if part.get("x-if-encrypt", "1") != "0":
                try:
                    data = decrypt(data)
                except ValueError:
                    raise GrabError("failed", "the video could not be decrypted; the account password may not match this camera") from None
            yield data
    finally:
        writer.close()
