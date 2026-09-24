"""Owner tokens for the Envoy's local API.

An Enlighten `session_id` (from `POST /login/login.json`) is swapped at Entrez for a token scoped
to one Envoy serial. The Envoy takes it as `Authorization: Bearer <token>`. Tokens last about a
year; the integration renews one within 30 days of `exp` (spec 4.2).
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from .errors import EnvoyAuthError, EnvoyConnectionError

ENTREZ_TOKEN_URL = "https://entrez.enphaseenergy.com/tokens"
CLOUD_TIMEOUT = aiohttp.ClientTimeout(total=30)


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT's payload without checking the signature. The Envoy checks it; we only read
    `exp`. Returns `{}` for anything that isn't a readable JWT."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except IndexError, ValueError, binascii.Error, UnicodeDecodeError:
        return {}
    return claims if isinstance(claims, dict) else {}


@dataclass(frozen=True, slots=True)
class OwnerToken:
    token: str
    expires_at: datetime | None

    @classmethod
    def from_jwt(cls, token: str) -> OwnerToken:
        exp = jwt_claims(token).get("exp")
        expires_at = datetime.fromtimestamp(exp, UTC) if isinstance(exp, int | float) else None
        return cls(token=token, expires_at=expires_at)

    def expires_within(self, window: timedelta, now: datetime | None = None) -> bool:
        """True when the token should be renewed. A token without `exp` is never renewed early;
        a 401 renews it instead."""
        if self.expires_at is None:
            return False
        return self.expires_at - (now or datetime.now(UTC)) <= window


async def fetch_owner_token(
    session: aiohttp.ClientSession,
    *,
    session_id: str,
    serial: str,
    username: str,
    url: str = ENTREZ_TOKEN_URL,
) -> OwnerToken:
    """Ask Entrez for an owner token. The body is the token itself, as plain text."""
    body = {"session_id": session_id, "serial_num": serial, "username": username}
    try:
        async with session.post(url, json=body, timeout=CLOUD_TIMEOUT) as resp:
            text = (await resp.text()).strip()
            status = resp.status
    except (aiohttp.ClientError, TimeoutError) as err:
        raise EnvoyConnectionError(f"Entrez unreachable: {err!r}") from err
    if status >= 500:
        raise EnvoyConnectionError(f"Entrez returned {status}")
    if status != 200:
        raise EnvoyAuthError(f"Entrez refused the token request ({status})")
    if text.count(".") != 2 or not jwt_claims(text):
        raise EnvoyAuthError("Entrez didn't return a token")
    return OwnerToken.from_jwt(text)
