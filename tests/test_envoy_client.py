"""The Envoy transport against a fake Envoy: bearer auth, 401 renewal, the stream and backoff."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from aiohttp import web
from envoy_client.auth import OwnerToken, fetch_owner_token, jwt_claims
from envoy_client.errors import (
    EnvoyAuthError,
    EnvoyConnectionError,
    EnvoyParseError,
    EnvoyStreamUnavailable,
)
from envoy_client.local import EnvoyClient
from envoy_client.models import LiveData, StreamFrame
from envoy_client.stream import Backoff, StreamDecoder, run_stream

from tests.helpers import load_json, load_text, make_jwt, serve

STREAM = load_text("stream_meter.txt").encode()


class FakeEnvoy:
    """Accepts one token at a time; `valid` changes when the test "rotates" it."""

    def __init__(self, valid: str = "good") -> None:
        self.valid = valid
        self.stream_status = 200
        self.seen_tokens: list[str | None] = []
        self.posted: list[object] = []
        self.app = web.Application()
        self.app.router.add_get("/info", self.info)
        self.app.router.add_get("/ivp/livedata/status", self.livedata)
        self.app.router.add_post("/ivp/livedata/stream", self.enable_stream)
        self.app.router.add_get("/ivp/meters/readings", self.garbage)
        self.app.router.add_get("/ivp/meters/reports", self.broken)
        self.app.router.add_get("/stream/meter", self.stream)

    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization")
        self.seen_tokens.append(header.removeprefix("Bearer ") if header else None)
        return header == f"Bearer {self.valid}"

    async def info(self, request: web.Request) -> web.Response:
        return web.Response(text=load_text("info.xml"), content_type="application/xml")

    async def livedata(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=401)
        return web.json_response(load_json("ivp_livedata_status.json"))

    async def enable_stream(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=401)
        self.posted.append(await request.json())
        return web.json_response({"sc_stream": "enabled"})

    async def garbage(self, request: web.Request) -> web.Response:
        return web.Response(text="<html>not json</html>", content_type="text/html")

    async def broken(self, request: web.Request) -> web.Response:
        return web.Response(status=503)

    async def stream(self, request: web.Request) -> web.StreamResponse:
        if self.stream_status != 200:
            return web.Response(status=self.stream_status)
        if not self._authorized(request):
            return web.Response(status=401)
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        await resp.prepare(request)
        # Odd-sized chunks so frames (and multi-byte characters, if any) straddle writes.
        for i in range(0, len(STREAM), 37):
            await resp.write(STREAM[i : i + 37])
        await resp.write_eof()
        return resp


@pytest.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


def _refresher(token: str, calls: list[int]):
    async def refresh() -> str:
        calls.append(1)
        return token

    return refresh


async def test_info_needs_no_token(http: aiohttp.ClientSession) -> None:
    envoy = FakeEnvoy()
    async with serve(envoy.app) as url:
        info = await EnvoyClient(http, url).info()
    assert info.serial == "900000000001"


async def test_bearer_token_is_sent(http: aiohttp.ClientSession) -> None:
    envoy = FakeEnvoy()
    async with serve(envoy.app) as url:
        client = EnvoyClient(http, url, "good")
        live = await client.livedata()
    assert envoy.seen_tokens == ["good"]
    assert live == LiveData.from_payload(load_json("ivp_livedata_status.json"))
    assert "/ivp/livedata/status" in client.last_payloads


async def test_401_renews_the_token_once_and_retries(http: aiohttp.ClientSession) -> None:
    envoy = FakeEnvoy(valid="fresh")
    calls: list[int] = []
    async with serve(envoy.app) as url:
        client = EnvoyClient(http, url, "stale", token_refresher=_refresher("fresh", calls))
        await client.livedata()
    assert envoy.seen_tokens == ["stale", "fresh"]
    assert len(calls) == 1
    assert client.token == "fresh"


async def test_enable_livedata_stream_posts_and_renews(http: aiohttp.ClientSession) -> None:
    envoy = FakeEnvoy(valid="fresh")
    calls: list[int] = []
    async with serve(envoy.app) as url:
        client = EnvoyClient(http, url, "stale", token_refresher=_refresher("fresh", calls))
        await client.enable_livedata_stream()
    assert envoy.posted == [{"enable": 1}]
    assert envoy.seen_tokens == ["stale", "fresh"]
    # A write's reply isn't a payload any entity reads.
    assert client.last_payloads == {}


async def test_401_after_renewal_is_an_auth_error(http: aiohttp.ClientSession) -> None:
    envoy = FakeEnvoy(valid="never")
    calls: list[int] = []
    async with serve(envoy.app) as url:
        client = EnvoyClient(http, url, "stale", token_refresher=_refresher("also-bad", calls))
        with pytest.raises(EnvoyAuthError):
            await client.livedata()
    assert len(calls) == 1
    assert envoy.seen_tokens == ["stale", "also-bad"]


async def test_401_without_a_refresher_is_an_auth_error(http: aiohttp.ClientSession) -> None:
    async with serve(FakeEnvoy().app) as url:
        with pytest.raises(EnvoyAuthError):
            await EnvoyClient(http, url, "stale").livedata()


async def test_server_errors_and_bad_bodies(http: aiohttp.ClientSession) -> None:
    async with serve(FakeEnvoy().app) as url:
        client = EnvoyClient(http, url, "good")
        with pytest.raises(EnvoyParseError):
            await client.meter_readings()
        with pytest.raises(EnvoyConnectionError):
            await client.meter_reports()


async def test_unreachable_envoy_is_a_connection_error(http: aiohttp.ClientSession) -> None:
    async with serve(FakeEnvoy().app) as url:
        pass  # closed again: nothing is listening on the port any more
    with pytest.raises(EnvoyConnectionError):
        await EnvoyClient(http, url, "good").livedata()


async def test_stream_decodes_chunked_frames(http: aiohttp.ClientSession) -> None:
    expected = StreamDecoder().feed(STREAM)
    async with serve(FakeEnvoy().app) as url:
        frames = [f async for f in EnvoyClient(http, url, "good").stream_frames()]
    assert frames == expected
    assert len(frames) == 2


async def test_stream_renews_the_token_once(http: aiohttp.ClientSession) -> None:
    envoy = FakeEnvoy(valid="fresh")
    calls: list[int] = []
    async with serve(envoy.app) as url:
        client = EnvoyClient(http, url, "stale", token_refresher=_refresher("fresh", calls))
        frames = [f async for f in client.stream_frames()]
    assert len(frames) == 2
    assert len(calls) == 1


async def test_stream_401_after_renewal_is_unavailable(http: aiohttp.ClientSession) -> None:
    envoy = FakeEnvoy(valid="never")
    calls: list[int] = []
    async with serve(envoy.app) as url:
        client = EnvoyClient(http, url, "stale", token_refresher=_refresher("also-bad", calls))
        with pytest.raises(EnvoyStreamUnavailable):
            [f async for f in client.stream_frames()]
    assert len(calls) == 1


async def test_stream_404_is_unavailable(http: aiohttp.ClientSession) -> None:
    envoy = FakeEnvoy()
    envoy.stream_status = 404
    async with serve(envoy.app) as url:
        with pytest.raises(EnvoyStreamUnavailable):
            [f async for f in EnvoyClient(http, url, "good").stream_frames()]


# --- reconnect loop -----------------------------------------------------------------------------


def test_backoff_doubles_to_a_ceiling_and_resets() -> None:
    backoff = Backoff()
    assert [backoff.next_delay() for _ in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    backoff.reset()
    assert backoff.next_delay() == 1


async def test_run_stream_reconnects_with_backoff_and_stops_when_unavailable() -> None:
    frame = StreamDecoder().feed(STREAM)[0]
    script: list[object] = [
        EnvoyConnectionError("refused"),
        EnvoyConnectionError("refused"),
        [frame],  # a good connection resets the backoff, then the stream ends
        EnvoyConnectionError("refused"),
        EnvoyStreamUnavailable("404"),
    ]
    delays: list[float] = []
    received: list[StreamFrame] = []
    drops: list[str] = []

    def connect() -> AsyncIterator[StreamFrame]:
        step = script.pop(0)

        async def gen() -> AsyncIterator[StreamFrame]:
            if isinstance(step, Exception):
                raise step
            for f in step:
                yield f

        return gen()

    async def sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(EnvoyStreamUnavailable):
        await run_stream(
            connect, received.append, on_disconnect=lambda e: drops.append(str(e)), sleep=sleep
        )
    assert received == [frame]
    assert delays == [1, 2, 1, 2]
    assert drops == ["refused", "refused", "stream ended", "refused"]


# --- owner tokens -------------------------------------------------------------------------------


async def test_fetch_owner_token_uses_serial_num(http: aiohttp.ClientSession) -> None:
    exp = int(datetime(2027, 1, 1, tzinfo=UTC).timestamp())
    token = make_jwt({"exp": exp, "enphaseUser": "owner"})
    bodies: list[dict] = []

    async def tokens(request: web.Request) -> web.Response:
        bodies.append(await request.json())
        return web.Response(text=token + "\n")

    app = web.Application()
    app.router.add_post("/tokens", tokens)
    async with serve(app) as url:
        owner = await fetch_owner_token(
            http, session_id="sid", serial="900000000001", username="o@x", url=f"{url}/tokens"
        )
    assert bodies == [{"session_id": "sid", "serial_num": "900000000001", "username": "o@x"}]
    assert owner.token == token
    assert owner.expires_at == datetime(2027, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "text", "error"),
    [
        (401, "no", EnvoyAuthError),
        (200, "<html>login</html>", EnvoyAuthError),
        (502, "", EnvoyConnectionError),
    ],
)
async def test_fetch_owner_token_failures(
    http: aiohttp.ClientSession, status: int, text: str, error: type[Exception]
) -> None:
    async def tokens(request: web.Request) -> web.Response:
        return web.Response(status=status, text=text)

    app = web.Application()
    app.router.add_post("/tokens", tokens)
    async with serve(app) as url:
        with pytest.raises(error):
            await fetch_owner_token(
                http, session_id="s", serial="1", username="u", url=f"{url}/tokens"
            )


def test_owner_token_renewal_window() -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    soon = OwnerToken.from_jwt(make_jwt({"exp": int((now + timedelta(days=29)).timestamp())}))
    later = OwnerToken.from_jwt(make_jwt({"exp": int((now + timedelta(days=31)).timestamp())}))
    assert soon.expires_within(timedelta(days=30), now)
    assert not later.expires_within(timedelta(days=30), now)
    assert not OwnerToken.from_jwt("not-a-jwt").expires_within(timedelta(days=30), now)


def test_jwt_claims_tolerates_garbage() -> None:
    assert jwt_claims("a.!!!.c") == {}
    assert jwt_claims("") == {}
