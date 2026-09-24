"""Cloud-backed controls: write, then confirm from the Envoy (spec 6.1, 6.2)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.number import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.enphase_realtime.const import CONF_COUNTRY, CONF_TIME_ZONE, DOMAIN
from custom_components.enphase_realtime.enlighten_client.errors import (
    EnlightenAuthError,
    EnlightenConnectionError,
)
from custom_components.enphase_realtime.envoy_client.errors import EnvoyConnectionError

from .conftest import SERIAL, SITE, FakeEnphase, entry_data

SECCTRL = "/ivp/ensemble/secctrl"
SCHED = "/ivp/sc/sched"
BATTERY_SETTINGS = f"/service/batteryConfig/api/v1/batterySettings/{SITE}"
ACCEPT_DISCLAIMER = f"/service/batteryConfig/api/v1/batterySettings/acceptDisclaimer/{SITE}"


def _entity_id(hass: HomeAssistant, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{SERIAL}_{key}")


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> tuple[str | None, str | None]:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return _entity_id(hass, "switch", "allow_charge_from_grid"), _entity_id(
        hass, "number", "very_low_soc"
    )


async def _set_very_low_soc(hass: HomeAssistant, entity_id: str, value: int) -> None:
    await hass.services.async_call(
        "number", SERVICE_SET_VALUE, {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value}, blocking=True
    )


async def _local_tick(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.fast.async_refresh()
    await hass.async_block_till_done()


async def test_controls_show_local_values(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    switch, number = await _setup(hass, config_entry)
    # The reference site has showChargeFromGrid false but cfgControl.show true (spec 3.3).
    assert switch is not None
    assert number is not None

    state = hass.states.get(number)
    assert state is not None
    assert state.state == "10"
    assert (state.attributes["min"], state.attributes["max"]) == (5, 25)
    assert "confirmation" not in state.attributes

    state = hass.states.get(switch)
    assert state is not None
    assert state.state == STATE_ON  # sc/sched, not the cloud's chargeFromGrid (false)
    assert state.attributes["charge_begin_time"] == "02:00"
    assert state.attributes["charge_end_time"] == "05:00"
    assert state.attributes["schedule_enabled"] is False
    assert "confirmation" not in state.attributes


async def test_restricted_site_gets_no_switch(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    fake.site_settings_overrides = {"showChargeFromGrid": True, "restrictCfg": True}
    switch, number = await _setup(hass, config_entry)
    assert switch is None
    assert number is not None


async def test_no_battery_no_controls(hass: HomeAssistant, fake: FakeEnphase) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, data=entry_data(has_battery=False))
    assert await _setup(hass, entry) == (None, None)


async def test_very_low_soc_confirmed(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    _, number = await _setup(hass, config_entry)
    assert number is not None
    await _set_very_low_soc(hass, number, 15)

    assert fake.writes == [("PUT", BATTERY_SETTINGS, {"veryLowSoc": 15})]
    state = hass.states.get(number)
    assert state is not None
    assert (state.state, state.attributes["confirmation"]) == ("15", "pending")

    # The Envoy hasn't caught up: still pending, still showing what was asked for.
    await _local_tick(hass, config_entry)
    state = hass.states.get(number)
    assert state is not None
    assert (state.state, state.attributes["confirmation"]) == ("15", "pending")

    fake.envoy_overrides[SECCTRL] = {"VLS_Limit": 15}
    await _local_tick(hass, config_entry)
    state = hass.states.get(number)
    assert state is not None
    assert (state.state, state.attributes["confirmation"]) == ("15", "confirmed")


async def test_very_low_soc_not_confirmed_reverts(
    hass: HomeAssistant,
    fake: FakeEnphase,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, number = await _setup(hass, config_entry)
    assert number is not None
    await _set_very_low_soc(hass, number, 15)

    freezer.tick(timedelta(seconds=91))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get(number)
    assert state is not None
    assert (state.state, state.attributes["confirmation"]) == ("10", "failed")
    assert "the Envoy still reports 10" in caplog.text


async def test_timeout_fails_even_with_envoy_down(
    hass: HomeAssistant,
    fake: FakeEnphase,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With the fast coordinator failing there are no ticks; the timer still settles it."""
    _, number = await _setup(hass, config_entry)
    assert number is not None
    await _set_very_low_soc(hass, number, 15)
    fake.envoy_errors[SECCTRL] = EnvoyConnectionError("down")
    await _local_tick(hass, config_entry)
    await _local_tick(hass, config_entry)

    freezer.tick(timedelta(seconds=91))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get(number)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    assert "the Envoy still reports 10" in caplog.text

    del fake.envoy_errors[SECCTRL]
    await _local_tick(hass, config_entry)
    state = hass.states.get(number)
    assert state is not None
    assert (state.state, state.attributes["confirmation"]) == ("10", "failed")


