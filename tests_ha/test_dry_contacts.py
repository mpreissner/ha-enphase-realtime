"""Dry-contact controls: local write, confirm on a quick re-read (docs/specs/dry-contacts.md)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.number import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.components.select import ATTR_OPTION, SERVICE_SELECT_OPTION
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.enphase_realtime.const import (
    CONF_ALLOW_DRY_CONTACTS,
    CONF_HAS_ENPOWER,
    DOMAIN,
)
from custom_components.enphase_realtime.envoy_client.errors import EnvoyConnectionError
from tests.helpers import load_json

from .conftest import SERIAL, FakeEnphase, entry_data

STATES = "/ivp/ensemble/dry_contacts"
SETTINGS = "/ivp/ss/dry_contact_settings"
# The fixture: NC1 and NC2 closed, NO1 and NO2 open; all in manual mode, levels 30 and 40.


def _entry(**data: object) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        data=entry_data(**data),
        options={CONF_ALLOW_DRY_CONTACTS: True},
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _id(hass: HomeAssistant, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{SERIAL}_{key}")


def _entity(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = _id(hass, platform, key)
    assert entity_id is not None
    return entity_id


def _state(hass: HomeAssistant, entity_id: str) -> tuple[str, str | None]:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state, state.attributes.get("confirmation")


def _raw(contact_id: str) -> dict[str, Any]:
    contacts = load_json("ivp_ss_dry_contact_settings.json")["dry_contacts"]
    return next(c for c in contacts if c["id"] == contact_id)


def _report_settings(fake: FakeEnphase, contact_id: str, **changes: Any) -> None:
    contacts = load_json("ivp_ss_dry_contact_settings.json")["dry_contacts"]
    fake.envoy_overrides[SETTINGS] = {
        "dry_contacts": [c | changes if c["id"] == contact_id else c for c in contacts]
    }


def _report_state(fake: FakeEnphase, contact_id: str, status: str) -> None:
    contacts = load_json("ivp_ensemble_dry_contacts.json")["dry_contacts"]
    fake.envoy_overrides[STATES] = {
        "dry_contacts": [c | {"status": status} if c["id"] == contact_id else c for c in contacts]
    }


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float = 3) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _select(hass: HomeAssistant, key: str, option: str) -> None:
    await hass.services.async_call(
        "select",
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: _entity(hass, "select", key), ATTR_OPTION: option},
        blocking=True,
    )


async def _number(hass: HomeAssistant, key: str, value: float) -> None:
    await hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: _entity(hass, "number", key), ATTR_VALUE: value},
        blocking=True,
    )


async def _switch(hass: HomeAssistant, key: str, service: str) -> None:
    await hass.services.async_call(
        "switch", service, {ATTR_ENTITY_ID: _entity(hass, "switch", key)}, blocking=True
    )


CONTROLS = (
    ("switch", "dry_contact_NC1"),
    ("select", "dry_contact_NC1_mode"),
    ("select", "dry_contact_NC1_grid_action"),
    ("select", "dry_contact_NC1_micro_grid_action"),
    ("select", "dry_contact_NC1_gen_action"),
    ("number", "dry_contact_NC1_soc_low"),
    ("number", "dry_contact_NC1_soc_high"),
)


async def test_no_controls_unless_allowed(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    for platform, key in CONTROLS:
        assert _id(hass, platform, key) is None
    # The read-only entities are there regardless.
    assert _id(hass, "binary_sensor", "dry_contact_NC1") is not None


async def test_no_controls_without_a_system_controller(
    hass: HomeAssistant, fake: FakeEnphase
) -> None:
    await _setup(hass, _entry(**{CONF_HAS_ENPOWER: False}))
    for platform, key in CONTROLS:
        assert _id(hass, platform, key) is None


async def test_controls_and_their_states(hass: HomeAssistant, fake: FakeEnphase) -> None:
    await _setup(hass, _entry())
    for contact in ("NC1", "NC2", "NO1", "NO2"):
        for platform, key in CONTROLS:
            assert _id(hass, platform, key.replace("NC1", contact)) is not None
    assert _state(hass, _entity(hass, "switch", "dry_contact_NC1")) == (STATE_ON, None)
    assert _state(hass, _entity(hass, "switch", "dry_contact_NO1"))[0] == "off"
    assert _state(hass, _entity(hass, "select", "dry_contact_NC1_mode"))[0] == "standard"
    assert _state(hass, _entity(hass, "select", "dry_contact_NC1_grid_action"))[0] == "none"
    assert _state(hass, _entity(hass, "number", "dry_contact_NC1_soc_low"))[0] == "30"
    assert _state(hass, _entity(hass, "number", "dry_contact_NC1_soc_high"))[0] == "40"

    entity = er.async_get(hass).async_get(_entity(hass, "switch", "dry_contact_NC1"))
    assert entity is not None and entity.device_id is not None
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device is not None
    assert (device.model, device.name) == ("Dry contact relay", "NC1")
    controller = dr.async_get(hass).async_get(device.via_device_id or "")
    assert controller is not None and controller.model == "IQ System Controller"
    # Named as in the core integration: the switch takes the device's name.
    assert entity.entity_id == "switch.nc1"
    assert _entity(hass, "select", "dry_contact_NC1_mode") == "select.nc1_mode"
    assert _entity(hass, "number", "dry_contact_NC1_soc_low") == "number.nc1_cutoff_battery_level"
    assert _entity(hass, "binary_sensor", "dry_contact_NC1") == "binary_sensor.nc1"


async def test_switch_confirms_on_a_quick_re_read(
    hass: HomeAssistant, fake: FakeEnphase, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, _entry())
    entity_id = _entity(hass, "switch", "dry_contact_NC1")

    await _switch(hass, "dry_contact_NC1", SERVICE_TURN_OFF)
    assert fake.envoy_posts == [(STATES, {"dry_contacts": {"id": "NC1", "status": "open"}})]
    assert _state(hass, entity_id) == ("off", "pending")

    # Not moved yet: still pending on the first re-read.
    await _poll(hass, freezer)
    assert _state(hass, entity_id) == ("off", "pending")

    _report_state(fake, "NC1", "open")
    await _poll(hass, freezer)
    assert _state(hass, entity_id) == ("off", "confirmed")


async def test_nothing_sent_when_already_in_that_state(
    hass: HomeAssistant, fake: FakeEnphase
) -> None:
    await _setup(hass, _entry())
    await _switch(hass, "dry_contact_NC1", SERVICE_TURN_ON)
    await _select(hass, "dry_contact_NC1_mode", "standard")
    assert fake.envoy_posts == []


async def test_settings_write_sends_the_full_object(
    hass: HomeAssistant, fake: FakeEnphase, freezer: FrozenDateTimeFactory
) -> None:
    entry = _entry()
    await _setup(hass, entry)
    mode = _entity(hass, "select", "dry_contact_NO1_mode")

    await _select(hass, "dry_contact_NO1_mode", "battery")
    assert fake.envoy_posts == [(SETTINGS, {"dry_contacts": _raw("NO1") | {"mode": "soc"}})]
    assert _state(hass, mode) == ("battery", "pending")

    # A second write before the Envoy reports the first keeps it.
    await _number(hass, "dry_contact_NO1_soc_low", 20)
    assert fake.envoy_posts[-1] == (
        SETTINGS,
        {"dry_contacts": _raw("NO1") | {"mode": "soc", "soc_low": 20.0}},
    )

    _report_settings(fake, "NO1", mode="soc", soc_low=20.0)
    await _poll(hass, freezer)
    assert _state(hass, mode) == ("battery", "confirmed")
    assert _state(hass, _entity(hass, "number", "dry_contact_NO1_soc_low")) == ("20", "confirmed")

    # Reported, so no longer laid over the Envoy's object: its own value goes out.
    _report_settings(fake, "NO1", mode="manual", soc_low=20.0)
    await entry.runtime_data.slow.async_refresh()
    await _select(hass, "dry_contact_NO1_grid_action", "not_powered")
    assert fake.envoy_posts[-1] == (
        SETTINGS,
        {"dry_contacts": _raw("NO1") | {"soc_low": 20.0, "grid_action": "shed"}},
    )


async def test_unconfirmed_write_fails_after_30_s(
    hass: HomeAssistant,
    fake: FakeEnphase,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _setup(hass, _entry())
    entity_id = _entity(hass, "select", "dry_contact_NC1_gen_action")
    await _select(hass, "dry_contact_NC1_gen_action", "powered")
    for _ in range(11):
        await _poll(hass, freezer)
    assert _state(hass, entity_id) == ("none", "failed")
    assert "after 30 s the Envoy still reports" in caplog.text

    # The unconfirmed field is forgotten, so it isn't sent again.
    await _select(hass, "dry_contact_NC1_grid_action", "powered")
    assert fake.envoy_posts[-1] == (
        SETTINGS,
        {"dry_contacts": _raw("NC1") | {"grid_action": "apply"}},
    )


async def test_cutoff_must_stay_below_restore(hass: HomeAssistant, fake: FakeEnphase) -> None:
    await _setup(hass, _entry())
    with pytest.raises(ServiceValidationError):
        await _number(hass, "dry_contact_NC1_soc_low", 40)
    with pytest.raises(ServiceValidationError):
        await _number(hass, "dry_contact_NC1_soc_high", 25)
    assert fake.envoy_posts == []
    await _number(hass, "dry_contact_NC1_soc_high", 80)
    await _number(hass, "dry_contact_NC1_soc_low", 60)
    assert fake.envoy_posts[-1] == (
        SETTINGS,
        {"dry_contacts": _raw("NC1") | {"soc_low": 60.0, "soc_high": 80.0}},
    )


async def test_envoy_refusing_the_write(hass: HomeAssistant, fake: FakeEnphase) -> None:
    await _setup(hass, _entry())
    fake.envoy_errors[STATES] = EnvoyConnectionError(f"{STATES}: HTTP 500")
    entity_id = _entity(hass, "switch", "dry_contact_NC1")
    with pytest.raises(HomeAssistantError, match="HTTP 500"):
        await _switch(hass, "dry_contact_NC1", SERVICE_TURN_OFF)
    assert _state(hass, entity_id) == (STATE_ON, None)
