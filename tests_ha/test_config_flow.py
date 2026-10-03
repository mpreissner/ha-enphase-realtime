"""Config, reauth and options flows (spec 4.1, 4.3)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_realtime.const import (
    CONF_ALLOW_DRY_CONTACTS,
    CONF_ALLOW_GRID_RELAY,
    CONF_CLOUD_INTERVAL,
    CONF_COUNTRY,
    CONF_ENABLE_STREAM,
    CONF_FAST_INTERVAL,
    CONF_HAS_BATTERY,
    CONF_HAS_ENPOWER,
    CONF_LIVE_INTERVAL,
    CONF_PHASE_LAYOUT,
    CONF_SERIAL,
    CONF_SITE_ID,
    CONF_STREAM_INTERVAL,
    CONF_TIME_ZONE,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.enphase_realtime.enlighten_client.errors import (
    EnlightenAuthError,
    EnlightenConnectionError,
)
from custom_components.enphase_realtime.envoy_client.errors import EnvoyConnectionError

from .conftest import EMAIL, HOST, PASSWORD, SERIAL, SITE, FakeEnphase, entry_data

USER_INPUT = {CONF_HOST: HOST, CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}


@pytest.fixture(autouse=True)
def no_setup():
    """Flow tests stop at the entry; setting it up is test_init's business."""
    with patch("custom_components.enphase_realtime.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_happy_path(hass: HomeAssistant, fake: FakeEnphase) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["layout"] == "Split-phase"
    assert result["description_placeholders"]["serial"] == SERIAL

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "US", CONF_TIME_ZONE: "US/Eastern"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert result["result"].unique_id == SERIAL
    assert data[CONF_SERIAL] == SERIAL
    assert data[CONF_SITE_ID] == SITE
    assert data[CONF_TOKEN] == fake.token
    assert data[CONF_PHASE_LAYOUT] == "split"
    assert data[CONF_HAS_BATTERY] is True
    assert data[CONF_HAS_ENPOWER] is True
    assert result["options"] == {
        CONF_COUNTRY: "US",
        CONF_TIME_ZONE: "US/Eastern",
        CONF_ALLOW_GRID_RELAY: False,
        CONF_ALLOW_DRY_CONTACTS: False,
    }


async def test_control_options_offered_at_setup(hass: HomeAssistant, fake: FakeEnphase) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert CONF_ALLOW_GRID_RELAY in result["data_schema"].schema
    assert CONF_ALLOW_DRY_CONTACTS in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_COUNTRY: "US",
            CONF_TIME_ZONE: "US/Eastern",
            CONF_ALLOW_GRID_RELAY: True,
            CONF_ALLOW_DRY_CONTACTS: True,
        },
    )
    assert result["options"][CONF_ALLOW_GRID_RELAY] is True
    assert result["options"][CONF_ALLOW_DRY_CONTACTS] is True


async def test_setup_without_a_system_controller_has_no_control_options(
    hass: HomeAssistant, fake: FakeEnphase
) -> None:
    with patch("custom_components.enphase_realtime.config_flow._has_enpower", return_value=False):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        assert CONF_ALLOW_GRID_RELAY not in result["data_schema"].schema
        assert CONF_ALLOW_DRY_CONTACTS not in result["data_schema"].schema
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_COUNTRY: "US", CONF_TIME_ZONE: "US/Eastern"}
        )
    assert result["options"] == {CONF_COUNTRY: "US", CONF_TIME_ZONE: "US/Eastern"}


async def test_site_found_by_search_when_login_has_none(
    hass: HomeAssistant, fake: FakeEnphase
) -> None:
    fake.system_id = None
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["step_id"] == "confirm"


async def test_several_sites_ask_which(hass: HomeAssistant, fake: FakeEnphase) -> None:
    fake.system_id = None
    fake.site_ids = [111, SITE]
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["step_id"] == "site"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_ID: str(SITE)}
    )
    assert result["step_id"] == "confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "US", CONF_TIME_ZONE: "US/Eastern"}
    )
    assert result["data"][CONF_SITE_ID] == SITE


async def test_no_site(hass: HomeAssistant, fake: FakeEnphase) -> None:
    fake.system_id = None
    fake.site_ids = []
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "no_site"}


@pytest.mark.parametrize(
    ("script", "error"),
    [
        ({"info_error": EnvoyConnectionError("down")}, "cannot_connect"),
        ({"firmware": "D5.0.62"}, "firmware_unsupported"),
        ({"login_error": EnlightenAuthError("refused")}, "invalid_auth"),
        ({"login_error": EnlightenConnectionError("down")}, "cannot_connect"),
        (
            {"envoy_errors": {"/ivp/meters": EnvoyConnectionError("down")}},
            "cannot_connect",
        ),
        ({"battery_settings_error": EnlightenConnectionError("down")}, "cloud_unavailable"),
    ],
)
async def test_errors_then_recovery(
    hass: HomeAssistant, fake: FakeEnphase, script: dict, error: str
) -> None:
    for name, value in script.items():
        setattr(fake, name, value)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    fresh = FakeEnphase()
    for name in script:
        setattr(fake, name, getattr(fresh, name))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["step_id"] == "confirm"


async def test_already_configured_updates_host(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT | {CONF_HOST: "192.0.2.10"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_HOST] == "192.0.2.10"


async def test_reauth(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    fake.login_error = EnlightenAuthError("refused")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    fake.login_error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "new-password"
    assert config_entry.data[CONF_TOKEN] == fake.token


async def test_reauth_against_another_envoy(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, unique_id="000000000000")
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_envoy"


async def test_options(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_LIVE_INTERVAL: 2.0,
            CONF_FAST_INTERVAL: 10.0,
            CONF_STREAM_INTERVAL: 0.0,
            CONF_CLOUD_INTERVAL: 600.0,
            CONF_ENABLE_STREAM: False,
            CONF_COUNTRY: "AU",
            CONF_TIME_ZONE: "Australia/Sydney",
            CONF_ALLOW_GRID_RELAY: True,
            CONF_ALLOW_DRY_CONTACTS: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        CONF_LIVE_INTERVAL: 2,
        CONF_FAST_INTERVAL: 10,
        CONF_STREAM_INTERVAL: 0,
        CONF_CLOUD_INTERVAL: 600,
        CONF_ENABLE_STREAM: False,
        CONF_COUNTRY: "AU",
        CONF_TIME_ZONE: "Australia/Sydney",
        CONF_ALLOW_GRID_RELAY: True,
        CONF_ALLOW_DRY_CONTACTS: False,
    }


async def test_options_without_a_system_controller_have_no_relay_option(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=SERIAL, data=entry_data(**{CONF_HAS_ENPOWER: False})
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert CONF_ALLOW_GRID_RELAY not in result["data_schema"].schema
    assert CONF_ALLOW_DRY_CONTACTS not in result["data_schema"].schema
