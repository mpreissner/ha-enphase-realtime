"""The five coordinators of spec 3.1: stream (push), live, fast, slow and cloud."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .confirm import DRY_CONTACT_CONFIRM_TIMEOUT
from .const import (
    CONF_FIRMWARE,
    CONF_PHASE_LAYOUT,
    DOMAIN,
    DRY_CONTACT_POLL,
    LIVE_FAILURES_BEFORE_UNAVAILABLE,
    LIVE_STAMP_LOG_AFTER,
    SC_STREAM_ENABLE_COOLDOWN,
    SLOW_INTERVAL,
    STREAM_STALE_AFTER,
    STREAM_STALE_CHECK,
    STREAM_THROTTLE_SLACK,
)
from .credentials import TokenKeeper, translate_errors
from .enlighten_client.battery import BatteryConfigClient
from .enlighten_client.models import BatterySettings, SiteSettings
from .envoy_client.errors import (
    EnvoyAuthError,
    EnvoyConnectionError,
    EnvoyError,
    EnvoyParseError,
    EnvoyStreamUnavailable,
)
from .envoy_client.local import EnvoyClient
from .envoy_client.models import (
    BatteryPower,
    CtMeter,
    DryContactSettings,
    ExportLimit,
    Inventory,
    Inverter,
    InverterDetail,
    LifetimeEnergy,
    LiveData,
    Meter,
    PcsSettings,
    PhaseLayout,
    ProductionReport,
    Relay,
    Schedule,
    SecCtrl,
    StreamFrame,
    detect_phase_layout,
    parse_ct_meters,
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


# --- Live ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiveFeed:
    livedata: LiveData
    relay: Relay | None


class LiveCoordinator(DataUpdateCoordinator[LiveFeed]):
    """The 1 s poll (spec 3.1). A short run of failed polls keeps the last good values: entities
    ask `entities_available`, not `last_update_success`, which still records every failure."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EnvoyClient,
        hardware: Hardware,
        interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} live",
            update_interval=interval,
            always_update=False,
        )
        self.client = client
        self._hw = hardware
        self.failed_polls = 0
        self._enable_sent_at: datetime | None = None
        # The last `meters.last_update`, when it was first seen (monotonic), and whether it has
        # been logged as held.
        self._stamp: datetime | None = None
        self._stamp_since = 0.0
        self._stamp_logged = False

    @property
    def entities_available(self) -> bool:
        return self.data is not None and self.failed_polls < LIVE_FAILURES_BEFORE_UNAVAILABLE

    async def _async_update_data(self) -> LiveFeed:
        c = self.client

        async def none() -> None:
            return None

        with translate_errors("Envoy"):
            livedata, relay = await asyncio.gather(
                c.livedata(), c.relay() if self._hw.has_enpower else none()
            )
        if livedata.sc_stream == "disabled":
            await self._enable_sc_stream()
        self._log_stamp(livedata)
        return LiveFeed(livedata, relay)

    def _log_stamp(self, livedata: LiveData) -> None:
        """Log `meters.last_update` held across polls: the Envoy's values may be stale then, and
        a snapshot identical to the last one reaches no listener (enphase-overhead.md, 6)."""
        now = time.monotonic()
        if livedata.last_update != self._stamp:
            if self._stamp_logged:
                _LOGGER.debug(
                    "livedata meters.last_update advanced to %s after %.1f s unchanged",
                    livedata.last_update,
                    now - self._stamp_since,
                )
            self._stamp, self._stamp_since, self._stamp_logged = livedata.last_update, now, False
        elif not self._stamp_logged and now - self._stamp_since >= LIVE_STAMP_LOG_AFTER:
            load = livedata.load.power if livedata.load is not None else None
            _LOGGER.debug(
                "livedata meters.last_update unchanged at %s for %.1f s (load %s W)",
                livedata.last_update,
                now - self._stamp_since,
                load,
            )
            self._stamp_logged = True

    async def _enable_sc_stream(self) -> None:
        now = dt_util.utcnow()
        if (
            self._enable_sent_at is not None
            and now - self._enable_sent_at < SC_STREAM_ENABLE_COOLDOWN
        ):
            return
        self._enable_sent_at = now
        _LOGGER.debug("livedata reports sc_stream disabled; asking the Envoy to enable it")
        try:
            await self.client.enable_livedata_stream()
        except EnvoyError as err:
            # The values still arrive, only staler; the poll itself succeeded.
            _LOGGER.debug("Enabling sc_stream failed: %s", err)

    @callback
    def _async_refresh_finished(self) -> None:
        if self.last_update_success:
            self.failed_polls = 0
            return
        self.failed_polls += 1
        # The coordinator tells listeners only about the first failure in a row; entities kept
        # through that one need telling again when they go.
        if self.failed_polls == LIVE_FAILURES_BEFORE_UNAVAILABLE:
            self.async_update_listeners()


