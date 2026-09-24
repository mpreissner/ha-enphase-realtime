"""Decoding for `/stream/meter`, the Envoy's server-sent event stream.

The transport hands chunks to `StreamDecoder.feed` as they arrive; chunk boundaries fall
anywhere, including mid-line and mid-UTF-8 sequence. Only `data:` lines carry frames. Blank lines
separate events, and lines starting with `:` are keepalive comments.
"""

from __future__ import annotations

import codecs
import json

from .models import EnvoyParseError, StreamFrame


def parse_stream_line(line: str) -> StreamFrame | None:
    """Parse one complete line. Returns `None` for anything that isn't a data line."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    try:
        payload = json.loads(line.removeprefix("data:"))
    except json.JSONDecodeError as err:
        raise EnvoyParseError(f"stream data line isn't JSON: {line[:80]!r}") from err
    return StreamFrame.from_payload(payload)


class StreamDecoder:
    """Buffer partial lines across chunks. Create a new decoder for each connection."""

    # A frame is about 1.5 K characters. Much longer without a newline isn't the stream we expect.
    MAX_LINE = 64 * 1024

    def __init__(self) -> None:
        self._text = codecs.getincrementaldecoder("utf-8")()
        self._pending = ""

    def feed(self, chunk: bytes) -> list[StreamFrame]:
        """Return the frames completed by this chunk, in order."""
        self._pending += self._text.decode(chunk)
        *lines, self._pending = self._pending.split("\n")
        if len(self._pending) > self.MAX_LINE:
            raise EnvoyParseError(
                f"stream line exceeds {self.MAX_LINE} characters without a newline"
            )
        return [frame for line in lines if (frame := parse_stream_line(line)) is not None]
