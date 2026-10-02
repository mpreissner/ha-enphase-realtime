"""Home Assistant-level tests: the config flow, setup and diagnostics against faked clients.

The clients' transport is replaced at its lowest typed layer (`get_json`, `info`, `login`,
`request`, the owner-token fetch and the stream), so the real parsers run on the captured
fixtures. Everything is imported through `custom_components.enphase_realtime`, the same module
objects Home Assistant loads.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_realtime.const import (
    CONF_COUNTRY,
    CONF_FIRMWARE,
    CONF_HAS_BATTERY,
    CONF_HAS_ENPOWER,
    CONF_PHASE_LAYOUT,
    CONF_SERIAL,
    CONF_SITE_ID,
    CONF_TIME_ZONE,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.enphase_realtime.enlighten_client.errors import (
    EnlightenError,
)
from custom_components.enphase_realtime.enlighten_client.session import EnlightenSession
from custom_components.enphase_realtime.envoy_client.auth import OwnerToken
from custom_components.enphase_realtime.envoy_client.errors import (
    EnvoyConnectionError,
    EnvoyError,
    EnvoyStreamUnavailable,
)
from custom_components.enphase_realtime.envoy_client.local import EnvoyClient
from custom_components.enphase_realtime.envoy_client.models import EnvoyInfo, StreamFrame
from custom_components.enphase_realtime.envoy_client.stream import StreamDecoder
from tests.helpers import load_json, load_text, make_jwt

SERIAL = "900000000001"
SITE = 1234567
USER_ID = 7654321
HOST = "envoy.local"
EMAIL = "owner@example.com"
PASSWORD = "hunter2"

# Envoy paths and the fixture each one serves.
ENVOY_FIXTURES = {
    "/ivp/livedata/status": "ivp_livedata_status.json",
    "/ivp/ensemble/secctrl": "ivp_ensemble_secctrl.json",
    "/ivp/sc/sched": "ivp_sc_sched.json",
    "/ivp/ensemble/relay": "ivp_ensemble_relay.json",
    "/ivp/meters": "ivp_meters.json",
    "/ivp/meters/readings": "ivp_meters_readings.json",
    "/ivp/meters/reports": "ivp_meters_reports.json",
    "/ivp/ensemble/inventory": "ivp_ensemble_inventory.json",
    "/ivp/ss/dry_contact_settings": "ivp_ss_dry_contact_settings.json",
    "/ivp/ensemble/dry_contacts": "ivp_ensemble_dry_contacts.json",
    "/ivp/ss/pel_settings": "ivp_ss_pel_settings.json",
    "/ivp/ss/pcs_settings": "ivp_ss_pcs_settings.json",
    "/api/v1/production/inverters": "api_v1_production_inverters.json",
    "/production.json?details=1": "production_details.json",
    "/ivp/ensemble/power": "ivp_ensemble_power.json",
    "/ivp/pdm/device_data": "ivp_pdm_device_data.json",
}


def owner_token() -> str:
    return make_jwt({"exp": int(time.time()) + 365 * 86400, "enphaseUser": "owner"})


@dataclass
class FakeEnphase:
    """What the fake Envoy and cloud return. Tests flip these to script failures."""

    info_error: EnvoyError | None = None
    firmware: str | None = None
    login_error: EnlightenError | None = None
    token_error: Exception | None = None
    system_id: int | None = SITE
    site_ids: list[int] = field(default_factory=lambda: [SITE])
    envoy_errors: dict[str, EnvoyError] = field(default_factory=dict)
    battery_settings_error: EnlightenError | None = None
    # Fields merged over a fixture, so a test can move a local value (e.g. after a write).
    envoy_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Endpoints served from another fixture layout, such as "collar" (tests/fixtures/README.md).
    envoy_layouts: dict[str, str] = field(default_factory=dict)
    site_settings_overrides: dict[str, Any] = field(default_factory=dict)
    battery_settings_overrides: dict[str, Any] = field(default_factory=dict)
    # Local POSTs as (path, body).
    envoy_posts: list[tuple[str, Any]] = field(default_factory=list)
    # Cloud writes as (method, path, body), and an error to raise instead of accepting them.
    writes: list[tuple[str, str, Any]] = field(default_factory=list)
    write_error: EnlightenError | None = None
    # grid_control_check.json: flags merged over the fixture, or an error; and a call count.
    grid_check_overrides: dict[str, bool] = field(default_factory=dict)
    grid_check_error: EnlightenError | None = None
    grid_checks: int = 0
    stream_available: bool = True
    token: str = field(default_factory=owner_token)
    stream_frames: list[StreamFrame] = field(default_factory=list)

    def __post_init__(self) -> None:
        decoder = StreamDecoder()
        self.stream_frames = decoder.feed(load_text("stream_meter.txt").encode())

    # --- Envoy ------------------------------------------------------------------------------

    async def info(self, client: EnvoyClient) -> EnvoyInfo:
        if self.info_error is not None:
            raise self.info_error
        xml = load_text("info.xml")
        if self.firmware is not None:
            xml = xml.replace("D8.3.6086", self.firmware)
        return EnvoyInfo.from_xml(xml)

    async def get_json(self, client: EnvoyClient, path: str, timeout: Any = None) -> Any:
        if path in self.envoy_errors:
            raise self.envoy_errors[path]
        if path not in ENVOY_FIXTURES:
            raise EnvoyConnectionError(f"{path}: HTTP 404")
        payload = load_json(ENVOY_FIXTURES[path], self.envoy_layouts.get(path, "reference"))
        if path in self.envoy_overrides:
            payload.update(self.envoy_overrides[path])
        client.last_payloads[path] = payload
        return payload

    async def post_json(
        self, client: EnvoyClient, path: str, body: Any, timeout: Any = None
    ) -> Any:
        if path in self.envoy_errors:
            raise self.envoy_errors[path]
        self.envoy_posts.append((path, body))
        return {}

    async def stream(self, client: EnvoyClient) -> AsyncIterator[StreamFrame]:
        if not self.stream_available:
            raise EnvoyStreamUnavailable("/stream/meter: HTTP 401")
        for frame in self.stream_frames:
            yield frame
        # A live stream stays open; the task is cancelled when the entry unloads.
        await asyncio.Event().wait()

    # --- Enlighten ----------------------------------------------------------------------------

    async def login(self, cloud: EnlightenSession) -> None:
        if self.login_error is not None:
            raise self.login_error
        cloud.session_id = "fake-session"
        cloud.user_id = USER_ID
        cloud.system_id = self.system_id

    async def request(
        self, cloud: EnlightenSession, method: str, path: str, json: Any = None, **_: Any
    ) -> Any:
        if path.endswith("/search_sites.json"):
            return {"sites": [{"id": i, "title": f"Site {i}"} for i in self.site_ids]}
        if path.endswith("/grid_control_check.json"):
            self.grid_checks += 1
            if self.grid_check_error is not None:
                raise self.grid_check_error
            return load_json("cloud_grid_control_check.json") | self.grid_check_overrides
        if "/siteSettings/" in path:
            payload = load_json("cloud_site_settings.json")
            payload["data"].update(self.site_settings_overrides)
            return payload
        if method != "GET" and "/batterySettings/" in path:
            if self.write_error is not None:
                raise self.write_error
            self.writes.append((method, path, json))
            if "/acceptDisclaimer/" in path:
                return load_json("cloud_accept_disclaimer.json")
            return load_json("cloud_put_response.json")
        if "/batterySettings/" in path:
            if self.battery_settings_error is not None:
                raise self.battery_settings_error
            payload = load_json("cloud_battery_settings.json")
            payload["data"].update(self.battery_settings_overrides)
            return payload
        raise EnlightenError(f"unexpected {method} {path}")

    async def fetch_owner_token(self, http: Any, **_: Any) -> OwnerToken:
        if self.token_error is not None:
            raise self.token_error
        return OwnerToken.from_jwt(self.token)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    return


@pytest.fixture
def fake() -> Iterator[FakeEnphase]:
    fake = FakeEnphase()
    with (
        patch.object(EnvoyClient, "get_json", _bind(fake.get_json)),
        patch.object(EnvoyClient, "post_json", _bind(fake.post_json)),
        patch.object(EnvoyClient, "info", _bind(fake.info)),
        patch.object(EnvoyClient, "stream_frames", _bind(fake.stream)),
        patch.object(EnlightenSession, "login", _bind(fake.login)),
        patch.object(EnlightenSession, "request", _bind(fake.request)),
        patch.object(EnlightenSession, "cookie", lambda self, name: "fake-xsrf"),
        patch(
            "custom_components.enphase_realtime.credentials.fetch_owner_token",
            fake.fetch_owner_token,
        ),
    ):
        yield fake


def _bind(method: Any) -> Any:
    """Turn a bound fake method into a plain function, so it binds to the patched instance."""

    def call(instance: Any, *args: Any, **kwargs: Any) -> Any:
        return method(instance, *args, **kwargs)

    return call


def entry_data(**overrides: Any) -> dict[str, Any]:
    data = {
        "host": HOST,
        "email": EMAIL,
        "password": PASSWORD,
        CONF_SERIAL: SERIAL,
        CONF_FIRMWARE: "D8.3.6086",
        CONF_SITE_ID: SITE,
        CONF_TOKEN: owner_token(),
        CONF_PHASE_LAYOUT: "split",
        CONF_HAS_BATTERY: True,
        CONF_HAS_ENPOWER: True,
    }
    return data | overrides


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        title=f"Envoy {SERIAL}",
        data=entry_data(),
        options={CONF_COUNTRY: "US", CONF_TIME_ZONE: "US/Eastern"},
    )
