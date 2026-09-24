"""The four coordinators of spec 3.1: stream (push), fast, slow and cloud."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import CONF_PHASE_LAYOUT, DOMAIN, SLOW_INTERVAL, STREAM_STALE_AFTER
from .credentials import TokenKeeper, translate_errors
from .enlighten_client.battery import BatteryConfigClient
from .enlighten_client.models import BatterySettings
from .envoy_client.errors import EnvoyAuthError, EnvoyError, EnvoyStreamUnavailable
from .envoy_client.local import EnvoyClient
from .envoy_client.models import (
    DryContactSettings,
    Inventory,
    Inverter,
    LifetimeEnergy,
    LiveData,
    Meter,
    PhaseLayout,
    Relay,
    Schedule,
    SecCtrl,
    StreamFrame,
    detect_phase_layout,
)
from .envoy_client.stream import run_stream

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Hardware:
    """What the site has, fixed at setup (spec 3.3). Endpoints for missing hardware aren't
    polled."""

    has_battery: bool
    has_enpower: bool


# --- Fast ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FastData:
    livedata: LiveData
    secctrl: SecCtrl | None
    schedule: Schedule | None
    relay: Relay | None


class FastCoordinator(DataUpdateCoordinator[FastData]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EnvoyClient,
        hardware: Hardware,
        interval: timedelta,
    ) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} fast", update_interval=interval
        )
        self.client = client
        self._hw = hardware

    async def _async_update_data(self) -> FastData:
        c, hw = self.client, self._hw

        async def none() -> None:
            return None

        with translate_errors("Envoy"):
            livedata, secctrl, schedule, relay = await asyncio.gather(
                c.livedata(),
                c.secctrl() if hw.has_battery else none(),
                c.schedule() if hw.has_battery else none(),
                c.relay() if hw.has_enpower else none(),
            )
        return FastData(livedata, secctrl, schedule, relay)


# --- Slow ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlowData:
    meters: list[Meter]
    energy: LifetimeEnergy
    inventory: Inventory | None
    dry_contact_settings: dict[str, DryContactSettings]
    dry_contact_states: dict[str, bool]
    inverters: list[Inverter]


class SlowCoordinator(DataUpdateCoordinator[SlowData]):
    """Also renews the owner token ahead of expiry and watches for a changed CT layout."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EnvoyClient,
        hardware: Hardware,
        tokens: TokenKeeper,
    ) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} slow", update_interval=SLOW_INTERVAL
        )
        self.client = client
        self._hw = hardware
        self._tokens = tokens
        self._layout = PhaseLayout(entry.data[CONF_PHASE_LAYOUT])

    async def _async_update_data(self) -> SlowData:
        c, hw = self.client, self._hw
        if self._tokens.needs_renewal():
            with translate_errors("owner token"):
                c.set_token(await self._tokens.refresh())

        async def empty() -> dict:
            return {}

        async def none() -> None:
            return None

        with translate_errors("Envoy"):
            (
                meters,
                readings,
                reports,
                inventory,
                contacts,
                states,
                inverters,
            ) = await asyncio.gather(
                c.meters(),
                c.meter_readings(),
                c.meter_reports(),
                c.inventory() if hw.has_battery or hw.has_enpower else none(),
                c.dry_contact_settings() if hw.has_enpower else empty(),
                c.dry_contact_states() if hw.has_enpower else empty(),
                c.inverters(),
            )
            energy = LifetimeEnergy.from_payloads(meters, readings, reports)
        self._check_layout(meters)
        return SlowData(meters, energy, inventory, contacts, states, inverters)

    def _check_layout(self, meters: list[Meter]) -> None:
        """An installer changing the CTs needs a reload, not entities rebuilt in place."""
        detected = detect_phase_layout(meters)
        issue_id = f"phase_layout_changed_{self.config_entry.entry_id}"
        if detected is None or detected == self._layout:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="phase_layout_changed",
            translation_placeholders={"stored": self._layout.value, "detected": detected.value},
        )


# --- Cloud --------------------------------------------------------------------------------------


class CloudCoordinator(DataUpdateCoordinator[BatterySettings]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        battery: BatteryConfigClient,
        interval: timedelta,
    ) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} cloud", update_interval=interval
        )
        self.battery = battery

    async def _async_update_data(self) -> BatterySettings:
        with translate_errors("Enlighten"):
            return await self.battery.battery_settings()


# --- Stream -------------------------------------------------------------------------------------


class StreamCoordinator(DataUpdateCoordinator[StreamFrame]):
    """Push, not poll. A background task keeps the newest frame; entities are told about it at
    most once every `interval`, which keeps the recorder load down (spec 3.2)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EnvoyClient,
        interval: timedelta,
    ) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} stream")
        self.client = client
        self._interval = interval
        self._latest: StreamFrame | None = None
        self._latest_at: datetime | None = None
        self._published_at: datetime | None = None
        # Set once the stream has either sent a frame or refused us.
        self.settled = asyncio.Event()
        # True when the Envoy won't offer the stream to this token; retrying won't help.
        self.unavailable = False

    async def _async_update_data(self) -> StreamFrame:
        """Never polled; `async_refresh` just re-publishes what the stream has sent."""
        if self._latest is None:
            raise UpdateFailed("no stream frame yet")
        return self._latest

    @callback
    def handle_frame(self, frame: StreamFrame) -> None:
        self._latest = frame
        self._latest_at = dt_util.utcnow()
        if self.data is None:
            self.async_set_updated_data(frame)
            self._published_at = self._latest_at
        self.settled.set()

    @callback
    def handle_disconnect(self, err: EnvoyError) -> None:
        _LOGGER.debug("Envoy stream dropped (%s); reconnecting", err)

    @callback
    def _tick(self, now: datetime) -> None:
        if self._latest_at is None:
            return
        if now - self._latest_at > STREAM_STALE_AFTER:
            if self.last_update_success:
                self.async_set_update_error(UpdateFailed("no stream frame for 30 s"))
            return
        if self._published_at is None or self._latest_at > self._published_at:
            self._published_at = self._latest_at
            self.async_set_updated_data(self._latest)  # type: ignore[arg-type]

    def start(self, entry: ConfigEntry) -> None:
        """Start the reader task and the publishing timer; both stop when the entry unloads."""
        entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._tick, self._interval, cancel_on_shutdown=True
            )
        )
        entry.async_create_background_task(self.hass, self._run(), name=f"{DOMAIN} stream")

    async def _run(self) -> None:
        try:
            await run_stream(
                self.client.stream_frames,
                self.handle_frame,
                on_disconnect=self.handle_disconnect,
            )
        except EnvoyStreamUnavailable as err:
            _LOGGER.info("Envoy stream unavailable (%s); using livedata for power", err)
        except EnvoyAuthError as err:
            _LOGGER.warning("Envoy stream refused the owner token: %s", err)
        self.unavailable = True
        self.settled.set()
        self.async_set_update_error(UpdateFailed("stream unavailable"))
