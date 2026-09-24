"""Enphase Realtime: real-time local Envoy telemetry with local and cloud control."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import (
    CONF_CLOUD_INTERVAL,
    CONF_ENABLE_STREAM,
    CONF_FAST_INTERVAL,
    CONF_FIRMWARE,
    CONF_HAS_BATTERY,
    CONF_HAS_ENPOWER,
    CONF_PHASE_LAYOUT,
    CONF_SERIAL,
    CONF_SITE_ID,
    CONF_STREAM_INTERVAL,
    DEFAULT_CLOUD_INTERVAL,
    DEFAULT_ENABLE_STREAM,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_STREAM_INTERVAL,
    STREAM_PROBE_TIMEOUT,
)
from .coordinator import (
    CloudCoordinator,
    FastCoordinator,
    Hardware,
    SlowCoordinator,
    StreamCoordinator,
)
from .credentials import TokenKeeper, translate_errors
from .enlighten_client.battery import BatteryConfigClient
from .enlighten_client.session import EnlightenSession
from .envoy_client.local import EnvoyClient
from .envoy_client.models import PhaseLayout

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]


@dataclass(slots=True)
class EnphaseData:
    serial: str
    firmware: str
    phase_layout: PhaseLayout
    hardware: Hardware
    client: EnvoyClient
    tokens: TokenKeeper
    fast: FastCoordinator
    slow: SlowCoordinator
    cloud: CloudCoordinator | None
    # None when the stream is off in the options or the Envoy won't serve it (spec 3.1).
    stream: StreamCoordinator | None
    # The token keeper rewrites entry.data, so the update listener reloads only when these
    # differ from the entry's current options.
    options: dict


type EnphaseConfigEntry = ConfigEntry[EnphaseData]


async def async_setup_entry(hass: HomeAssistant, entry: EnphaseConfigEntry) -> bool:
    data, options = entry.data, entry.options
    serial = data[CONF_SERIAL]
    hardware = Hardware(has_battery=data[CONF_HAS_BATTERY], has_enpower=data[CONF_HAS_ENPOWER])

    # Sessions of our own: the cloud login lives in its cookie jar, and the Envoy's certificate
    # is self-signed. HA detaches both when the entry unloads.
    cloud_http = async_create_clientsession(hass)
    envoy_http = async_create_clientsession(hass, verify_ssl=False)

    cloud = EnlightenSession(cloud_http, data[CONF_EMAIL], data[CONF_PASSWORD])
    tokens = TokenKeeper(hass, entry, cloud, cloud_http, serial)
    client = EnvoyClient(envoy_http, data[CONF_HOST], tokens.token, token_refresher=tokens.refresh)
    if tokens.token is None:
        with translate_errors("owner token"):
            client.set_token(await tokens.refresh())

    fast = FastCoordinator(
        hass,
        entry,
        client,
        hardware,
        timedelta(seconds=options.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)),
    )
    slow = SlowCoordinator(hass, entry, client, hardware, tokens)
    await fast.async_config_entry_first_refresh()
    await slow.async_config_entry_first_refresh()

    cloud_coordinator = None
    if hardware.has_battery:
        cloud_coordinator = CloudCoordinator(
            hass,
            entry,
            BatteryConfigClient(cloud, data[CONF_SITE_ID]),
            timedelta(seconds=options.get(CONF_CLOUD_INTERVAL, DEFAULT_CLOUD_INTERVAL)),
        )
        # Not a first refresh: a cloud outage mustn't stop local telemetry (spec 7). A rejected
        # login still starts reauth from inside the coordinator.
        await cloud_coordinator.async_refresh()

    stream = None
    if options.get(CONF_ENABLE_STREAM, DEFAULT_ENABLE_STREAM):
        stream = StreamCoordinator(
            hass,
            entry,
            client,
            timedelta(seconds=options.get(CONF_STREAM_INTERVAL, DEFAULT_STREAM_INTERVAL)),
        )
        stream.start(entry)
        # Wait briefly to learn whether the Envoy serves the stream at all. A slow first frame
        # still gets stream entities; they stay unavailable until it arrives.
        try:
            await asyncio.wait_for(stream.settled.wait(), STREAM_PROBE_TIMEOUT)
        except TimeoutError:
            _LOGGER.debug("No stream frame yet; creating stream entities anyway")
        if stream.unavailable:
            stream = None

    entry.runtime_data = EnphaseData(
        serial=serial,
        firmware=data[CONF_FIRMWARE],
        phase_layout=PhaseLayout(data[CONF_PHASE_LAYOUT]),
        hardware=hardware,
        client=client,
        tokens=tokens,
        fast=fast,
        slow=slow,
        cloud=cloud_coordinator,
        stream=stream,
        options=dict(entry.options),
    )
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: EnphaseConfigEntry) -> None:
    if entry.runtime_data.options != dict(entry.options):
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EnphaseConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