async def test_charge_from_grid_off_confirmed(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    switch, _ = await _setup(hass, config_entry)
    assert switch is not None
    await hass.services.async_call(
        "switch", SERVICE_TURN_OFF, {ATTR_ENTITY_ID: switch}, blocking=True
    )

    assert fake.writes == [("PUT", BATTERY_SETTINGS, {"chargeFromGrid": False})]
    state = hass.states.get(switch)
    assert state is not None
    assert (state.state, state.attributes["confirmation"]) == (STATE_OFF, "pending")

    fake.envoy_overrides[SCHED] = {"Charge From Grid Allowed": 0}
    await _local_tick(hass, config_entry)
    state = hass.states.get(switch)
    assert state is not None
    assert (state.state, state.attributes["confirmation"]) == (STATE_OFF, "confirmed")


@pytest.mark.parametrize("country", ["US", "DE"])
async def test_charge_from_grid_on(hass: HomeAssistant, fake: FakeEnphase, country: str) -> None:
    """The ITC disclaimer goes with "on" in the US only (spec 3.3); the times are kept."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        data=entry_data(),
        options={CONF_COUNTRY: country, CONF_TIME_ZONE: "US/Eastern"},
    )
    fake.envoy_overrides[SCHED] = {"Charge From Grid Allowed": 0}
    switch, _ = await _setup(hass, entry)
    assert switch is not None
    state = hass.states.get(switch)
    assert state is not None
    assert state.state == STATE_OFF

    await hass.services.async_call(
        "switch", SERVICE_TURN_ON, {ATTR_ENTITY_ID: switch}, blocking=True
    )

    body = {
        "chargeFromGrid": True,
        "chargeFromGridScheduleEnabled": False,
        "chargeBeginTime": 120,
        "chargeEndTime": 300,
    }
    if country == "US":
        assert fake.writes == [
            ("POST", ACCEPT_DISCLAIMER, {"disclaimer-type": "itc"}),
            ("PUT", BATTERY_SETTINGS, body | {"acceptedItcDisclaimer": True}),
        ]
    else:
        assert fake.writes == [("PUT", BATTERY_SETTINGS, body)]
    state = hass.states.get(switch)
    assert state is not None
    assert (state.state, state.attributes["confirmation"]) == (STATE_ON, "pending")

    fake.envoy_overrides[SCHED] = {"Charge From Grid Allowed": 1}
    await _local_tick(hass, entry)
    state = hass.states.get(switch)
    assert state is not None
    assert state.attributes["confirmation"] == "confirmed"


async def test_rejected_write_raises_and_keeps_state(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    _, number = await _setup(hass, config_entry)
    assert number is not None
    fake.write_error = EnlightenConnectionError("batterySettings: HTTP 500")

    with pytest.raises(HomeAssistantError, match="HTTP 500"):
        await _set_very_low_soc(hass, number, 15)
    state = hass.states.get(number)
    assert state is not None
    assert state.state == "10"
    assert "confirmation" not in state.attributes


async def test_refused_login_on_write_starts_reauth(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    switch, _ = await _setup(hass, config_entry)
    assert switch is not None
    fake.write_error = EnlightenAuthError("refused")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch", SERVICE_TURN_OFF, {ATTR_ENTITY_ID: switch}, blocking=True
        )
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    state = hass.states.get(switch)
    assert state is not None
    assert state.state == STATE_ON


async def test_cloud_outage_takes_controls_down(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    switch, number = await _setup(hass, config_entry)
    assert switch is not None
    assert number is not None
    fake.battery_settings_error = EnlightenConnectionError("down")
    await config_entry.runtime_data.cloud.async_refresh()
    await hass.async_block_till_done()

    for entity_id in (switch, number):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == STATE_UNAVAILABLE, entity_id
