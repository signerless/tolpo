"""The camera's side of port 8800, written from the same public description as tolpo/media.py but separately,
so that a mistake in one is not silently copied into the other. It binds to 127.0.0.1 and nothing else."""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


class FakeCamera:
    def __init__(self, password: str, chunks: list[bytes], *, encrypt_type: str = "3", exchange_user: str = "admin",
                 send_key_exchange: bool = True, extra_parts: list[tuple[str, bytes]] = (), part_size: int | None = None,
                 answer: bytes | None = None, silent: bool = False, stream_password: str | None = None):
        self.chunks, self.extra_parts, self.part_size = chunks, list(extra_parts), part_size
        self.encrypt_type, self.exchange_user, self.send_key_exchange = encrypt_type, exchange_user, send_key_exchange
        self.answer, self.silent = answer, silent  # a fixed reply instead of the real conversation; or no reply at all
        digest = hashlib.sha256 if encrypt_type == "3" else hashlib.md5
        self.hashed = digest(password.encode()).hexdigest().upper()
        # normally the stream is locked with the same secret the login used; a test can make them differ
        self.stream_hashed = digest((stream_password or password).encode()).hexdigest().upper()
        self.logins = 0  # accepted
        self.failed_logins = 0  # rejected
        self.requests: list[dict] = []  # the JSON the client sent after logging in
        self.disconnected = asyncio.Event()
        self.port = 0
        self._server = None

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    # ---- one connection ----------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            if self.silent:
                await reader.read()
                return
            if self.answer is not None:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(self.answer)
                await writer.drain()
                await reader.read()
                return
            nonce, opaque = os.urandom(8).hex(), os.urandom(8).hex()
            challenge = (
                f'Digest realm="TP-Link IP-Camera",algorithm="MD5",encrypt_type="{self.encrypt_type}",'
                f'qop="auth",nonce="{nonce}",opaque="{opaque}"'
            )
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            if "authorization:" not in head.lower():
                writer.write(f"HTTP/1.0 401 Unauthorized\r\nWWW-Authenticate: {challenge}\r\nContent-Length: 0\r\n\r\n".encode())
                await writer.drain()
                head = (await reader.readuntil(b"\r\n\r\n")).decode()
            if not self._login_ok(head, nonce, opaque):
                self.failed_logins += 1
                writer.write(b"HTTP/1.0 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            self.logins += 1

            stream_nonce = os.urandom(8).hex()
            headers = "Content-Type: multipart/mixed;boundary=--device-stream-boundary--\r\n"
            if self.send_key_exchange:
                headers += f'Key-Exchange: cipher="AES_128_CBC" username="{self.exchange_user}" nonce="{stream_nonce}"\r\n'
            writer.write(f"HTTP/1.1 200 OK\r\n{headers}\r\n".encode())
            await writer.drain()

            self.requests.append(await self._read_request(reader))
            key = hashlib.md5(f"{stream_nonce}:{self.stream_hashed}".encode()).digest()
            iv = hashlib.md5(f"{self.exchange_user}:{stream_nonce}".encode()).digest()

            def part(mime: str, body: bytes, *, encrypted: bool, seq: int, claimed_size: int | None = None) -> bytes:
                if encrypted:
                    pad = padding.PKCS7(128).padder()
                    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
                    body = enc.update(pad.update(body) + pad.finalize()) + enc.finalize()
                return (
                    b"----device-stream-boundary--\r\n"
                    + f"Content-Type: {mime}\r\nContent-Length: {claimed_size if claimed_size is not None else len(body)}\r\n"
                      f"X-If-Encrypt: {int(encrypted)}\r\n"
                      f"X-Session-Id: 4242\r\nX-Data-Sequence: {seq}\r\n\r\n".encode()
                    + body + b"\r\n"
                )

            answer = json.dumps({"type": "response", "seq": 1, "params": {"error_code": 0, "session_id": "4242"}}).encode()
            writer.write(part("application/json", answer, encrypted=False, seq=1))
            for mime, body in self.extra_parts:
                writer.write(part(mime, body, encrypted=True, seq=2))
            for seq, chunk in enumerate(self.chunks, start=3):
                writer.write(part("video/mp2t", chunk, encrypted=True, seq=seq, claimed_size=self.part_size))
                await writer.drain()
            await reader.read()  # a live camera keeps talking; here the client leaves first
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.disconnected.set()
            writer.close()

    def _login_ok(self, head: str, nonce: str, opaque: str) -> bool:
        auth = re.search(r"authorization:\s*Digest\s+(.*)", head, re.I)
        if not auth:
            return False
        fields = {k.lower(): v.strip('"') for k, v in re.findall(r'(\w+)=("[^"]*"|[^,\s]+)', auth.group(1))}
        ha1 = _md5(f"admin:TP-Link IP-Camera:{self.hashed}")
        ha2 = _md5("POST:/stream")
        expected = _md5(f"{ha1}:{nonce}:{fields.get('nc')}:{fields.get('cnonce')}:auth:{ha2}")
        return fields.get("username") == "admin" and fields.get("nonce") == nonce and fields.get("response") == expected

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> dict:
        await reader.readuntil(b"----client-stream-boundary--\r\n")
        headers = (await reader.readuntil(b"\r\n\r\n")).decode().lower()
        size = int(re.search(r"content-length:\s*(\d+)", headers).group(1))
        return json.loads(await reader.readexactly(size))


def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def mpegts_with_unknown_track(ffmpeg: str = "ffmpeg") -> bytes:
    """A short real clip (640x360 video) whose second track has a stream type nothing can identify, like the
    private track the Tapo cameras add. Made with the real ffmpeg, then the program map is edited and re-signed."""
    with tempfile.TemporaryDirectory() as folder:
        clip = Path(folder) / "clip.ts"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10",
             "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3", "-c:v", "mpeg2video", "-c:a", "ac3", "-f", "mpegts", str(clip)],
            check=True,
        )
        data = bytearray(clip.read_bytes())
    pmt_pid = None
    for i in range(0, len(data), 188):  # the program association table says where the program map is
        pkt = data[i : i + 188]
        if ((pkt[1] & 0x1F) << 8 | pkt[2]) == 0 and pkt[1] & 0x40:
            start = 5 + pkt[4]
            length = (pkt[start + 1] & 0x0F) << 8 | pkt[start + 2]
            entries = pkt[start + 8 : start + 3 + length - 4]
            pmt_pid = next((entries[j + 2] & 0x1F) << 8 | entries[j + 3] for j in range(0, len(entries), 4) if entries[j] << 8 | entries[j + 1])
            break
    patched = 0
    for i in range(0, len(data), 188):
        pkt = data[i : i + 188]
        if ((pkt[1] & 0x1F) << 8 | pkt[2]) != pmt_pid or not pkt[1] & 0x40:
            continue
        start = 5 + pkt[4]
        length = (pkt[start + 1] & 0x0F) << 8 | pkt[start + 2]
        end = start + 3 + length - 4
        pos = start + 12 + ((pkt[start + 10] & 0x0F) << 8 | pkt[start + 11])
        while pos + 5 <= end:
            es_len = (pkt[pos + 3] & 0x0F) << 8 | pkt[pos + 4]
            if pkt[pos] == 0x81:  # the AC-3 audio track becomes a type no decoder knows, and loses its descriptors' meaning
                pkt[pos] = 0x91
                patched += 1
                d = pos + 5
                while d + 2 <= pos + 5 + es_len:
                    d_len = pkt[d + 1]
                    pkt[d] = 0xFE  # a user-private descriptor tag
                    d += 2 + d_len
            pos += 5 + es_len
        pkt[end : end + 4] = _crc32_mpeg(bytes(pkt[start:end])).to_bytes(4, "big")
        data[i : i + 188] = pkt
    assert patched, "the clip had no audio track to rewrite"
    # ffmpeg also probes a track's bytes, so make them unrecognisable as well (the PES framing stays intact)
    audio_pid = None
    for i in range(0, len(data), 188):
        pkt = data[i : i + 188]
        if ((pkt[1] & 0x1F) << 8 | pkt[2]) == pmt_pid and pkt[1] & 0x40:
            start = 5 + pkt[4]
            end = start + 3 + ((pkt[start + 1] & 0x0F) << 8 | pkt[start + 2]) - 4
            pos = start + 12 + ((pkt[start + 10] & 0x0F) << 8 | pkt[start + 11])
            while pos + 5 <= end:
                if pkt[pos] == 0x91:
                    audio_pid = (pkt[pos + 1] & 0x1F) << 8 | pkt[pos + 2]
                pos += 5 + ((pkt[pos + 3] & 0x0F) << 8 | pkt[pos + 4])
            break
    for i in range(0, len(data), 188):
        if ((data[i + 1] & 0x1F) << 8 | data[i + 2]) != audio_pid or not data[i + 3] & 0x10:
            continue
        payload = i + 4 + (data[i + 4] + 1 if data[i + 3] & 0x20 else 0)
        if data[i + 1] & 0x40:
            payload += 14  # start code, stream id, length, flags and the time stamp
        data[payload : i + 188] = b"\xa5" * (i + 188 - payload)
    return bytes(data)
