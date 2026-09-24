"""Spikes S1/S2: log in to Enlighten once and report what the login provides.

    ENLIGHTEN_EMAIL=... ENLIGHTEN_PASSWORD=... python tools/spike_login.py [site_id]

Prints only names, shapes and yes/no answers, never cookie, token or account values, so the
output is safe to paste into an issue. Makes exactly one login attempt: repeated failures can
lock the account.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import aiohttp

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components/enphase_realtime")
)

from enlighten_client.battery import BatteryConfigClient
from enlighten_client.errors import EnlightenError
from enlighten_client.session import (
    BASE_URL,
    LOGIN_PATH,
    MANAGER_TOKEN_COOKIE,
    SESSION_COOKIE,
    EnlightenSession,
    _user_id_from_jwt,
)


def shape(value: object) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {shape(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return f"[{len(value)} items]"
    return type(value).__name__


async def main() -> None:
    email, password = os.environ["ENLIGHTEN_EMAIL"], os.environ["ENLIGHTEN_PASSWORD"]
    async with aiohttp.ClientSession() as http:
        # The raw login, to see what the server itself returns before the client fills gaps.
        form = {"user[email]": email, "user[password]": password}
        async with http.post(BASE_URL + LOGIN_PATH, data=form, allow_redirects=False) as resp:
            print("login status:", resp.status, resp.content_type)
            body = await resp.json(content_type=None)
            print("login body shape:", shape(body))
            print("cookies set by login:", sorted(m.key for m in resp.cookies.values()))
        if resp.status != 200 or not isinstance(body, dict) or "session_id" not in body:
            sys.exit("login refused; not retrying")

        jar = {m.key: m.value for m in http.cookie_jar}
        sid = body["session_id"]
        print("S1 server set session cookie:", SESSION_COOKIE in jar)
        print("S1 session cookie == session_id:", jar.get(SESSION_COOKIE) == sid)
        print("S2 user_id in body:", "user_id" in body)
        print(
            "S2 user_id in manager_token field:",
            _user_id_from_jwt(body.get("manager_token")) is not None,
        )
        print(
            "S2 user_id in manager token cookie:",
            _user_id_from_jwt(jar.get(MANAGER_TOKEN_COOKIE)) is not None,
        )

        # Reuse this login in the client rather than logging in a second time.
        cloud = EnlightenSession(http, email, password)
        cloud.session_id = sid
        cloud.user_id = cloud._find_user_id(body)
        if jar.get(SESSION_COOKIE) != sid:
            http.cookie_jar.update_cookies({SESSION_COOKIE: sid}, response_url=cloud._base)
        try:
            sites = await cloud.search_sites()
            print("search_sites:", len(sites), "site(s)")
            site_id = int(sys.argv[1]) if len(sys.argv) > 1 else (sites[0].id if sites else None)
            if site_id is None:
                sys.exit("no site ID; pass it as an argument to test batterySettings")
            settings = await BatteryConfigClient(cloud, site_id).battery_settings()
            print("S1 batterySettings GET works: True (profile read:", bool(settings.profile), ")")
            print("XSRF cookie set:", cloud.cookie("BP-XSRF-Token") is not None)
        except EnlightenError as err:
            print("request failed:", type(err).__name__, err)


if __name__ == "__main__":
    asyncio.run(main())
