"""The Enlighten session and batteryConfig client against a fake Enlighten.

Covers the XSRF refresh-then-write sequence, re-login on an expired session, and that a refused
login is never retried (spec 4.2, 8).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiohttp
import pytest
from aiohttp import web
from enlighten_client.battery import BatteryConfigClient
from enlighten_client.errors import EnlightenAuthError, EnlightenError
from enlighten_client.session import EnlightenSession

from tests.helpers import load_json, make_jwt, serve

SITE = 1234567
USER = 7654321
PAGE = "/service/batteryConfig/api/v1"


class FakeEnlighten:
    def __init__(self) -> None:
        self.logins = 0
        self.reject_login = False
        self.sid: str | None = None  # the one live session; None once it has expired
        self.xsrf: str | None = None
        self.xsrf_issued = 0
        # Where the login puts the user ID: "body", "manager_token", "cookie" or None.
        self.user_id_in: str | None = "manager_token"
        self.sets_session_cookie = False
        self.expired_as_html = False
        self.expire_before_write = 0  # expire the session on this many upcoming writes
        self.events: list[tuple] = []

        self.app = web.Application()
        r = self.app.router
        r.add_post("/login/login.json", self.login)
        r.add_get("/login", self.login_page)
        r.add_get(f"{PAGE}/batterySettings/{{site}}", self.get_battery)
        r.add_put(f"{PAGE}/batterySettings/{{site}}", self.write)
        r.add_post(f"{PAGE}/batterySettings/acceptDisclaimer/{{site}}", self.write)
        r.add_get(f"{PAGE}/siteSettings/{{site}}", self.site_settings)
        r.add_get("/app-api/search_sites.json", self.search_sites)
        r.add_get("/app-api/{site}/grid_control_check.json", self.grid_control_check)

    def expire(self) -> None:
        self.sid = None

    async def login(self, request: web.Request) -> web.Response:
        self.logins += 1
        form = await request.post()
        assert form["user[email]"] == "owner@example.com"
        if self.reject_login or form["user[password]"] != "hunter2":
            return web.json_response({"message": "Invalid email or password"}, status=401)
        self.sid = f"sid-{self.logins}"
        body: dict = {"session_id": self.sid, "message": "success"}
        manager = make_jwt({"data": {"user_id": USER, "session_id": self.sid}, "exp": 2**31})
        if self.user_id_in == "body":
            body["user_id"] = USER
        elif self.user_id_in == "manager_token":
            body["manager_token"] = manager
        resp = web.json_response(body)
        if self.user_id_in == "cookie":
            resp.set_cookie("enlighten_manager_token_production", manager)
        if self.sets_session_cookie:
            resp.set_cookie("_enlighten_4_session", self.sid)
        return resp

    async def login_page(self, request: web.Request) -> web.Response:
        return web.Response(text="<html>sign in</html>", content_type="text/html")

    def _session_ok(self, request: web.Request) -> bool:
        return self.sid is not None and request.cookies.get("_enlighten_4_session") == self.sid

    def _expired(self) -> web.Response:
        if self.expired_as_html:
            return web.Response(text="<html>sign in</html>", content_type="text/html")
        raise web.HTTPFound("/login")

    def _check_headers(self, request: web.Request) -> None:
        assert request.headers["Username"] == str(USER)
        assert request.headers["Origin"] == "https://battery-profile-ui.enphaseenergy.com"
        assert len(request.headers["Requestid"]) == 36

    async def get_battery(self, request: web.Request) -> web.Response:
        if not self._session_ok(request):
            self.events.append(("GET", "expired"))
            return self._expired()
        self._check_headers(request)
        assert request.query["userId"] == str(USER)
        assert request.query["source"] == "enho"
        self.xsrf_issued += 1
        self.xsrf = f"xsrf-{self.xsrf_issued}"
        self.events.append(("GET", self.xsrf))
        resp = web.json_response(load_json("cloud_battery_settings.json"))
        resp.set_cookie("BP-XSRF-Token", self.xsrf, path="/")
        return resp

    async def write(self, request: web.Request) -> web.Response:
        if self.expire_before_write:
            self.expire_before_write -= 1
            self.expire()
        if not self._session_ok(request):
            self.events.append((request.method, "expired"))
            return self._expired()
        self._check_headers(request)
        sent = request.headers.get("X-XSRF-Token")
        if sent != self.xsrf:
            self.events.append((request.method, "bad xsrf", sent))
            return web.json_response({"message": "Forbidden"}, status=403)
        self.events.append((request.method, sent, await request.json(), dict(request.query)))
        return web.json_response(load_json("cloud_put_response.json"))

    async def site_settings(self, request: web.Request) -> web.Response:
        if not self._session_ok(request):
            return self._expired()
        self._check_headers(request)
        return web.json_response(load_json("cloud_site_settings.json"))

    async def search_sites(self, request: web.Request) -> web.Response:
        if not self._session_ok(request):
            return self._expired()
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        return web.json_response(load_json("cloud_search_sites.json"))

    async def grid_control_check(self, request: web.Request) -> web.Response:
        if not self._session_ok(request):
            return self._expired()
        return web.json_response(load_json("cloud_grid_control_check.json"))


@pytest.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    # unsafe=True lets the jar keep cookies for 127.0.0.1; real hosts don't need it.
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        yield session


@pytest.fixture
async def fake() -> AsyncIterator[tuple[FakeEnlighten, str]]:
    server = FakeEnlighten()
    async with serve(server.app) as url:
        yield server, url


def _cloud(http: aiohttp.ClientSession, url: str, password: str = "hunter2") -> EnlightenSession:  # noqa: S107
    return EnlightenSession(http, "owner@example.com", password, base_url=url)


# --- login --------------------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["body", "manager_token", "cookie"])
async def test_login_sets_session_cookie_and_finds_user_id(
    http: aiohttp.ClientSession, fake: tuple[FakeEnlighten, str], where: str
) -> None:
    server, url = fake
    server.user_id_in = where
    cloud = _cloud(http, url)
    await cloud.login()
    assert cloud.session_id == "sid-1"
    assert cloud.cookie("_enlighten_4_session") == "sid-1"
    assert cloud.user_id == USER


async def test_login_keeps_a_session_cookie_the_server_set(
    http: aiohttp.ClientSession, fake: tuple[FakeEnlighten, str]
) -> None:
    server, url = fake
    server.sets_session_cookie = True
    cloud = _cloud(http, url)
    await cloud.login()
    assert [m.key for m in http.cookie_jar].count("_enlighten_4_session") == 1


async def test_login_without_a_user_id_leaves_battery_calls_unavailable(
    http: aiohttp.ClientSession, fake: tuple[FakeEnlighten, str]
) -> None:
    server, url = fake
    server.user_id_in = None
    cloud = _cloud(http, url)
    await cloud.login()
    assert cloud.user_id is None
    with pytest.raises(EnlightenError, match="user ID"):
        await BatteryConfigClient(cloud, SITE).battery_settings()


async def test_refused_login_is_not_retried(
    http: aiohttp.ClientSession, fake: tuple[FakeEnlighten, str]
) -> None:
    server, url = fake
    with pytest.raises(EnlightenAuthError):
        await BatteryConfigClient(_cloud(http, url, password="wrong"), SITE).battery_settings()
    assert server.logins == 1


async def test_refused_relogin_is_not_retried(
    http: aiohttp.ClientSession, fake: tuple[FakeEnlighten, str]
) -> None:
    server, url = fake
    cloud = _cloud(http, url)
    battery = BatteryConfigClient(cloud, SITE)
    await battery.battery_settings()
    server.expire()
    server.reject_login = True  # e.g. the password was changed
    with pytest.raises(EnlightenAuthError):
        await battery.battery_settings()
    assert server.logins == 2


# --- expired sessions ---------------------------------------------------------------------------


@pytest.mark.parametrize("as_html", [False, True])
async def test_expired_session_logs_in_again_once(
    http: aiohttp.ClientSession, fake: tuple[FakeEnlighten, str], as_html: bool
) -> None:
    server, url = fake
    server.expired_as_html = as_html
    cloud = _cloud(http, url)
    battery = BatteryConfigClient(cloud, SITE)
    await battery.battery_settings()
    server.expire()
    settings = await battery.battery_settings()
    assert settings.profile
    assert server.logins == 2
    assert cloud.session_id == "sid-2"


async def test_session_rejected_after_relogin_is_an_auth_error(
    http: aiohttp.ClientSession, fake: tuple[FakeEnlighten, str]
) -> None:
    server, url = fake
    cloud = _cloud(http, url)
    await cloud.login()
    # The login works but the session never does: one re-login, then give up.
    http.cookie_jar.clear()
    server.sid = "something-else"

    async def login_without_cookie() -> None:
        server.logins += 1

    cloud.login = login_without_cookie  # type: ignore[method-assign]
    with pytest.raises(EnlightenAuthError):
        await cloud.search_sites()
    assert server.logins == 2


# --- batteryConfig ------------------------------------------------------------------------------


async def test_reads(http: aiohttp.ClientSession, fake: tuple[FakeEnlighten, str]) -> None:
    _, url = fake
    cloud = _cloud(http, url)
    battery = BatteryConfigClient(cloud, SITE)
    assert (await battery.site_settings()).country_code == "US"
    assert (await battery.battery_settings()).has_pending_change is False
    assert [s.id for s in await cloud.search_sites()] == [SITE]
    assert (await cloud.grid_control_check(SITE)).flags


async def test_write_gets_a_fresh_xsrf_token_first(
    http: aiohttp.ClientSession, fake: tuple[FakeEnlighten, str]
) -> None:
    server, url = fake
    battery = BatteryConfigClient(_cloud(http, url), SITE)
    await battery.battery_settings()
    response = await battery.update_battery_settings({"batteryBackupPercentage": 30})
    assert response == load_json("cloud_put_response.json")
    assert server.events == [
        ("GET", "xsrf-1"),
        ("GET", "xsrf-2"),
        ("PUT", "xsrf-2", {"batteryBackupPercentage": 30}, {"userId": str(USER)}),
    ]


async def test_accept_disclaimer(
    http: aiohttp.ClientSession, fake: tuple[FakeEnlighten, str]
) -> None:
    server, url = fake
    await BatteryConfigClient(_cloud(http, url), SITE).accept_disclaimer()
    assert server.events == [("GET", "xsrf-1"), ("POST", "xsrf-1", {"disclaimer-type": "itc"}, {})]


async def test_write_after_session_expiry_starts_over_from_the_get(
    http: aiohttp.ClientSession, fake: tuple[FakeEnlighten, str]
) -> None:
    server, url = fake
    battery = BatteryConfigClient(_cloud(http, url), SITE)
    server.expire_before_write = 1
    await battery.update_battery_settings({"chargeFromGrid": True})
    assert server.logins == 2
    assert server.events == [
        ("GET", "xsrf-1"),
        ("PUT", "expired"),
        ("GET", "xsrf-2"),
        ("PUT", "xsrf-2", {"chargeFromGrid": True}, {"userId": str(USER)}),
    ]


async def test_write_gives_up_after_one_relogin(
    http: aiohttp.ClientSession, fake: tuple[FakeEnlighten, str]
) -> None:
    server, url = fake
    battery = BatteryConfigClient(_cloud(http, url), SITE)
    server.expire_before_write = 5
    with pytest.raises(EnlightenAuthError):
        await battery.update_battery_settings({"chargeFromGrid": True})
    assert server.logins == 2
    assert [e[0] for e in server.events] == ["GET", "PUT", "GET", "PUT"]


async def test_stale_xsrf_is_refused(
    http: aiohttp.ClientSession, fake: tuple[FakeEnlighten, str]
) -> None:
    """Guards the fake itself: a write with an old token must fail, or the tests prove nothing."""
    server, url = fake
    cloud = _cloud(http, url)
    battery = BatteryConfigClient(cloud, SITE)
    await battery.battery_settings()
    stale = cloud.cookie("BP-XSRF-Token")
    await battery.battery_settings()
    # 403 reads as an expired session (spec 4.2); inside a write that means log in, GET, retry.
    with pytest.raises(EnlightenAuthError):
        await cloud.request(
            "PUT",
            f"{PAGE}/batterySettings/{SITE}",
            json={},
            headers={
                "Username": str(USER),
                "Requestid": "0" * 36,
                "Origin": "https://battery-profile-ui.enphaseenergy.com",
                "X-XSRF-Token": stale or "",
            },
            relogin=False,
        )
    assert server.events[-1] == ("PUT", "bad xsrf", "xsrf-1")
