"""The Enphase overhead entities (docs/specs/enphase-overhead.md)."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.enphase_realtime import coordinator, sensor
from custom_components.enphase_realtime.const import (
    CONF_BACKUP_LOAD_ENTITY,
    CONF_COUNTRY,
    CONF_TIME_ZONE,
    DOMAIN,
    LIVE_STAMP_LOG_AFTER,
    OVERHEAD_STALE_LOG_AFTER,
)
from custom_components.enphase_realtime.envoy_client.errors import EnvoyConnectionError
from tests.helpers import load_json

from .conftest import SERIAL, FakeEnphase, entry_data

LIVEDATA = "/ivp/livedata/status"
BACKUP = "sensor.span_main"
POWER = "enphase_overhead_power"
ENERGY = "enphase_overhead_energy"
# The fixture's livedata load is 1213.8 W.


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(sensor, "time", c)
    monkeypatch.setattr(coordinator, "time", c)
    return c


def _entry(**options: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        data=entry_data(),
        options={CONF_COUNTRY: "US", CONF_TIME_ZONE: "US/Eastern", **options},
    )


def _entity_id(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_{key}")


def _state(hass: HomeAssistant, key: str) -> State:
    entity_id = _entity_id(hass, key)
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None, key
    return state


def _set_load(fake: FakeEnphase, watts: float) -> None:
    meters = load_json("ivp_livedata_status.json")["meters"]
    meters["load"]["agg_p_mw"] = round(watts * 1000)
    fake.envoy_overrides[LIVEDATA] = {"meters": meters}


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _live_tick(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.live.async_refresh()
    await hass.async_block_till_done()


async def test_no_backup_sensor_no_entities(hass: HomeAssistant, fake: FakeEnphase) -> None:
    await _setup(hass, _entry())
    assert _entity_id(hass, POWER) is None
    assert _entity_id(hass, ENERGY) is None


async def test_overhead_is_load_less_backup(
    hass: HomeAssistant, fake: FakeEnphase, clock: Clock
) -> None:
    hass.states.async_set(BACKUP, "1.2", {"unit_of_measurement": "kW"})
    entry = _entry(**{CONF_BACKUP_LOAD_ENTITY: BACKUP})
    await _setup(hass, entry)
    power = _state(hass, POWER)
    assert float(power.state) == pytest.approx(13.8)
    assert power.attributes["unit_of_measurement"] == "W"
    assert power.attributes["device_class"] == "power"
    energy = _state(hass, ENERGY)
    assert float(energy.state) == 0
    assert energy.attributes["state_class"] == "total"

    # 13.8 W for 18 s, then a 33.8 W sample: the power is the mean, the energy the left sum.
    clock.now += 18
    _set_load(fake, 1233.8)
    await _live_tick(hass, entry)
    assert float(_state(hass, POWER).state) == pytest.approx(23.8)
    assert float(_state(hass, ENERGY).state) == pytest.approx(13.8 * 18 / 3600)


async def test_power_unavailable_without_a_backup_reading(
    hass: HomeAssistant, fake: FakeEnphase
) -> None:
    hass.states.async_set(BACKUP, STATE_UNAVAILABLE)
    await _setup(hass, _entry(**{CONF_BACKUP_LOAD_ENTITY: BACKUP}))
    assert _state(hass, POWER).state == STATE_UNAVAILABLE
    assert float(_state(hass, ENERGY).state) == 0


async def test_power_unavailable_with_the_live_entities(
    hass: HomeAssistant, fake: FakeEnphase
) -> None:
    hass.states.async_set(BACKUP, "1200", {"unit_of_measurement": "W"})
    entry = _entry(**{CONF_BACKUP_LOAD_ENTITY: BACKUP})
    await _setup(hass, entry)
    assert _state(hass, POWER).state != STATE_UNAVAILABLE

    fake.envoy_errors[LIVEDATA] = EnvoyConnectionError("timeout")
    for _ in range(3):
        await _live_tick(hass, entry)
    assert _state(hass, POWER).state == STATE_UNAVAILABLE
    assert _state(hass, ENERGY).state != STATE_UNAVAILABLE


async def test_energy_is_restored(hass: HomeAssistant, fake: FakeEnphase) -> None:
    entity_id = f"sensor.envoy_{SERIAL}_{ENERGY}"
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(entity_id, "250"),
                {"native_value": 250.0, "native_unit_of_measurement": "Wh"},
            ),
        ),
    )
    hass.states.async_set(BACKUP, "1200", {"unit_of_measurement": "W"})
    await _setup(hass, _entry(**{CONF_BACKUP_LOAD_ENTITY: BACKUP}))
    assert _entity_id(hass, ENERGY) == entity_id
    assert float(_state(hass, ENERGY).state) == 250


async def test_clearing_the_option_removes_the_entities(
    hass: HomeAssistant, fake: FakeEnphase
) -> None:
    hass.states.async_set(BACKUP, "1200", {"unit_of_measurement": "W"})
    entry = _entry(**{CONF_BACKUP_LOAD_ENTITY: BACKUP})
    await _setup(hass, entry)
    assert _entity_id(hass, POWER) is not None

    hass.config_entries.async_update_entry(
        entry, options={CONF_COUNTRY: "US", CONF_TIME_ZONE: "US/Eastern"}
    )
    await hass.async_block_till_done()
    assert _entity_id(hass, POWER) is None
    assert _entity_id(hass, ENERGY) is None


async def test_options_offer_the_backup_sensor(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert CONF_BACKUP_LOAD_ENTITY in result["data_schema"].schema


async def test_a_held_livedata_timestamp_is_logged(
    hass: HomeAssistant, fake: FakeEnphase, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="custom_components.enphase_realtime.coordinator")
    hass.states.async_set(BACKUP, "1200", {"unit_of_measurement": "W"})
    entry = _entry(**{CONF_BACKUP_LOAD_ENTITY: BACKUP})
    await _setup(hass, entry)

    # The fixture's timestamp never moves.
    clock.now += LIVE_STAMP_LOG_AFTER
    await _live_tick(hass, entry)
    assert "meters.last_update unchanged" in caplog.text

    meters = load_json("ivp_livedata_status.json")["meters"]
    meters["last_update"] += 5
    fake.envoy_overrides[LIVEDATA] = {"meters": meters}
    clock.now += 1
    await _live_tick(hass, entry)
    assert "meters.last_update advanced" in caplog.text


async def test_only_a_lasting_stale_run_is_logged(
    hass: HomeAssistant, fake: FakeEnphase, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="custom_components.enphase_realtime.sensor")
    hass.states.async_set(BACKUP, "1200", {"unit_of_measurement": "W"})
    entry = _entry(**{CONF_BACKUP_LOAD_ENTITY: BACKUP})
    await _setup(hass, entry)
    meters = load_json("ivp_livedata_status.json")["meters"]

    async def poll(load_moves: bool) -> None:
        # A fresh timestamp every poll, as the Envoy gives even while it repeats old values.
        meters["last_update"] += 1
        if load_moves:
            meters["load"]["agg_p_mw"] += 1000
        fake.envoy_overrides[LIVEDATA] = {"meters": meters}
        clock.now += 1
        await _live_tick(hass, entry)

    # One repeat, as the Envoy's own update rate gives: skipped, not logged.
    await poll(load_moves=False)
    await poll(load_moves=True)
    assert "Envoy load" not in caplog.text

    for _ in range(int(OVERHEAD_STALE_LOG_AFTER) + 1):
        await poll(load_moves=False)
    assert "Envoy load held" in caplog.text
    await poll(load_moves=True)
    assert "Envoy load moving again" in caplog.text


async def test_a_held_backup_load_is_skipped(
    hass: HomeAssistant, fake: FakeEnphase, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="custom_components.enphase_realtime.sensor")
    hass.states.async_set(BACKUP, "1200", {"unit_of_measurement": "W"})
    entry = _entry(**{CONF_BACKUP_LOAD_ENTITY: BACKUP})
    await _setup(hass, entry)
    load = 1213.8

    async def poll(backup: float) -> None:
        # Re-set every poll, as the SPAN integration re-reported its frozen value.
        nonlocal load
        load += 1
        _set_load(fake, load)
        hass.states.async_set(BACKUP, str(backup), {"unit_of_measurement": "W"})
        clock.now += 10
        await _live_tick(hass, entry)

    for _ in range(3):
        await poll(1200)
    assert "backup load held" not in caplog.text
    await poll(1200)
    assert "backup load held at 1200.0 W for 40 s" in caplog.text
    energy = float(_state(hass, ENERGY).state)

    # Moving again, but the samples wait for the backup load to settle.
    for i in range(1, 12):
        await poll(1200 + i)
    assert float(_state(hass, ENERGY).state) == energy
    assert "taking samples again" not in caplog.text
    await poll(1212)
    assert "backup load fresh for 120 s, taking samples again" in caplog.text
    await poll(1213)
    assert float(_state(hass, ENERGY).state) > energy
