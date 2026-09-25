"""The 1 s live poll and the stream write throttle (spec 3.1, 3.2)."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.enphase_realtime.const import (
    CONF_LIVE_INTERVAL,
    CONF_STREAM_INTERVAL,
    DOMAIN,
)
from custom_components.enphase_realtime.envoy_client.errors import EnvoyConnectionError

from .conftest import SERIAL, FakeEnphase, entry_data

LIVEDATA = "/ivp/livedata/status"
RELAY = "/ivp/ensemble/relay"
ENABLE = "/ivp/livedata/stream"


def _state(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{SERIAL}_{key}")
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None, key
    return state.state


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _live_tick(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.live.async_refresh()
    await hass.async_block_till_done()


async def test_live_poll_defaults_to_one_second(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    assert config_entry.runtime_data.live.update_interval == timedelta(seconds=1)


async def test_live_entities_ride_out_two_failures(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    live = config_entry.runtime_data.live
    before = _state(hass, "sensor", "battery_power")
    assert before != STATE_UNAVAILABLE

    fake.envoy_errors[LIVEDATA] = EnvoyConnectionError("timeout")
    for _ in range(2):
        await _live_tick(hass, config_entry)
        assert not live.last_update_success
        assert _state(hass, "sensor", "battery_power") == before
        assert _state(hass, "binary_sensor", "grid_status") == STATE_ON

    await _live_tick(hass, config_entry)
    assert _state(hass, "sensor", "battery_power") == STATE_UNAVAILABLE
    assert _state(hass, "binary_sensor", "grid_status") == STATE_UNAVAILABLE
    # Other coordinators' entities are untouched.
    assert _state(hass, "sensor", "battery_soc") != STATE_UNAVAILABLE

    del fake.envoy_errors[LIVEDATA]
    await _live_tick(hass, config_entry)
    assert live.failed_polls == 0
    assert _state(hass, "sensor", "battery_power") == before


async def test_grid_status_follows_the_live_relay(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    assert _state(hass, "binary_sensor", "grid_outage") == STATE_OFF

    fake.envoy_overrides[RELAY] = {"mains_oper_state": "open"}
    await _live_tick(hass, config_entry)
    assert _state(hass, "binary_sensor", "grid_status") == STATE_OFF
    assert _state(hass, "binary_sensor", "grid_outage") == STATE_ON


async def test_sc_stream_enabled_sends_nothing(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    await _live_tick(hass, config_entry)
    assert fake.envoy_posts == []


async def test_sc_stream_disabled_is_enabled_at_most_once_a_minute(
    hass: HomeAssistant,
    fake: FakeEnphase,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    fake.envoy_overrides[LIVEDATA] = {"connection": {"sc_stream": "disabled"}}
    await _setup(hass, config_entry)
    assert fake.envoy_posts == [(ENABLE, {"enable": 1})]

    freezer.tick(timedelta(seconds=30))
    await _live_tick(hass, config_entry)
    assert len(fake.envoy_posts) == 1

    freezer.tick(timedelta(seconds=31))
    await _live_tick(hass, config_entry)
    assert len(fake.envoy_posts) == 2


async def test_failed_sc_stream_enable_does_not_fail_the_poll(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    fake.envoy_overrides[LIVEDATA] = {"connection": {"sc_stream": "disabled"}}
    fake.envoy_errors[ENABLE] = EnvoyConnectionError("HTTP 500")
    await _setup(hass, config_entry)
    assert config_entry.runtime_data.live.last_update_success
    assert _state(hass, "sensor", "battery_power") != STATE_UNAVAILABLE


async def test_live_interval_option(hass: HomeAssistant, fake: FakeEnphase) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=SERIAL, data=entry_data(), options={CONF_LIVE_INTERVAL: 5}
    )
    await _setup(hass, entry)
    assert entry.runtime_data.live.update_interval == timedelta(seconds=5)


# --- Stream throttle ----------------------------------------------------------------------------


async def _stream_writes(
    hass: HomeAssistant, fake: FakeEnphase, interval: int, freezer: FrozenDateTimeFactory
) -> list[int]:
    """Feed frames at fixed offsets (ms); return the offsets that were published."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        data=entry_data(),
        options={CONF_STREAM_INTERVAL: interval},
    )
    await _setup(hass, entry)
    stream = entry.runtime_data.stream
    assert stream is not None
    published: list[int] = []
    offset = 0
    stream.async_add_listener(lambda: published.append(offset))

    # Setup already published the fake's frames; start clear of that write.
    freezer.tick(timedelta(seconds=interval + 1))
    base = dt_util.utcnow()
    frame = fake.stream_frames[0]
    for offset in (0, 950, 1900, 2900, 4850, 5800, 9800, 10500):
        freezer.move_to(base + timedelta(milliseconds=offset))
        stream.handle_frame(frame)
    return published


async def test_stream_writes_every_frame_by_default(
    hass: HomeAssistant, fake: FakeEnphase, freezer: FrozenDateTimeFactory
) -> None:
    assert await _stream_writes(hass, fake, 0, freezer) == [
        0,
        950,
        1900,
        2900,
        4850,
        5800,
        9800,
        10500,
    ]


async def test_stream_throttle_writes_the_leading_frame(
    hass: HomeAssistant, fake: FakeEnphase, freezer: FrozenDateTimeFactory
) -> None:
    # A frame a little early (4850 after 0) still counts as 5 s on; 10500 is only 700 ms after
    # 9800, so it waits.
    assert await _stream_writes(hass, fake, 5, freezer) == [0, 4850, 9800]


async def test_stream_throttle_still_goes_stale(
    hass: HomeAssistant,
    fake: FakeEnphase,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, config_entry)
    stream = config_entry.runtime_data.stream
    assert stream is not None
    assert stream.last_update_success

    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert not stream.last_update_success
