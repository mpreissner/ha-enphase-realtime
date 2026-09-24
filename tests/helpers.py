"""Helpers for loading the captured payloads in tests/fixtures/ and faking servers."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

FIXTURES = Path(__file__).parent / "fixtures"


def load_text(name: str, layout: str = "reference") -> str:
    return (FIXTURES / layout / name).read_text()


def load_json(name: str, layout: str = "reference") -> Any:
    return json.loads(load_text(name, layout))


def make_jwt(claims: dict[str, Any]) -> str:
    """An unsigned-looking JWT carrying `claims`. The clients never check signatures."""

    def part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'ES256', 'typ': 'JWT'})}.{part(claims)}.c2lnbmF0dXJl"


@asynccontextmanager
async def serve(app: web.Application) -> AsyncIterator[str]:
    """Run `app` on a local port and yield its base URL."""
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/")
    finally:
        await server.close()
