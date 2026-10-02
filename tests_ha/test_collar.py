"""A site with an IQ Meter Collar and an IQ Combiner 6C instead of a System Controller, from
pyenphase's capture (tests/fixtures/README.md)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_realtime.const import DOMAIN
from custom_components.enphase_realtime.diagnostics import async_get_config_entry_diagnostics

from .conftest import SERIAL, FakeEnphase, entry_data

COLLAR = "910000000003"
COMBINER = "910000000004"


@pytest.fixture
async def entry(hass: HomeAssistant, fake: FakeEnphase) -> MockConfigEntry:
    fake.envoy_layouts["/ivp/ensemble/inventory"] = "collar"
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=SERIAL, data=entry_data(has_battery=True, has_enpower=False)
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _state(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        # The same entity IDs as the core integration (docs/MIGRATION.md).
        (f"sensor.collar_{COLLAR}_temperature", "42"),
        (f"sensor.collar_{COLLAR}_last_reported", "2025-07-19T15:42:39+00:00"),
        (f"sensor.collar_{COLLAR}_admin_state", "on_grid"),
        (f"sensor.collar_{COLLAR}_grid_status", "on_grid"),
        (f"sensor.collar_{COLLAR}_mid_state", "close"),
        (f"sensor.collar_{COLLAR}_collar_state", "Installed"),
        (f"sensor.collar_{COLLAR}_control_error", "0"),
        (f"binary_sensor.collar_{COLLAR}_communicating", "on"),
        (f"sensor.c6_combiner_{COMBINER}_last_reported", "2025-07-19T17:17:31+00:00"),
        (f"sensor.c6_combiner_{COMBINER}_admin_state", "ENCMN_C6_CC_READY"),
        (f"binary_sensor.c6_combiner_{COMBINER}_communicating", "on"),
    ],
)
async def test_collar_and_combiner_entities(
    hass: HomeAssistant, entry: MockConfigEntry, entity_id: str, expected: str
) -> None:
    assert _state(hass, entity_id) == expected


async def test_devices(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    devices = {
        d.serial_number: d
        for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    }
    assert devices[COLLAR].model == "IQ Meter Collar"
    assert devices[COLLAR].name == f"Collar {COLLAR}"
    assert devices[COMBINER].model == "C6 Combiner Controller"
    assert devices[COMBINER].name == f"C6 Combiner {COMBINER}"
    envoy = devices[SERIAL]
    assert devices[COLLAR].via_device_id == envoy.id
    assert devices[COMBINER].via_device_id == envoy.id
    # Both batteries, and no System Controller.
    models = [d.model for d in devices.values()]
    assert models.count("IQ Battery") == 2
    assert "IQ System Controller" not in models


async def test_status_sensors_are_primary(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Grid and MID state are what a collar owner watches; temperature is primary as in core."""
    registry = er.async_get(hass)
    for key, category in [
        ("admin_state", None),
        ("grid_status", None),
        ("mid_state", None),
        ("temperature", None),
        ("collar_state", EntityCategory.DIAGNOSTIC),
        ("control_error", EntityCategory.DIAGNOSTIC),
    ]:
        entity = registry.async_get(f"sensor.collar_{COLLAR}_{key}")
        assert entity is not None, key
        assert entity.entity_category == category, key


async def test_off_grid_shows_in_the_admin_state(
    hass: HomeAssistant, fake: FakeEnphase, entry: MockConfigEntry
) -> None:
    """Unknown admin states pass through; the two known ones are mapped as in core."""
    rt = entry.runtime_data
    inventory = rt.slow.data.inventory
    [collar] = inventory.collars
    off = replace(collar, status="ENCMN_MDE_OFF_GRID", mid_state="open")
    rt.slow.async_set_updated_data(
        replace(rt.slow.data, inventory=replace(inventory, collars=[off]))
    )
    await hass.async_block_till_done()
    assert _state(hass, f"sensor.collar_{COLLAR}_admin_state") == "off_grid"
    assert _state(hass, f"sensor.collar_{COLLAR}_mid_state") == "open"


async def test_no_grid_relay_or_dry_contacts(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Those are built around the System Controller; a collar site doesn't get them yet."""
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, f"{SERIAL}_grid_status") is None
    ids = {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert not any(i.startswith(("switch.nc", "switch.no")) for i in ids)


async def test_diagnostics_redact_collar_serials(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    dump = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    assert COLLAR not in dump
    assert COMBINER not in dump
