"""Setup, entity creation and unload against the reference fixtures (spec 3, 5)."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_realtime.const import CONF_ENABLE_STREAM, CONF_TOKEN, DOMAIN
from custom_components.enphase_realtime.enlighten_client.errors import EnlightenConnectionError
from custom_components.enphase_realtime.envoy_client.errors import (
    EnvoyAuthError,
    EnvoyConnectionError,
)

from .conftest import SERIAL, FakeEnphase, entry_data


def _entity_id(hass: HomeAssistant, platform: str, key: str, prefix: str = SERIAL) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{prefix}_{key}")


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_creates_entities(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    rt = config_entry.runtime_data
    assert rt.stream is not None
    assert rt.cloud is not None and rt.cloud.last_update_success

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    assert len(entities) > 30
    # Unique IDs are per platform: a sensor and a number may share a key.
    assert len({(e.domain, e.unique_id) for e in entities}) == len(entities)

    for platform, key in [
        ("sensor", "current_power_production"),
        ("sensor", "grid_power"),
        ("sensor", "current_battery_discharge"),
        ("sensor", "battery_soc"),
        ("sensor", "storage_mode"),
        ("binary_sensor", "grid_status"),
        ("binary_sensor", "pending_cloud_change"),
    ]:
        entity_id = _entity_id(hass, platform, key)
        assert entity_id, key
        state = hass.states.get(entity_id)
        assert state is not None, key
        assert state.state != STATE_UNAVAILABLE, key

    # Batteries and the System Controller get devices of their own under the Envoy.
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)
    models = {d.model for d in devices}
    assert "IQ Battery" in models
    assert "IQ System Controller" in models

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_entity_ids_match_the_core_integration(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    """Moving from the core integration keeps entity IDs, and with them history (MIGRATION.md)."""
    await _setup(hass, config_entry)
    slow = config_entry.runtime_data.slow.data
    assert slow.inventory is not None
    battery = slow.inventory.batteries[0].serial
    sc = slow.inventory.system_controllers[0].serial
    inverter = slow.inverters[0].serial
    envoy = f"envoy_{SERIAL}"
    entries = er.async_entries_for_config_entry(er.async_get(hass), config_entry.entry_id)
    ids = {e.entity_id for e in entries}
    expected = {
        f"sensor.{envoy}_current_power_production",
        f"sensor.{envoy}_current_power_consumption",
        f"sensor.{envoy}_current_net_power_consumption",
        f"sensor.{envoy}_current_power_production_l1",
        f"sensor.{envoy}_current_battery_discharge",
        f"sensor.{envoy}_lifetime_energy_production",
        f"sensor.{envoy}_lifetime_energy_consumption",
        f"sensor.{envoy}_lifetime_net_energy_consumption",
        f"sensor.{envoy}_lifetime_net_energy_production",
        f"sensor.{envoy}_lifetime_battery_energy_charged",
        f"sensor.{envoy}_lifetime_battery_energy_discharged",
        f"sensor.{envoy}_battery",
        f"sensor.{envoy}_available_battery_energy",
        f"sensor.{envoy}_battery_capacity",
        f"sensor.{envoy}_reserve_battery_energy",
        f"sensor.{envoy}_battery_scheduler_mode",
        f"sensor.{envoy}_reserve_battery_level",
        f"sensor.{envoy}_voltage_net_consumption_ct_l1",
        f"sensor.{envoy}_frequency_net_consumption_ct",
        f"sensor.{envoy}_net_consumption_ct_current_l1",
        f"sensor.{envoy}_power_factor_net_consumption_ct_l1",
        f"switch.{envoy}_charge_from_grid",
        f"sensor.encharge_{battery}_battery",
        f"sensor.encharge_{battery}_temperature",
        f"sensor.encharge_{battery}_last_reported",
        f"binary_sensor.encharge_{battery}_communicating",
        f"binary_sensor.encharge_{battery}_dc_switch",
        f"sensor.enpower_{sc}_temperature",
        f"binary_sensor.enpower_{sc}_grid_status",
        f"number.enpower_{sc}_reserve_battery_level",
        f"sensor.inverter_{inverter}",
        f"sensor.inverter_{inverter}_last_reported",
    }
    assert expected - ids == set()


async def test_child_devices_hang_off_the_envoy(
    hass: HomeAssistant,
    fake: FakeEnphase,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _setup(hass, config_entry)
    registry = dr.async_get(hass)
    envoy = registry.async_get(config_entry.runtime_data.envoy_device_id)
    assert envoy is not None
    assert (DOMAIN, SERIAL) in envoy.identifiers
    children = [
        d
        for d in dr.async_entries_for_config_entry(registry, config_entry.entry_id)
        if d.id != envoy.id
    ]
    assert children
    assert all(d.via_device_id == envoy.id for d in children)
    assert "via_device" not in caplog.text  # the form deprecated in HA 2026.9


async def test_without_stream_falls_back_to_livedata(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    fake.stream_available = False
    await _setup(hass, config_entry)
    assert config_entry.runtime_data.stream is None
    assert _entity_id(hass, "sensor", "current_power_production") is None
    # The per-phase livedata fallbacks exist (disabled by default).
    registry = er.async_get(hass)
    keys = {
        e.unique_id.removeprefix(f"{SERIAL}_")
        for e in er.async_entries_for_config_entry(registry, config_entry.entry_id)
    }
    assert any(k.endswith("ph_a") for k in keys)


async def test_stream_disabled_in_options(hass: HomeAssistant, fake: FakeEnphase) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=SERIAL, data=entry_data(), options={CONF_ENABLE_STREAM: False}
    )
    await _setup(hass, entry)
    assert entry.runtime_data.stream is None


async def test_no_battery_skips_battery_endpoints(hass: HomeAssistant, fake: FakeEnphase) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        data=entry_data(has_battery=False, has_enpower=False),
    )
    await _setup(hass, entry)
    rt = entry.runtime_data
    assert rt.cloud is None
    assert rt.fast is None
    assert rt.slow.data.inventory is None
    assert _entity_id(hass, "sensor", "battery_soc") is None
    assert _entity_id(hass, "sensor", "grid_power") is not None


async def test_cloud_outage_does_not_block_setup(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    fake.battery_settings_error = EnlightenConnectionError("down")
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert not config_entry.runtime_data.cloud.last_update_success
    state = hass.states.get(_entity_id(hass, "sensor", "storage_mode"))
    assert state.state == STATE_UNAVAILABLE


async def test_envoy_down_retries_setup(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    fake.envoy_errors["/ivp/livedata/status"] = EnvoyConnectionError("down")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_rejected_token_starts_reauth(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    fake.envoy_errors["/ivp/livedata/status"] = EnvoyAuthError("refused")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_missing_token_is_fetched_and_saved(hass: HomeAssistant, fake: FakeEnphase) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, data=entry_data(token=None))
    await _setup(hass, entry)
    assert entry.data[CONF_TOKEN] == fake.token


async def test_options_change_reloads(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    first = config_entry.runtime_data
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_ENABLE_STREAM: False}
    )
    await hass.async_block_till_done()
    assert config_entry.runtime_data is not first
    assert config_entry.runtime_data.stream is None


async def test_token_save_does_not_reload(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    first = config_entry.runtime_data
    await first.tokens.refresh()
    await hass.async_block_till_done()
    assert config_entry.runtime_data is first
