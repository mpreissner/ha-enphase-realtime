"""Setup, entity creation and unload against the reference fixtures (spec 3, 5)."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, EntityCategory
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
        ("select", "storage_mode"),
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


# Every entity the core integration creates for the reference site, by device. Taken from a
# registry listing of core's entities on a split-phase site with batteries, a System Controller
# and all three CTs. The dry contacts are covered in test_dry_contacts.py and Grid enabled, which
# needs the grid relay option, in test_grid_relay.py.
_PHASED_ENVOY_SENSORS = [
    "balanced_net_power_consumption",
    "current_battery_discharge",
    "current_net_power_consumption",
    "current_power_consumption",
    "current_power_production",
    "energy_consumption_last_seven_days",
    "energy_consumption_today",
    "energy_production_last_seven_days",
    "energy_production_today",
    "frequency_net_consumption_ct",
    "frequency_production_ct",
    "frequency_storage_ct",
    "lifetime_balanced_net_energy_consumption",
    "lifetime_battery_energy_charged",
    "lifetime_battery_energy_discharged",
    "lifetime_energy_consumption",
    "lifetime_energy_production",
    "lifetime_net_energy_consumption",
    "lifetime_net_energy_production",
    "meter_status_flags_active_net_consumption_ct",
    "meter_status_flags_active_production_ct",
    "meter_status_flags_active_storage_ct",
    "metering_status_net_consumption_ct",
    "metering_status_production_ct",
    "metering_status_storage_ct",
    "net_consumption_ct_current",
    "power_factor_net_consumption_ct",
    "power_factor_production_ct",
    "power_factor_storage_ct",
    "production_ct_current",
    "production_ct_energy_delivered",
    "production_ct_energy_received",
    "production_ct_power",
    "storage_ct_current",
    "voltage_net_consumption_ct",
    "voltage_production_ct",
    "voltage_storage_ct",
]
_ENVOY_SENSORS = [
    "available_battery_energy",
    "battery",
    "battery_capacity",
    "reserve_battery_energy",
    "reserve_battery_level",
]
_INVERTER_SENSORS = [
    "ac_current",
    "ac_voltage",
    "dc_current",
    "dc_voltage",
    "energy_production_since_previous_report",
    "energy_production_today",
    "frequency",
    "last_report_duration",
    "last_reported",
    "lifetime_energy_production",
    "lifetime_maximum_power",
    "temperature",
]


def _core_entity_ids(batteries: list[str], sc: str, inverters: list[str]) -> set[str]:
    envoy = f"envoy_{SERIAL}"
    ids = {f"sensor.{envoy}_{key}" for key in _ENVOY_SENSORS}
    for key in _PHASED_ENVOY_SENSORS:
        ids |= {f"sensor.{envoy}_{key}{suffix}" for suffix in ("", "_l1", "_l2")}
    for battery in batteries:
        ids |= {
            f"sensor.encharge_{battery}_{key}"
            for key in ("apparent_power", "battery", "last_reported", "power", "temperature")
        }
        ids |= {f"binary_sensor.encharge_{battery}_{key}" for key in ("communicating", "dc_switch")}
    ids |= {
        f"sensor.enpower_{sc}_last_reported",
        f"sensor.enpower_{sc}_temperature",
        f"binary_sensor.enpower_{sc}_communicating",
        f"binary_sensor.enpower_{sc}_grid_status",
        f"number.enpower_{sc}_reserve_battery_level",
        f"select.enpower_{sc}_storage_mode",
        f"switch.enpower_{sc}_charge_from_grid",
    }
    for inverter in inverters:
        ids.add(f"sensor.inverter_{inverter}")
        ids |= {f"sensor.inverter_{inverter}_{key}" for key in _INVERTER_SENSORS}
    # The fixture's four contacts have no load name, so core numbers them in the Envoy's order.
    for contact, n in (("nc1", ""), ("nc2", "_2"), ("no1", "_3"), ("no2", "_4")):
        ids |= {
            f"switch.enphase_envoy_{sc}_relay_{contact}_relay_status",
            f"number.{envoy}_cutoff_battery_level{n}",
            f"number.restore_battery_level{n}",
            f"select.mode{n}",
            f"select.grid_action{n}",
            f"select.microgrid_action{n}",
            f"select.generator_action{n}",
        }
    return ids


async def test_entity_ids_match_the_core_integration(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    """Moving from the core integration keeps entity IDs, and with them history (MIGRATION.md)."""
    await _setup(hass, config_entry)
    slow = config_entry.runtime_data.slow.data
    assert slow.inventory is not None
    expected = _core_entity_ids(
        [b.serial for b in slow.inventory.batteries],
        slow.inventory.system_controllers[0].serial,
        [i.serial for i in slow.inverters],
    )
    assert len(expected) == 116 + 7 + 7 + 13 * 25 + 7 * 4
    entries = er.async_entries_for_config_entry(er.async_get(hass), config_entry.entry_id)
    assert expected - {e.entity_id for e in entries} == set()


async def test_core_entities_are_enabled_as_in_core(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    """What core leaves disabled until asked for stays disabled here (spec core-entity-parity)."""
    await _setup(hass, config_entry)
    registry = er.async_get(hass)
    envoy = f"sensor.envoy_{SERIAL}"
    inverter = config_entry.runtime_data.slow.data.inverters[0].serial

    def entry(entity_id: str) -> er.RegistryEntry:
        found = registry.async_get(entity_id)
        assert found is not None, entity_id
        return found

    for key in [
        "current_power_production",
        "current_power_consumption",
        "energy_production_today",
        "energy_production_last_seven_days",
        "lifetime_energy_production",
        "energy_consumption_today",
        "energy_consumption_last_seven_days",
        "lifetime_energy_consumption",
        "lifetime_net_energy_consumption",
        "lifetime_net_energy_production",
        "current_net_power_consumption",
        "lifetime_battery_energy_charged",
        "lifetime_battery_energy_discharged",
        "current_battery_discharge",
        "production_ct_power",
        "production_ct_energy_delivered",
        "production_ct_energy_received",
        "battery",
    ]:
        assert entry(f"{envoy}_{key}").disabled_by is None, key
    assert entry(f"sensor.inverter_{inverter}").disabled_by is None

    for key in [
        "balanced_net_power_consumption",
        "lifetime_balanced_net_energy_consumption",
        "energy_production_today_l1",
        "lifetime_energy_consumption_l2",
        "production_ct_power_l1",
        "production_ct_energy_delivered_l2",
        "frequency_net_consumption_ct",
        "voltage_storage_ct",
        "metering_status_production_ct",
        "meter_status_flags_active_net_consumption_ct",
    ]:
        assert entry(f"{envoy}_{key}").disabled_by is er.RegistryEntryDisabler.INTEGRATION, key
    for key in _INVERTER_SENSORS:
        found = entry(f"sensor.inverter_{inverter}_{key}")
        assert found.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key

    # Core's categories: the meter status sensors are diagnostic, the readings are not.
    assert entry(f"{envoy}_metering_status_production_ct").entity_category is (
        EntityCategory.DIAGNOSTIC
    )
    assert entry(f"{envoy}_production_ct_power").entity_category is None
    assert entry(f"sensor.inverter_{inverter}_temperature").entity_category is (
        EntityCategory.DIAGNOSTIC
    )
    assert entry(f"sensor.inverter_{inverter}_last_reported").entity_category is None


async def test_production_report_sensors(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    """Today and seven-day energy come from production.json, as in core."""
    await _setup(hass, config_entry)
    report = config_entry.runtime_data.slow.data.production
    assert report is not None
    envoy = f"sensor.envoy_{SERIAL}"
    for key, totals in [("production", report.production), ("consumption", report.consumption)]:
        assert totals is not None
        today = hass.states.get(f"{envoy}_energy_{key}_today")
        assert today is not None
        assert today.attributes["state_class"] == "total_increasing"
        assert today.attributes["unit_of_measurement"] == "kWh"
        assert float(today.state) == pytest.approx(totals.energy_today / 1000, abs=0.01)
        week = hass.states.get(f"{envoy}_energy_{key}_last_seven_days")
        assert week is not None
        assert "state_class" not in week.attributes
        assert float(week.state) == pytest.approx(totals.energy_last_seven_days / 1000, abs=0.1)


async def test_encharge_power_sensors(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    slow = config_entry.runtime_data.slow.data
    (serial, reading), *_ = slow.battery_power.items()
    power = hass.states.get(f"sensor.encharge_{serial}_power")
    apparent = hass.states.get(f"sensor.encharge_{serial}_apparent_power")
    assert power is not None and apparent is not None
    assert float(power.state) == reading.power
    assert power.attributes["unit_of_measurement"] == "W"
    assert float(apparent.state) == reading.apparent_power
    assert apparent.attributes["unit_of_measurement"] == "VA"


@pytest.mark.parametrize(
    "path", ["/production.json?details=1", "/ivp/ensemble/power", "/ivp/pdm/device_data"]
)
async def test_setup_survives_a_missing_core_parity_endpoint(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry, path: str
) -> None:
    """Older firmware lacks some of these; the rest of the integration must still load."""
    fake.envoy_errors[path] = EnvoyConnectionError(f"{path}: HTTP 404")
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    state = hass.states.get(f"sensor.envoy_{SERIAL}_lifetime_energy_production")
    assert state is not None
    assert state.state != STATE_UNAVAILABLE


async def test_devices_hang_off_the_envoy(
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
    devices = dr.async_entries_for_config_entry(registry, config_entry.entry_id)
    children = [d for d in devices if d.id != envoy.id and d.model != "Dry contact relay"]
    assert children
    assert all(d.via_device_id == envoy.id for d in children)
    # Each dry contact hangs off the System Controller, as in the core integration.
    (controller,) = (d for d in children if d.model == "IQ System Controller")
    relays = [d for d in devices if d.model == "Dry contact relay"]
    assert sorted(d.name for d in relays) == ["NC1", "NC2", "NO1", "NO2"]
    assert all(d.via_device_id == controller.id for d in relays)
    assert "via_device" not in caplog.text  # the form deprecated in HA 2026.9


async def test_without_stream_falls_back_to_livedata(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    fake.stream_available = False
    await _setup(hass, config_entry)
    assert config_entry.runtime_data.stream is None
    # Without the stream the core-named power sensor is the slower production report's.
    entity_id = _entity_id(hass, "sensor", "current_power_production")
    assert entity_id == f"sensor.envoy_{SERIAL}_current_power_production"
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state != STATE_UNAVAILABLE
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


INSTALLER = [
    ("sensor", "export_limit_mode", "soft"),
    ("sensor", "export_limit", "0.0"),
    ("sensor", "export_limit_type", "Aggregate"),
    ("sensor", "main_breaker_rating", "200.0"),
    ("sensor", "main_busbar_rating", "200.0"),
    ("sensor", "der_breaker_rating", "40.0"),
    ("sensor", "consumption_meter_location", "Between_Mains_Supply_and_Main_Load_Panel"),
    ("binary_sensor", "pcs_mpuavoidance", "on"),
    ("binary_sensor", "pcs_enchargeoversubscription", "off"),
]


async def test_installer_settings_are_diagnostic_sensors_on_the_envoy(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)
    envoy_id = config_entry.runtime_data.envoy_device_id
    for platform, key, value in INSTALLER:
        entity_id = _entity_id(hass, platform, key)
        assert entity_id, key
        entry = registry.async_get(entity_id)
        assert entry is not None
        assert entry.entity_category is EntityCategory.DIAGNOSTIC, key
        assert entry.device_id == envoy_id, key
        state = hass.states.get(entity_id)
        assert state is not None and state.state == value, key


async def test_installer_settings_missing_on_this_firmware(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    for path in ("/ivp/ss/pel_settings", "/ivp/ss/pcs_settings"):
        fake.envoy_errors[path] = EnvoyConnectionError(f"{path}: HTTP 404")
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    for platform, key, _ in INSTALLER:
        assert _entity_id(hass, platform, key) is None, key


async def test_installer_settings_failing_later_go_unavailable(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    fake.envoy_errors["/ivp/ss/pel_settings"] = EnvoyConnectionError("timed out")
    await config_entry.runtime_data.slow.async_refresh()
    await hass.async_block_till_done()
    assert config_entry.runtime_data.slow.last_update_success
    state = hass.states.get(_entity_id(hass, "sensor", "export_limit_mode") or "")
    assert state is not None and state.state == STATE_UNAVAILABLE
    state = hass.states.get(_entity_id(hass, "sensor", "main_breaker_rating") or "")
    assert state is not None and state.state == "200.0"


async def test_cloud_outage_does_not_block_setup(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    fake.battery_settings_error = EnlightenConnectionError("down")
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert not config_entry.runtime_data.cloud.last_update_success
    state = hass.states.get(_entity_id(hass, "select", "storage_mode"))
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
