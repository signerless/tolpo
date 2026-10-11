"""Pull the video track out of an MPEG transport stream.

The cameras' live stream carries video plus a private second track that ffmpeg cannot identify, and it can begin with
padding packets. ffmpeg guesses what is in a pipe by reading it for a while, and those two things make it guess for
minutes or not at all. So tolpo finds the video itself: the program tables name the video track, and the payload of
that track is the raw video that ffmpeg decodes without any guessing.
"""

from __future__ import annotations

PACKET = 188
SYNC = 0x47

# MPEG-TS stream types of the video the Tapo cameras send, and the raw format name ffmpeg uses for each.
VIDEO_FORMATS = {0x1B: "h264", 0x24: "hevc"}


class TsVideo:
    """Feed it transport stream bytes in any chunking. `feed` returns the raw video bytes found so far.
    Until the program tables have been seen, `format` is None and nothing is returned."""

    def __init__(self) -> None:
        self.format: str | None = None
        self._buffer = b""
        self._pmt_pids: set[int] = set()
        self._video_pid: int | None = None

    def feed(self, data: bytes) -> bytes:
        self._buffer += data
        out = bytearray()
        pos = 0
        while len(self._buffer) - pos >= PACKET:
            if self._buffer[pos] != SYNC:  # lost the beat: find the next packet start
                nxt = self._buffer.find(bytes([SYNC]), pos + 1)
                pos = nxt if nxt != -1 else len(self._buffer)
                continue
            self._packet(self._buffer[pos : pos + PACKET], out)
            pos += PACKET
        self._buffer = self._buffer[pos:]
        return bytes(out)

    def _packet(self, pkt: bytes, out: bytearray) -> None:
        if pkt[1] & 0x80:  # the transport error flag: the camera says this packet is damaged
            return
        pid = (pkt[1] & 0x1F) << 8 | pkt[2]
        start = bool(pkt[1] & 0x40)
        adaptation = (pkt[3] >> 4) & 3
        if not adaptation & 1:  # no payload (padding or only an adaptation field)
            return
        offset = 4 + (1 + pkt[4] if adaptation & 2 else 0)
        if offset >= PACKET:
            return
        payload = pkt[offset:]
        if pid == 0 and start:
            self._pat(payload)
        elif pid in self._pmt_pids and start:
            self._pmt(payload)
        elif pid == self._video_pid:
            if start:
                if payload[:3] != b"\x00\x00\x01" or len(payload) < 9:
                    return
                payload = payload[9 + payload[8] :]  # skip the PES header and its optional fields
            out += payload

    def _section(self, payload: bytes) -> bytes | None:
        """The table that starts in this packet, if it fits in it."""
        body = payload[1 + payload[0] :]
        if len(body) < 3:
            return None
        length = (body[1] & 0x0F) << 8 | body[2]
        return body[: 3 + length] if len(body) >= 3 + length else None

    def _pat(self, payload: bytes) -> None:
        section = self._section(payload)
        if section is None or section[0] != 0x00:
            return
        entries = section[8:-4]
        for i in range(0, len(entries) - 3, 4):
            if entries[i] << 8 | entries[i + 1]:  # program 0 points at the network table, not a program
                self._pmt_pids.add((entries[i + 2] & 0x1F) << 8 | entries[i + 3])

    def _pmt(self, payload: bytes) -> None:
        section = self._section(payload)
        if section is None or section[0] != 0x02 or self._video_pid is not None:
            return
        end = len(section) - 4
        pos = 12 + ((section[10] & 0x0F) << 8 | section[11])
        while pos + 5 <= end:
            kind, pid = section[pos], (section[pos + 1] & 0x1F) << 8 | section[pos + 2]
            if kind in VIDEO_FORMATS:
                self._video_pid, self.format = pid, VIDEO_FORMATS[kind]
                return
            pos += 5 + ((section[pos + 3] & 0x0F) << 8 | section[pos + 4])