# --- Fast ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FastData:
    secctrl: SecCtrl
    schedule: Schedule


class FastCoordinator(DataUpdateCoordinator[FastData]):
    """Battery state that changes slowly. Only created on sites with a battery."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EnvoyClient,
        interval: timedelta,
    ) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} fast", update_interval=interval
        )
        self.client = client

    async def _async_update_data(self) -> FastData:
        with translate_errors("Envoy"):
            secctrl, schedule = await asyncio.gather(self.client.secctrl(), self.client.schedule())
        return FastData(secctrl, schedule)


# --- Slow ---------------------------------------------------------------------------------------


async def _optional[T](read: Awaitable[T]) -> T | None:
    """A display-only read that mustn't fail the poll: older firmware may not serve it."""
    try:
        return await read
    except (EnvoyConnectionError, EnvoyParseError) as err:
        _LOGGER.debug("Skipping an optional read: %s", err)
        return None


def _parsed[T](parse: Callable[[], T]) -> T | None:
    """The same for a payload parsed after its read."""
    try:
        return parse()
    except EnvoyParseError as err:
        _LOGGER.debug("Skipping an optional payload: %s", err)
        return None


@dataclass(frozen=True, slots=True)
class SlowData:
    meters: list[Meter]
    energy: LifetimeEnergy
    inventory: Inventory | None
    dry_contact_settings: dict[str, DryContactSettings]
    dry_contact_states: dict[str, bool]
    inverters: list[Inverter]
    # Installer settings, read for display only. None where the Envoy doesn't serve them.
    export_limit: ExportLimit | None = None
    pcs: PcsSettings | None = None
    # What the core integration's entities read (docs/specs/core-entity-parity.md). Each CT by
    # measurement type, and three reads that are None where the Envoy doesn't serve them.
    ct_meters: dict[str, CtMeter] = field(default_factory=dict)
    production: ProductionReport | None = None
    battery_power: dict[str, BatteryPower] | None = None
    inverter_details: dict[str, InverterDetail] | None = None


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
        self._firmware = entry.data.get(CONF_FIRMWARE) or ""
        # Per contact, settings fields written but not yet reported back: field -> (value, when
        # written). Later writes lay them over the Envoy's object (docs/specs/dry-contacts.md 5).
        self._written: dict[str, dict[str, tuple[Any, float]]] = {}
        self._contact_poll_busy = False

    async def refresh_dry_contacts(self) -> None:
        """Re-read just the dry contacts, for a control waiting on a write. Raises the client's
        `EnvoyError`; the next slow poll deals with a lasting failure."""
        contacts, states = await asyncio.gather(
            self.client.dry_contact_settings(), self.client.dry_contact_states()
        )
        self._forget_reported(contacts)
        self._push(replace(self.data, dry_contact_settings=contacts, dry_contact_states=states))

    def start_contact_poll(self, entry: ConfigEntry) -> None:
        """Re-read the contacts' states every 2 s until the entry unloads."""
        entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._poll_contact_states, DRY_CONTACT_POLL, cancel_on_shutdown=True
            )
        )

    async def _poll_contact_states(self, _now: datetime) -> None:
        # A slow Envoy mustn't pile reads up: skip the tick while the last read is still out.
        if self._contact_poll_busy or self.data is None:
            return
        self._contact_poll_busy = True
        try:
            states = await self.client.dry_contact_states()
        except EnvoyError as err:
            # Keep the last states; the 60 s poll deals with a lasting failure.
            _LOGGER.debug("Skipping a dry-contact poll: %s", err)
            return
        finally:
            self._contact_poll_busy = False
        self._push(replace(self.data, dry_contact_states=states))

    @callback
    def _push(self, data: SlowData) -> None:
        """Publish re-read dry contacts. Not `async_set_updated_data`, which would push the next
        60 s poll back each time."""
        self._log_contact_changes(data.dry_contact_states)
        if data != self.data:
            self.data = data
            self.async_update_listeners()

    def _log_contact_changes(self, states: dict[str, bool]) -> None:
        old = self.data.dry_contact_states if self.data is not None else {}
        for contact_id, closed in states.items():
            if contact_id in old and old[contact_id] != closed:
                _LOGGER.debug(
                    "Dry contact %s now reports %s", contact_id, "closed" if closed else "open"
                )

    async def write_dry_contact_settings(self, contact_id: str, changes: dict[str, Any]) -> None:
        """Send the contact's full object with `changes` (Envoy field names) replaced."""
        # A write that timed out mustn't ride along before the next poll prunes it.
        self._forget_reported(self.data.dry_contact_settings)
        written = self._written.setdefault(contact_id, {})
        body = {
            **self.data.dry_contact_settings[contact_id].raw,
            **{key: value for key, (value, _) in written.items()},
            **changes,
        }
        await self.client.set_dry_contact_settings(body)
        now = time.monotonic()
        written.update({key: (value, now) for key, value in changes.items()})

    def dry_contact_setting(self, contact_id: str, key: str) -> Any:
        """A settings field as the next write would send it: written, or else reported."""
        written = self._written.get(contact_id, {})
        if key in written:
            return written[key][0]
        return self.data.dry_contact_settings[contact_id].raw.get(key)

    def _forget_reported(self, contacts: dict[str, DryContactSettings]) -> None:
        """Drop written fields the Envoy now reports, or that it never took up."""
        expired = time.monotonic() - DRY_CONTACT_CONFIRM_TIMEOUT.total_seconds()
        for contact_id, written in self._written.items():
            raw = contacts[contact_id].raw if contact_id in contacts else {}
            for key in [
                key
                for key, (value, when) in written.items()
                if raw.get(key) == value or when < expired
            ]:
                del written[key]

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
                export_limit,
                pcs,
                report,
                battery_power,
                inverter_details,
            ) = await asyncio.gather(
                c.meters(),
                c.meter_readings(),
                c.meter_reports(),
                c.inventory() if hw.has_battery or hw.has_enpower else none(),
                c.dry_contact_settings() if hw.has_enpower else empty(),
                c.dry_contact_states() if hw.has_enpower else empty(),
                c.inverters(),
                _optional(c.export_limit()),
                _optional(c.pcs_settings()),
                _optional(c.production_report()),
                _optional(c.battery_power()) if hw.has_battery else none(),
                _optional(c.inverter_details()),
            )
            energy = LifetimeEnergy.from_payloads(meters, readings, reports)
        firmware = self._firmware
        self._check_layout(meters)
        self._forget_reported(contacts)
        self._log_contact_changes(states)
        return SlowData(
            meters,
            energy,
            inventory,
            contacts,
            states,
            inverters,
            export_limit,
            pcs,
            ct_meters=_parsed(lambda: parse_ct_meters(meters, readings, firmware)) or {},
            production=(
                _parsed(lambda: ProductionReport.from_payload(report, meters, firmware))
                if report is not None
                else None
            ),
            battery_power=battery_power,
            inverter_details=inverter_details,
        )

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
    """Polls `batterySettings`. `siteSettings` (region and feature flags, spec 3.3) is read once,
    on the first update that reaches the cloud."""

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
        self.site: SiteSettings | None = None

    async def _async_update_data(self) -> BatterySettings:
        with translate_errors("Enlighten"):
            if self.site is None:
                self.site = await self.battery.site_settings()
            return await self.battery.battery_settings()


