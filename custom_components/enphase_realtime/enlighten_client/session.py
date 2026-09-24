"""A logged-in Enlighten session.

Pass a dedicated aiohttp session (in Home Assistant, `async_create_clientsession`), not a shared
one: the login lives in its cookie jar.

The Enlighten website authenticates with the `_enlighten_4_session` cookie, whose value is the
`session_id` from `login.json`. When the login response doesn't set it, it's set here (spike S1).
batteryConfig calls also need the numeric user ID (spike S2), found in the login body or in the
`data` claim of a manager-token JWT.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
from yarl import URL

from .errors import (
    EnlightenAuthError,
    EnlightenConnectionError,
    EnlightenError,
    EnlightenParseError,
)
from .models import GridControlCheck, Site, parse_search_sites

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://enlighten.enphaseenergy.com"
LOGIN_PATH = "/login/login.json"
SESSION_COOKIE = "_enlighten_4_session"
MANAGER_TOKEN_COOKIE = "enlighten_manager_token_production"
CLOUD_TIMEOUT = aiohttp.ClientTimeout(total=30)

# How an expired session answers: a redirect to the login page, a refusal, or the page itself.
_SESSION_EXPIRED = frozenset({301, 302, 303, 307, 308, 401, 403})


# app-api calls. The app also sends `e-auth-token: null`; the cookie is what authenticates.
_APP_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def _user_id_from_jwt(token: Any) -> int | None:
    """`data.user_id` from a manager-token JWT, signature unchecked. The two client packages
    stay independent, so this doesn't share the Envoy client's decoder."""
    if not isinstance(token, str):
        return None
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return None
    data = claims.get("data") if isinstance(claims, dict) else None
    user_id = data.get("user_id") if isinstance(data, dict) else None
    return user_id if isinstance(user_id, int) else None


class EnlightenSession:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        *,
        base_url: str = BASE_URL,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._base = URL(base_url)
        self.session_id: str | None = None
        self.user_id: int | None = None

    @property
    def email(self) -> str:
        return self._email

    def cookie(self, name: str) -> str | None:
        """A cookie's value from the jar, whatever its domain or path."""
        for morsel in self._session.cookie_jar:
            if morsel.key == name:
                return morsel.value
        return None

    async def login(self) -> None:
        """Log in once. A refusal raises `EnlightenAuthError` and is never retried here."""
        form = {"user[email]": self._email, "user[password]": self._password}
        try:
            async with self._session.post(
                self._base.join(URL(LOGIN_PATH)),
                data=form,
                allow_redirects=False,
                timeout=CLOUD_TIMEOUT,
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EnlightenConnectionError(f"login: {err!r}") from err
        if status >= 500:
            raise EnlightenConnectionError(f"login: HTTP {status}")
        session_id = body.get("session_id") if isinstance(body, dict) else None
        if status != 200 or not isinstance(session_id, str) or not session_id:
            raise EnlightenAuthError(f"login refused (HTTP {status})")

        self.session_id = session_id
        if self.cookie(SESSION_COOKIE) != session_id:
            self._session.cookie_jar.update_cookies(
                {SESSION_COOKIE: session_id}, response_url=self._base
            )
        self.user_id = self._find_user_id(body)
        if self.user_id is None:
            _LOGGER.warning("Enlighten login succeeded but no user ID was found (spike S2)")

    def _find_user_id(self, body: Mapping[str, Any]) -> int | None:
        user_id = body.get("user_id")
        if isinstance(user_id, int):
            return user_id
        if isinstance(user_id, str) and user_id.isdigit():
            return int(user_id)
        return _user_id_from_jwt(body.get("manager_token")) or _user_id_from_jwt(
            self.cookie(MANAGER_TOKEN_COOKIE)
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        relogin: bool = True,
    ) -> Any:
        """Send a request with the session cookie and return the decoded JSON body.

        When the session has expired, log in again and retry once (spec 4.2). With
        `relogin=False` an expired session raises `EnlightenAuthError` straight away, for
        callers that must rebuild the request after a new login.
        """
        if self.session_id is None:
            await self.login()
        for attempt in range(2):
            try:
                async with self._session.request(
                    method,
                    self._base.join(URL(path)),
                    params=params,
                    json=json,
                    headers=headers,
                    allow_redirects=False,
                    timeout=CLOUD_TIMEOUT,
                ) as resp:
                    expired = resp.status in _SESSION_EXPIRED or resp.content_type == "text/html"
                    if not expired:
                        if resp.status >= 500:
                            raise EnlightenConnectionError(f"{method} {path}: HTTP {resp.status}")
                        if resp.status >= 400:
                            raise EnlightenError(f"{method} {path}: HTTP {resp.status}")
                        try:
                            return await resp.json(content_type=None)
                        except ValueError as err:
                            raise EnlightenParseError(f"{method} {path}: body isn't JSON") from err
            except (aiohttp.ClientError, TimeoutError) as err:
                raise EnlightenConnectionError(f"{method} {path}: {err!r}") from err
            if attempt or not relogin:
                raise EnlightenAuthError(f"{method} {path}: session rejected")
            _LOGGER.debug("Enlighten session expired; logging in again")
            await self.login()
        raise AssertionError("unreachable")  # pragma: no cover

    # --- app-api ------------------------------------------------------------------------------

    async def search_sites(self) -> list[Site]:
        """May come back empty for an account that has a site (spike S2); callers retry later."""
        data = await self.request(
            "GET",
            "/app-api/search_sites.json",
            params={"searchText": "", "favourite": "true"},
            headers=_APP_HEADERS,
        )
        return parse_search_sites(data)

    async def grid_control_check(self, site_id: int) -> GridControlCheck:
        data = await self.request(
            "GET", f"/app-api/{site_id}/grid_control_check.json", headers=_APP_HEADERS
        )
        return GridControlCheck.from_payload(data)
