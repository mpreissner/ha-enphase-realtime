"""The Envoy's local HTTPS API.

The caller supplies the aiohttp session. In Home Assistant it's one made with
`verify_ssl=False`, because the Envoy's certificate is self-signed (spec 3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import aiohttp

from .errors import EnvoyAuthError, EnvoyConnectionError, EnvoyParseError, EnvoyStreamUnavailable
from .models import (
    DryContactSettings,
    EnvoyInfo,
    Inventory,
    Inverter,
    LiveData,
    Meter,
    Relay,
    Schedule,
    SecCtrl,
    StreamFrame,
    parse_dry_contact_states,
)
from .stream import StreamDecoder

# Returns a fresh owner token. Called once after a 401 before giving up (spec 4.2).
TokenRefresher = Callable[[], Awaitable[str]]

FAST_TIMEOUT = aiohttp.ClientTimeout(total=10)
# The 1 s live poll gives up quickly; the next poll is a second away (spec 7).
LIVE_TIMEOUT = aiohttp.ClientTimeout(total=3)
# Readings and reports walk every meter channel and are slower on a busy Envoy.
SLOW_TIMEOUT = aiohttp.ClientTimeout(total=20)
# The stream sends a frame about once a second; 30 s of silence means it's dead (spec 3.1).
STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=30)


class EnvoyClient:
    """Read-only access to the endpoints in spec 3.1.

    Each typed method keeps the raw body in `last_payloads`, keyed by path, for diagnostics.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        token: str | None = None,
        *,
        token_refresher: TokenRefresher | None = None,
    ) -> None:
        self._session = session
        # A bare host means HTTPS. A full URL is accepted as given (tests use plain HTTP).
        self._base = host.rstrip("/") if "://" in host else f"https://{host}"
        self._token = token
        self._refresh = token_refresher
        self.last_payloads: dict[str, Any] = {}

    @property
    def token(self) -> str | None:
        return self._token

    def set_token(self, token: str) -> None:
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def _renew_token(self) -> bool:
        """Swap in a fresh token after a 401. False when there's no way to get one."""
        if self._refresh is None:
            return False
        self._token = await self._refresh()
        return True

    async def get_json(self, path: str, timeout: aiohttp.ClientTimeout = FAST_TIMEOUT) -> Any:
        """GET an authenticated endpoint and return its decoded JSON body."""
        payload = await self._request_json("GET", path, timeout)
        self.last_payloads[path] = payload
        return payload

    async def post_json(
        self, path: str, body: Any, timeout: aiohttp.ClientTimeout = FAST_TIMEOUT
    ) -> Any:
        """POST a JSON body to an authenticated endpoint and return its decoded JSON reply."""
        return await self._request_json("POST", path, timeout, body)

    async def _request_json(
        self, method: str, path: str, timeout: aiohttp.ClientTimeout, body: Any = None
    ) -> Any:
        for attempt in range(2):
            try:
                async with self._session.request(
                    method, self._base + path, headers=self._headers(), json=body, timeout=timeout
                ) as resp:
                    if resp.status == 401 and attempt == 0 and await self._renew_token():
                        continue
                    if resp.status == 401:
                        raise EnvoyAuthError(f"{path}: owner token refused")
                    if resp.status != 200:
                        raise EnvoyConnectionError(f"{path}: HTTP {resp.status}")
                    try:
                        payload = await resp.json(content_type=None)
                    except ValueError as err:
                        raise EnvoyParseError(f"{path}: body isn't JSON") from err
            except (aiohttp.ClientError, TimeoutError) as err:
                raise EnvoyConnectionError(f"{path}: {err!r}") from err
            return payload
        raise AssertionError("unreachable")  # pragma: no cover

    # --- setup ----------------------------------------------------------------------------------

    async def info(self) -> EnvoyInfo:
        """`/info` needs no token, so setup can read the serial before it has one."""
        try:
            async with self._session.get(self._base + "/info", timeout=FAST_TIMEOUT) as resp:
                if resp.status != 200:
                    raise EnvoyConnectionError(f"/info: HTTP {resp.status}")
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EnvoyConnectionError(f"/info: {err!r}") from err
        self.last_payloads["/info"] = text
        return EnvoyInfo.from_xml(text)

    # --- live tick ------------------------------------------------------------------------------

    async def livedata(self, timeout: aiohttp.ClientTimeout = LIVE_TIMEOUT) -> LiveData:
        return LiveData.from_payload(await self.get_json("/ivp/livedata/status", timeout))

    async def relay(self) -> Relay:
        return Relay.from_payload(await self.get_json("/ivp/ensemble/relay", LIVE_TIMEOUT))

    async def set_relay(self, closed: bool) -> None:
        """Tell the System Controller to close (on grid) or open (off grid) the main relay. The
        reply isn't used; the live poll's `mains_admin_state` and `mains_oper_state` confirm it
        (spec 6.3)."""
        state = "closed" if closed else "open"
        await self.post_json("/ivp/ensemble/relay", {"mains_admin_state": state})

    async def enable_livedata_stream(self) -> None:
        """Ask the System Controller to keep `livedata` fresh (`sc_stream`, spec 3.1). The
        reply isn't used."""
        await self.post_json("/ivp/livedata/stream", {"enable": 1}, LIVE_TIMEOUT)

    # --- fast tick ------------------------------------------------------------------------------

    async def secctrl(self) -> SecCtrl:
        return SecCtrl.from_payload(await self.get_json("/ivp/ensemble/secctrl"))

    async def schedule(self) -> Schedule:
        return Schedule.from_payload(await self.get_json("/ivp/sc/sched"))

    # --- slow tick ------------------------------------------------------------------------------

    async def meters(self) -> list[Meter]:
        return Meter.parse_list(await self.get_json("/ivp/meters"))

    async def meter_readings(self) -> Any:
        """Raw; `LifetimeEnergy.from_payloads` needs it together with meters and reports."""
        return await self.get_json("/ivp/meters/readings", SLOW_TIMEOUT)

    async def meter_reports(self) -> Any:
        return await self.get_json("/ivp/meters/reports", SLOW_TIMEOUT)

    async def inventory(self) -> Inventory:
        return Inventory.from_payload(await self.get_json("/ivp/ensemble/inventory"))

    async def dry_contact_settings(self) -> dict[str, DryContactSettings]:
        return DryContactSettings.parse_dict(await self.get_json("/ivp/ss/dry_contact_settings"))

    async def dry_contact_states(self) -> dict[str, bool]:
        return parse_dry_contact_states(await self.get_json("/ivp/ensemble/dry_contacts"))

    async def set_dry_contact(self, contact_id: str, closed: bool) -> None:
        """Close or open one dry contact. The reply isn't used (docs/specs/dry-contacts.md)."""
        status = "closed" if closed else "open"
        await self.post_json(
            "/ivp/ensemble/dry_contacts", {"dry_contacts": {"id": contact_id, "status": status}}
        )

    async def set_dry_contact_settings(self, settings: dict[str, Any]) -> None:
        """Write one contact's settings. `settings` must be the contact's full object: a partial
        one may crash the Envoy."""
        await self.post_json("/ivp/ss/dry_contact_settings", {"dry_contacts": settings})

    async def inverters(self) -> list[Inverter]:
        return Inverter.parse_list(await self.get_json("/api/v1/production/inverters"))

    # --- stream ---------------------------------------------------------------------------------

    async def stream_frames(self) -> AsyncIterator[StreamFrame]:
        """Yield frames from one `/stream/meter` connection until it ends or goes quiet.

        A 401 gets one token renewal. A 401 after that, or a 404, raises
        `EnvoyStreamUnavailable`: the Envoy doesn't offer the stream to this token.
        """
        for attempt in range(2):
            try:
                async with self._session.get(
                    self._base + "/stream/meter", headers=self._headers(), timeout=STREAM_TIMEOUT
                ) as resp:
                    if resp.status == 401 and attempt == 0 and await self._renew_token():
                        continue
                    if resp.status in (401, 404):
                        raise EnvoyStreamUnavailable(f"/stream/meter: HTTP {resp.status}")
                    if resp.status != 200:
                        raise EnvoyConnectionError(f"/stream/meter: HTTP {resp.status}")
                    decoder = StreamDecoder()
                    async for chunk in resp.content.iter_any():
                        for frame in decoder.feed(chunk):
                            yield frame
                    return
            except (aiohttp.ClientError, TimeoutError) as err:
                raise EnvoyConnectionError(f"/stream/meter: {err!r}") from err
