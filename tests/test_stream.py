"""`/stream/meter` frame parsing and the chunk decoder."""

from __future__ import annotations

import pytest
from envoy_client.models import EnvoyParseError, PhaseLayout
from envoy_client.stream import StreamDecoder, parse_stream_line

from tests.helpers import load_text

RAW = load_text("stream_meter.txt")


def _frames() -> list:
    return [f for line in RAW.splitlines() if (f := parse_stream_line(line))]


def test_reference_frames() -> None:
    first, second = _frames()
    assert first.production is not None
    assert first.net_consumption is not None
    assert first.production.power == 0
    assert first.net_consumption.power == pytest.approx(589.034 + 584.159)
    assert first.net_consumption.phases["ph-a"].voltage == pytest.approx(120.378)
    assert second.net_consumption is not None
    assert second.net_consumption.power == pytest.approx(602.934 + 582.624)


def test_frames_carry_ph_c_on_split_phase() -> None:
    """Entities follow the layout, not the keys: ph-c is present and all zeros (spec 3.3)."""
    frame = _frames()[0]
    assert frame.net_consumption is not None
    assert set(frame.net_consumption.phases) == {"ph-a", "ph-b", "ph-c"}
    ph_c = frame.net_consumption.phases["ph-c"]
    assert (ph_c.power, ph_c.voltage, ph_c.current) == (0, 0, 0)
    assert "ph-c" not in PhaseLayout.SPLIT.phases


def test_split_phase_voltage() -> None:
    assert _frames()[0].split_phase_voltage() == pytest.approx(120.378 + 120.366)


@pytest.mark.parametrize("line", ["", "   ", ": keepalive", "event: meter", "id: 3"])
def test_non_data_lines_are_skipped(line: str) -> None:
    assert parse_stream_line(line) is None


def test_bad_json_raises() -> None:
    with pytest.raises(EnvoyParseError):
        parse_stream_line("data: {not json")


@pytest.mark.parametrize("size", [1, 7, 64, 4096])
def test_decoder_reassembles_any_chunking(size: int) -> None:
    data = RAW.encode()
    decoder = StreamDecoder()
    frames = []
    for start in range(0, len(data), size):
        frames += decoder.feed(data[start : start + size])
    assert frames == _frames()


def test_decoder_holds_incomplete_line() -> None:
    line = RAW.splitlines()[0]
    decoder = StreamDecoder()
    assert decoder.feed(line.encode()) == []
    assert decoder.feed(b"\n") == [_frames()[0]]


def test_decoder_handles_split_utf8_and_keepalives() -> None:
    body = ': ping\n\ndata: {"note": "é", "production": {}}\n'.encode()
    split = body.index("é".encode()) + 1  # between the two bytes of é
    decoder = StreamDecoder()
    frames = decoder.feed(body[:split]) + decoder.feed(body[split:])
    assert len(frames) == 1
    assert frames[0].production is not None
    assert frames[0].production.phases == {}


def test_decoder_rejects_runaway_line() -> None:
    decoder = StreamDecoder()
    with pytest.raises(EnvoyParseError):
        decoder.feed(b"x" * (StreamDecoder.MAX_LINE + 1))
