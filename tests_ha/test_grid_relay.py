"""The grid relay switch: pre-check, local write, confirm on the live poll (spec 6.3)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.enphase_realtime.const import (
    CONF_ALLOW_GRID_RELAY,
    CONF_HAS_ENPOWER,
    DOMAIN,
)
from custom_components.enphase_realtime.enlighten_client.errors import (
    EnlightenAuthError,
    EnlightenConnectionError,
)
from custom_components.enphase_realtime.envoy_client.errors import EnvoyConnectionError

from .conftest import SERIAL, FakeEnphase, entry_data

RELAY = "/ivp/ensemble/relay"


def _entry(**data: object) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        data=entry_data(**data),
        options={CONF_ALLOW_GRID_RELAY: True},
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> str | None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{SERIAL}_grid_enabled")


async def _switch(hass: HomeAssistant, entity_id: str, service: str) -> None:
    await hass.services.async_call("switch", service, {ATTR_ENTITY_ID: entity_id}, blocking=True)


async def _live_tick(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.live.async_refresh()
    await hass.async_block_till_done()


def _relay(fake: FakeEnphase, admin: str, oper: str) -> None:
    fake.envoy_overrides[RELAY] = {"mains_admin_state": admin, "mains_oper_state": oper}


async def test_no_switch_unless_allowed(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    assert await _setup(hass, config_entry) is None


async def test_no_switch_without_a_system_controller(
    hass: HomeAssistant, fake: FakeEnphase
) -> None:
    assert await _setup(hass, _entry(**{CONF_HAS_ENPOWER: False})) is None


async def test_switch_on_the_system_controller(hass: HomeAssistant, fake: FakeEnphase) -> None:
    entity_id = await _setup(hass, _entry())
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_ON
    assert "confirmation" not in state.attributes

    entity = er.async_get(hass).async_get(entity_id)
    assert entity is not None and entity.device_id is not None
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device is not None
    assert device.model == "IQ System Controller"


async def test_go_off_grid_confirms_once_the_relay_opens(
    hass: HomeAssistant, fake: FakeEnphase
) -> None:
    entry = _entry()
    entity_id = await _setup(hass, entry)
    assert entity_id is not None

    await _switch(hass, entity_id, SERVICE_TURN_OFF)
    assert fake.grid_checks == 1
    assert fake.envoy_posts == [(RELAY, {"mains_admin_state": "open"})]
    state = hass.states.get(entity_id)
    assert state is not None
    assert (state.state, state.attributes["confirmation"]) == (STATE_OFF, "pending")

    # Told, but not yet moved: still pending.
    _relay(fake, "open", "closed")
    await _live_tick(hass, entry)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["confirmation"] == "pending"

    # The reference site never reports plain "open" off grid (FINDINGS 2026-10-01).
    _relay(fake, "open", "open synchronizing")
    await _live_tick(hass, entry)
    state = hass.states.get(entity_id)
    assert state is not None
    assert (state.state, state.attributes["confirmation"]) == (STATE_OFF, "confirmed")

    _relay(fake, "open", "open synchronized")
    await _live_tick(hass, entry)
    state = hass.states.get(entity_id)
    assert state is not None
    assert (state.state, state.attributes["confirmation"]) == (STATE_OFF, "confirmed")

    # And back on grid: told, but still open.
    await _switch(hass, entity_id, SERVICE_TURN_ON)
    assert fake.envoy_posts[-1] == (RELAY, {"mains_admin_state": "closed"})
    _relay(fake, "closed", "open synchronized")
    await _live_tick(hass, entry)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["confirmation"] == "pending"
    _relay(fake, "closed", "closed")
    await _live_tick(hass, entry)
    state = hass.states.get(entity_id)
    assert state is not None
    assert (state.state, state.attributes["confirmation"]) == (STATE_ON, "confirmed")


async def test_relay_that_never_moves_fails_after_90_s(
    hass: HomeAssistant,
    fake: FakeEnphase,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entity_id = await _setup(hass, _entry())
    assert entity_id is not None
    await _switch(hass, entity_id, SERVICE_TURN_OFF)

    freezer.tick(timedelta(seconds=91))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert (state.state, state.attributes["confirmation"]) == (STATE_ON, "failed")
    assert "after 90 s the Envoy still reports" in caplog.text


async def test_relay_state_changes_are_logged_once(
    hass: HomeAssistant, fake: FakeEnphase, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _entry()
    entity_id = await _setup(hass, entry)
    assert entity_id is not None
    caplog.set_level("DEBUG", logger="custom_components.enphase_realtime.switch")

    _relay(fake, "open", "closed")
    await _live_tick(hass, entry)
    await _live_tick(hass, entry)
    _relay(fake, "open", "open")
    await _live_tick(hass, entry)
    assert caplog.text.count("relay admin state open, oper state closed") == 1
    assert caplog.text.count("relay admin state open, oper state open") == 1


async def test_nothing_sent_when_already_in_that_state(
    hass: HomeAssistant, fake: FakeEnphase
) -> None:
    entity_id = await _setup(hass, _entry())
    assert entity_id is not None
    await _switch(hass, entity_id, SERVICE_TURN_ON)
    assert fake.grid_checks == 0
    assert fake.envoy_posts == []


async def test_pre_check_flag_refuses_the_write(hass: HomeAssistant, fake: FakeEnphase) -> None:
    entity_id = await _setup(hass, _entry())
    assert entity_id is not None
    fake.grid_check_overrides = {"activeDownload": True}

    with pytest.raises(HomeAssistantError, match="activeDownload"):
        await _switch(hass, entity_id, SERVICE_TURN_OFF)
    assert fake.envoy_posts == []
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_ON
    assert "confirmation" not in state.attributes


async def test_unreachable_cloud_refuses_the_write(hass: HomeAssistant, fake: FakeEnphase) -> None:
    entity_id = await _setup(hass, _entry())
    assert entity_id is not None
    fake.grid_check_error = EnlightenConnectionError("timeout")

    with pytest.raises(HomeAssistantError, match="relay wasn't changed"):
        await _switch(hass, entity_id, SERVICE_TURN_OFF)
    assert fake.envoy_posts == []


async def test_rejected_login_starts_reauth(hass: HomeAssistant, fake: FakeEnphase) -> None:
    entry = _entry()
    entity_id = await _setup(hass, entry)
    assert entity_id is not None
    fake.grid_check_error = EnlightenAuthError("expired")

    with pytest.raises(HomeAssistantError):
        await _switch(hass, entity_id, SERVICE_TURN_OFF)
    assert fake.envoy_posts == []
    assert any(entry.async_get_active_flows(hass, {"reauth"}))


async def test_envoy_refusing_the_write(hass: HomeAssistant, fake: FakeEnphase) -> None:
    entity_id = await _setup(hass, _entry())
    assert entity_id is not None
    fake.envoy_errors[RELAY] = EnvoyConnectionError(f"{RELAY}: HTTP 500")

    with pytest.raises(HomeAssistantError, match="HTTP 500"):
        await _switch(hass, entity_id, SERVICE_TURN_OFF)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_ON
    assert "confirmation" not in state.attributes
