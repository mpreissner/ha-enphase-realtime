"""Decoding for `/stream/meter`, the Envoy's server-sent event stream.

The transport hands chunks to `StreamDecoder.feed` as they arrive; chunk boundaries fall
anywhere, including mid-line and mid-UTF-8 sequence. Only `data:` lines carry frames. Blank lines
separate events, and lines starting with `:` are keepalive comments.
"""

from __future__ import annotations

import asyncio
import codecs
import json
from collections.abc import AsyncIterator, Awaitable, Callable

from .errors import (
    EnvoyAuthError,
    EnvoyConnectionError,
    EnvoyError,
    EnvoyParseError,
    EnvoyStreamUnavailable,
)
from .models import StreamFrame


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


class Backoff:
    """Reconnect delays: 1 s, doubling to 60 s, back to 1 s after a good frame (spec 3.1)."""

    def __init__(self, initial: float = 1.0, maximum: float = 60.0) -> None:
        self._initial = initial
        self._maximum = maximum
        self._next = initial

    def next_delay(self) -> float:
        delay = self._next
        self._next = min(self._next * 2, self._maximum)
        return delay

    def reset(self) -> None:
        self._next = self._initial


async def run_stream(
    connect: Callable[[], AsyncIterator[StreamFrame]],
    on_frame: Callable[[StreamFrame], None],
    *,
    on_disconnect: Callable[[EnvoyError], None] | None = None,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    backoff: Backoff | None = None,
) -> None:
    """Keep a stream open for as long as the task runs, reconnecting with backoff.

    Connection drops, silences and bad frames reconnect. `EnvoyStreamUnavailable` and
    `EnvoyAuthError` are raised to the caller: retrying won't fix either. Cancel the task to
    stop.
    """
    backoff = backoff or Backoff()
    while True:
        try:
            async for frame in connect():
                backoff.reset()
                on_frame(frame)
            err: EnvoyError = EnvoyConnectionError("stream ended")
        except (EnvoyStreamUnavailable, EnvoyAuthError):
            raise
        except (EnvoyConnectionError, EnvoyParseError) as caught:
            err = caught
        if on_disconnect is not None:
            on_disconnect(err)
        await sleep(backoff.next_delay())
