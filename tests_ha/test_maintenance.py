"""Battery maintenance's entities (docs/specs/battery-maintenance.md)."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.number import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

from custom_components.enphase_realtime import switch as switch_module
from custom_components.enphase_realtime.const import DOMAIN
from custom_components.enphase_realtime.enlighten_client.errors import EnlightenConnectionError
from custom_components.enphase_realtime.maintenance import STUCK_AFTER, WRITE_BACKOFF

from .conftest import SERIAL, SITE, FakeEnphase, load_json

SECCTRL = "/ivp/ensemble/secctrl"
SCHED = "/ivp/sc/sched"
LIVEDATA = "/ivp/livedata/status"
BATTERY_SETTINGS = f"/service/batteryConfig/api/v1/batterySettings/{SITE}"
ACCEPT_DISCLAIMER = f"/service/batteryConfig/api/v1/batterySettings/acceptDisclaimer/{SITE}"
ON_WRITES = [
    ("POST", ACCEPT_DISCLAIMER, {"disclaimer-type": "itc"}),
    (
        "PUT",
        BATTERY_SETTINGS,
        {
            "chargeFromGrid": True,
            "chargeFromGridScheduleEnabled": False,
            "chargeBeginTime": 120,
            "chargeEndTime": 300,
            "acceptedItcDisclaimer": True,
        },
    ),
]
OFF_WRITE = ("PUT", BATTERY_SETTINGS, {"chargeFromGrid": False})
# The fixture: Full Backup, battery at 59%, charge from grid allowed, no PV, battery idle.


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(switch_module, "time", c)
    return c


def _entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{SERIAL}_{key}")
    assert entity_id is not None
    return entity_id


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return _entity_id(hass, "switch", "battery_maintenance")


async def _switch(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    await hass.services.async_call(
        "switch", SERVICE_TURN_ON if on else SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}
    )
    await hass.async_block_till_done()


async def _set_number(hass: HomeAssistant, key: str, value: int) -> None:
    await hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: _entity_id(hass, "number", key), ATTR_VALUE: value},
        blocking=True,
    )


async def _tick(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.live.async_refresh()
    await entry.runtime_data.fast.async_refresh()
    await hass.async_block_till_done()


def _status(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.attributes["status"]


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"maintenance_charge_stuck_{entry.entry_id}")


async def test_entities_and_defaults(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    switch = await _setup(hass, config_entry)
    state = hass.states.get(switch)
    assert state is not None
    assert state.state == "off"
    assert state.attributes["status"] == "inactive"
    for key, value in (
        ("maintenance_charge_start_level", "90"),
        ("maintenance_charge_stop_level", "100"),
    ):
        number = hass.states.get(_entity_id(hass, "number", key))
        assert number is not None
        assert number.state == value


async def test_restricted_site_gets_no_maintenance(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    fake.site_settings_overrides = {"showChargeFromGrid": True, "restrictCfg": True}
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    for platform, key in (
        ("switch", "battery_maintenance"),
        ("number", "maintenance_charge_start_level"),
        ("number", "maintenance_charge_stop_level"),
    ):
        assert registry.async_get_entity_id(platform, DOMAIN, f"{SERIAL}_{key}") is None


async def test_start_must_stay_below_stop(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError):
        await _set_number(hass, "maintenance_charge_stop_level", 90)
    await _set_number(hass, "maintenance_charge_start_level", 50)
    await _set_number(hass, "maintenance_charge_stop_level", 80)
    assert config_entry.runtime_data.maintenance.start == 50
    assert config_entry.runtime_data.maintenance.stop == 80


async def test_charge_cycle(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry, clock: Clock
) -> None:
    fake.envoy_overrides[SCHED] = {"Charge From Grid Allowed": 0}
    switch = await _setup(hass, config_entry)
    await _tick(hass, config_entry)
    assert fake.writes == []  # off: nothing

    await _switch(hass, switch, True)
    assert fake.writes == ON_WRITES
    assert _status(hass, switch) == "charging"

    fake.writes.clear()
    fake.envoy_overrides[SCHED] = {"Charge From Grid Allowed": 1}
    fake.envoy_overrides[SECCTRL] = {"agg_soc": 99}
    await _tick(hass, config_entry)
    assert fake.writes == []  # between the levels

    fake.envoy_overrides[SECCTRL] = {"agg_soc": 100}
    await _tick(hass, config_entry)
    assert fake.writes == [OFF_WRITE]
    assert _status(hass, switch) == "idle"


async def test_turning_maintenance_off_ends_its_charge(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry, clock: Clock
) -> None:
    switch = await _setup(hass, config_entry)
    # Charge from grid is already on at 59%: adopted, no write.
    await _switch(hass, switch, True)
    assert fake.writes == []
    assert _status(hass, switch) == "charging"
    await _switch(hass, switch, False)
    assert fake.writes == [OFF_WRITE]
    assert _status(hass, switch) == "inactive"


async def test_stuck_retries_then_raises_issue(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry, clock: Clock
) -> None:
    switch = await _setup(hass, config_entry)
    await _switch(hass, switch, True)
    assert _status(hass, switch) == "charging"

    clock.now += STUCK_AFTER
    await _tick(hass, config_entry)
    assert fake.writes == [OFF_WRITE]
    assert _status(hass, switch) == "retrying"

    fake.writes.clear()
    fake.envoy_overrides[SCHED] = {"Charge From Grid Allowed": 0}
    await _tick(hass, config_entry)
    assert fake.writes == ON_WRITES
    fake.envoy_overrides[SCHED] = {"Charge From Grid Allowed": 1}

    fake.writes.clear()
    clock.now += STUCK_AFTER
    await _tick(hass, config_entry)
    assert fake.writes == []
    assert _status(hass, switch) == "stuck"
    issue = _issue(hass, config_entry)
    assert issue is not None
    assert issue.translation_placeholders == {
        "soc": "59",
        "mode": "Charge From PV",
        "minutes": "10",
    }

    # The battery charges (the owner changed the profile): the issue goes.
    fake.envoy_overrides[LIVEDATA] = {"meters": _meters_charging()}
    await _tick(hass, config_entry)
    assert _status(hass, switch) == "charging"
    assert _issue(hass, config_entry) is None


async def test_failed_write_waits(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry, clock: Clock
) -> None:
    fake.envoy_overrides[SCHED] = {"Charge From Grid Allowed": 0}
    switch = await _setup(hass, config_entry)
    fake.write_error = EnlightenConnectionError("batterySettings: HTTP 500")
    await _switch(hass, switch, True)
    assert _status(hass, switch) == "idle"
    fake.write_error = None
    await _tick(hass, config_entry)
    assert fake.writes == []  # waiting out the backoff

    clock.now += WRITE_BACKOFF
    await _tick(hass, config_entry)
    assert fake.writes == ON_WRITES
    assert _status(hass, switch) == "charging"


async def test_restores_state_and_ownership(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    mock_restore_cache(
        hass,
        [State(f"switch.envoy_{SERIAL}_battery_maintenance", STATE_ON, {"owned": True})],
    )
    switch = await _setup(hass, config_entry)
    assert switch == f"switch.envoy_{SERIAL}_battery_maintenance"
    state = hass.states.get(switch)
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes["status"] == "charging"


def _meters_charging() -> dict[str, Any]:
    meters = load_json("ivp_livedata_status.json")["meters"]
    meters["storage"] = {**meters["storage"], "agg_p_mw": -3_800_000, "agg_s_mva": 3_800_000}
    return meters
