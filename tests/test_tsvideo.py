"""Finding the video track in an MPEG transport stream, with no ffmpeg involved."""

import pytest

from tolpo.tsvideo import PACKET, TsVideo

from .fake_camera import fake_ts

VIDEO = b"\x00\x00\x00\x01\x67" + bytes(range(256)) * 12


def extract(stream: bytes, slice_size: int | None = None) -> tuple[str | None, bytes]:
    video, out = TsVideo(), b""
    for i in range(0, len(stream), slice_size or len(stream)):
        out += video.feed(stream[i : i + (slice_size or len(stream))])
    return video.format, out


def test_the_video_comes_out_exactly_as_it_went_in():
    assert extract(fake_ts(VIDEO)) == ("h264", VIDEO)


@pytest.mark.parametrize("slice_size", [1, 7, 187, 188, 189, 1000])
def test_it_does_not_matter_how_the_bytes_arrive(slice_size):
    assert extract(fake_ts(VIDEO), slice_size) == ("h264", VIDEO)


def test_padding_before_the_tables_is_skipped():
    padding = b"\x47\x1f\xff\x10" + b"\xff" * 184
    assert extract(padding * 50 + fake_ts(VIDEO, null_packets=3)) == ("h264", VIDEO)


def test_the_private_second_track_is_left_out():
    with_track, without = extract(fake_ts(VIDEO, extra_track=True)), extract(fake_ts(VIDEO, extra_track=False))
    assert with_track == without == ("h264", VIDEO)


@pytest.mark.parametrize("kind, name", [(0x1B, "h264"), (0x24, "hevc")])
def test_the_format_follows_the_stream_type(kind, name):
    assert extract(fake_ts(VIDEO, kind=kind)) == (name, VIDEO)


@pytest.mark.parametrize("kind", [0x0F, 0x02])
def test_a_stream_with_no_video_it_can_use_stays_unknown(kind):
    assert extract(fake_ts(VIDEO, kind=kind)) == (None, b"")  # 0x0F is audio, 0x02 is a kind of video no Tapo camera sends


def test_nothing_comes_out_before_the_tables_are_known():
    stream = fake_ts(VIDEO)
    video_only = stream[2 * PACKET :]  # without the program tables
    assert extract(video_only) == (None, b"")


def test_it_finds_its_footing_again_after_garbage():
    stream = fake_ts(VIDEO)
    noisy = b"\x00\x01\x02" + stream[:PACKET * 2] + b"\x13\x37" + stream[PACKET * 2 :]
    fmt, out = extract(noisy)
    assert fmt == "h264" and VIDEO.endswith(out[-200:]) and len(out) > 0


def test_a_packet_the_camera_marks_as_damaged_is_dropped():
    stream = bytearray(fake_ts(VIDEO))
    stream[5 * PACKET + 1] |= 0x80  # the transport error flag on one video packet
    fmt, out = extract(bytes(stream))
    assert fmt == "h264" and len(out) < len(VIDEO)


def test_a_packet_with_no_payload_is_ignored():
    empty = bytes([0x47, 0x01, 0x00, 0x20, 183]) + b"\x00" + b"\xff" * 182  # an adaptation field only
    assert extract(fake_ts(VIDEO)[: 2 * PACKET] + empty + fake_ts(VIDEO)[2 * PACKET :]) == ("h264", VIDEO)


def test_the_video_track_is_found_even_when_another_track_is_listed_first():
    assert extract(fake_ts(VIDEO, extra_first=True)) == ("h264", VIDEO)
