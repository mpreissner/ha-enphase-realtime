"""The batteryConfig service behind the Enphase app's battery settings screen (spec 3.3).

Reads need the session cookie plus a `Username: <user id>` header. Writes also need an
`X-XSRF-Token` header equal to the `BP-XSRF-Token` cookie, which every batteryConfig GET resets.
So each write is a fresh GET, then the write with the cookie that GET left behind.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from .errors import EnlightenAuthError, EnlightenError
from .models import BatterySettings, SiteSettings
from .session import EnlightenSession

API = "/service/batteryConfig/api/v1"
XSRF_COOKIE = "BP-XSRF-Token"
APP_ORIGIN = "https://battery-profile-ui.enphaseenergy.com"


class BatteryConfigClient:
    def __init__(self, cloud: EnlightenSession, site_id: int) -> None:
        self._cloud = cloud
        self._site = site_id

    async def _user_id(self) -> int:
        if self._cloud.user_id is None:
            await self._cloud.login()
        if self._cloud.user_id is None:
            raise EnlightenError("Enlighten user ID unknown; battery settings are unavailable")
        return self._cloud.user_id

    async def _headers(self) -> dict[str, str]:
        return {
            "Username": str(await self._user_id()),
            "Requestid": str(uuid.uuid4()),
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
        }

    async def _get_battery_settings(self, *, relogin: bool = True) -> Any:
        return await self._cloud.request(
            "GET",
            f"{API}/batterySettings/{self._site}",
            params={"userId": str(await self._user_id()), "source": "enho"},
            headers=await self._headers(),
            relogin=relogin,
        )

    async def site_settings(self) -> SiteSettings:
        data = await self._cloud.request(
            "GET",
            f"{API}/siteSettings/{self._site}",
            params={"userId": str(await self._user_id())},
            headers=await self._headers(),
        )
        return SiteSettings.from_payload(data)

    async def battery_settings(self) -> BatterySettings:
        return BatterySettings.from_payload(await self._get_battery_settings())

    async def _write(
        self, method: str, path: str, body: Any, params: Mapping[str, str] | None = None
    ) -> Any:
        """GET for a fresh XSRF cookie, then write. If the session expires in between, log in
        once and start over from the GET: the old XSRF token is useless after a new login."""
        for attempt in range(2):
            await self._get_battery_settings()
            xsrf = self._cloud.cookie(XSRF_COOKIE)
            if xsrf is None:
                raise EnlightenError("batterySettings GET didn't set the XSRF cookie")
            headers = await self._headers() | {"X-XSRF-Token": xsrf}
            try:
                return await self._cloud.request(
                    method, path, params=params, json=body, headers=headers, relogin=False
                )
            except EnlightenAuthError:
                if attempt:
                    raise
                await self._cloud.login()
        raise AssertionError("unreachable")  # pragma: no cover

    async def update_battery_settings(self, changes: Mapping[str, Any]) -> Any:
        """PUT a partial body, e.g. `{"batteryBackupPercentage": 30}`. The change isn't applied
        until the gateways pick it up; `BatterySettings.has_pending_change` tracks that."""
        return await self._write(
            "PUT",
            f"{API}/batterySettings/{self._site}",
            dict(changes),
            params={"userId": str(await self._user_id())},
        )

    async def accept_disclaimer(self, kind: str = "itc") -> Any:
        return await self._write(
            "POST",
            f"{API}/batterySettings/acceptDisclaimer/{self._site}",
            {"disclaimer-type": kind},
        )