# --- Stream -------------------------------------------------------------------------------------


class StreamCoordinator(DataUpdateCoordinator[StreamFrame]):
    """Push, not poll. A background task hands over each frame. With `interval` 0 every frame is
    published; otherwise a frame is published only if `interval` has passed since the last one,
    and the frames in between are dropped (spec 3.2)."""

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
        now = dt_util.utcnow()
        self._latest = frame
        self._latest_at = now
        if (
            not self.last_update_success
            or self._published_at is None
            or now - self._published_at >= self._interval - STREAM_THROTTLE_SLACK
        ):
            self._published_at = now
            self.async_set_updated_data(frame)
        self.settled.set()

    @callback
    def handle_disconnect(self, err: EnvoyError) -> None:
        _LOGGER.debug("Envoy stream dropped (%s); reconnecting", err)

    @callback
    def _check_stale(self, now: datetime) -> None:
        if self._latest_at is None or not self.last_update_success:
            return
        if now - self._latest_at > STREAM_STALE_AFTER:
            self.async_set_update_error(UpdateFailed("no stream frame for 30 s"))

    def start(self, entry: ConfigEntry) -> None:
        """Start the reader task and the staleness check; both stop when the entry unloads."""
        entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._check_stale, STREAM_STALE_CHECK, cancel_on_shutdown=True
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
