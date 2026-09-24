"""Owner-token upkeep: fetch, renew and persist the Envoy's token (spec 4.1, 4.2)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import CONF_TOKEN, TOKEN_RENEW_WINDOW
from .enlighten_client.errors import EnlightenAuthError, EnlightenError
from .enlighten_client.session import EnlightenSession
from .envoy_client.auth import OwnerToken, fetch_owner_token
from .envoy_client.errors import EnvoyAuthError, EnvoyError

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Calls that all hit a 401 at once (the fast tick gathers four) share one renewal.
_RECENT_RENEWAL = 60.0


@contextmanager
def translate_errors(what: str):
    """Turn client errors into what a coordinator raises: a rejected login starts reauth, and
    anything else fails just this update."""
    try:
        yield
    except (EnlightenAuthError, EnvoyAuthError) as err:
        raise ConfigEntryAuthFailed(f"{what}: {err}") from err
    except (EnlightenError, EnvoyError) as err:
        raise UpdateFailed(f"{what}: {err}") from err


async def request_owner_token(
    cloud: EnlightenSession, http: aiohttp.ClientSession, serial: str
) -> OwnerToken:
    """Swap the Enlighten session for an owner token, logging in first if needed. Entrez
    refusing a session that has gone stale gets one fresh login."""
    if cloud.session_id is None:
        await cloud.login()
    for attempt in range(2):
        assert cloud.session_id is not None  # noqa: S101 - login() sets it or raises
        try:
            return await fetch_owner_token(
                http, session_id=cloud.session_id, serial=serial, username=cloud.email
            )
        except EnvoyAuthError:
            if attempt:
                raise
            await cloud.login()
    raise AssertionError("unreachable")  # pragma: no cover


class TokenKeeper:
    """Hands the Envoy client fresh tokens and saves each one in the config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        cloud: EnlightenSession,
        http: aiohttp.ClientSession,
        serial: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._cloud = cloud
        self._http = http
        self._serial = serial
        self._clock = clock
        self._lock = asyncio.Lock()
        self._renewed_at: float | None = None
        self.token: str | None = entry.data.get(CONF_TOKEN)

    async def refresh(self) -> str:
        """A new token, for `EnvoyClient`'s 401 retry. Concurrent callers share one renewal."""
        async with self._lock:
            recent = self._renewed_at is not None and (
                self._clock() - self._renewed_at < _RECENT_RENEWAL
            )
            if recent and self.token:
                return self.token
            owner = await request_owner_token(self._cloud, self._http, self._serial)
            self._renewed_at = self._clock()
            self.token = owner.token
            self._hass.config_entries.async_update_entry(
                self._entry, data={**self._entry.data, CONF_TOKEN: owner.token}
            )
            _LOGGER.debug("Owner token renewed; expires %s", owner.expires_at)
            return owner.token

    def needs_renewal(self) -> bool:
        return self.token is None or OwnerToken.from_jwt(self.token).expires_within(
            TOKEN_RENEW_WINDOW
        )
