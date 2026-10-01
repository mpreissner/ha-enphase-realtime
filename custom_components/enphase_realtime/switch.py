"""Charge-from-grid switch (spec 6.2), battery maintenance (docs/specs/battery-maintenance.md),
the grid relay (spec 6.3) and the dry contacts (docs/specs/dry-contacts.md)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import EnphaseConfigEntry
from .confirm import RELAY_CONFIRM_TIMEOUT
from .const import (
    CONF_ALLOW_GRID_RELAY,
    CONF_SITE_ID,
    DEFAULT_ALLOW_GRID_RELAY,
    DOMAIN,
    FULL_BACKUP,
)
from .control import (
    CloudControl,
    ConfirmingControl,
    charge_from_grid_itc,
    write_charge_from_grid,
)
from .coordinator import (
    CloudCoordinator,
    FastCoordinator,
    FastData,
    LiveCoordinator,
    LiveFeed,
    SlowCoordinator,
)
from .dry_contact import DryContactControl, contact_device, dry_contact_controls
from .enlighten_client.battery import BatteryConfigClient
from .enlighten_client.errors import EnlightenAuthError, EnlightenError
from .enlighten_client.session import EnlightenSession
from .entity import EnphaseEntity, controller_or_envoy, envoy_device
from .envoy_client.errors import EnvoyError
from .envoy_client.models import Relay
from .maintenance import (
    STUCK_AFTER,
    WRITE_BACKOFF,
    Action,
    Maintenance,
    MaintenanceSettings,
    Observation,
    State,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EnphaseSwitchDescription(SwitchEntityDescription):
    value_fn: Callable[[Any], bool | None]


CHARGE_FROM_GRID = EnphaseSwitchDescription(
    key="allow_charge_from_grid",
    name="Charge from grid",
    icon="mdi:transmission-tower-import",
    value_fn=lambda d: d.schedule.charge_from_grid_allowed,
)


def _clock(minutes: int | None) -> str | None:
    """Minutes after midnight, in the site's time zone, as HH:MM."""
    return None if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"


class ChargeFromGridSwitch(CloudControl[bool], SwitchEntity):
    def __init__(
        self,
        fast: FastCoordinator,
        cloud: CloudCoordinator,
        device: DeviceInfo,
        serial: str,
        *,
        itc_disclaimer: bool,
    ) -> None:
        super().__init__(fast, cloud, CHARGE_FROM_GRID, device, serial)
        self._itc = itc_disclaimer

    @property
    def is_on(self) -> bool | None:
        return self._shown()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The schedule is read-only in v1 (spec 6.2)."""
        attrs = super().extra_state_attributes or {}
        settings = self._cloud.data
        if settings is not None:
            attrs |= {
                "schedule_enabled": settings.charge_from_grid_schedule_enabled,
                "charge_begin_time": _clock(settings.charge_begin_time),
                "charge_end_time": _clock(settings.charge_end_time),
            }
        return attrs or None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        async def write(battery: BatteryConfigClient) -> None:
            await write_charge_from_grid(battery, on, self._cloud.data, itc_disclaimer=self._itc)

        await self._async_write(on, write)


MAINTENANCE = EnphaseSwitchDescription(
    key="battery_maintenance",
    name="Battery maintenance",
    icon="mdi:battery-sync",
    entity_category=EntityCategory.CONFIG,
    # The switch is the integration's own setting, restored rather than read from the Envoy.
    value_fn=lambda d: None,
)


class MaintenanceSwitch(EnphaseEntity[FastData], SwitchEntity, RestoreEntity):
    """Turns charge from grid on at the start level and off at the stop level, in Full Backup
    with no PV (docs/specs/battery-maintenance.md). It runs the state machine on every fast
    poll and carries out its writes and repair issue."""

    coordinator: FastCoordinator

    def __init__(
        self,
        fast: FastCoordinator,
        cloud: CloudCoordinator,
        live: LiveCoordinator,
        settings: MaintenanceSettings,
        serial: str,
        firmware: str,
        *,
        itc_disclaimer: bool,
    ) -> None:
        super().__init__(fast, MAINTENANCE, envoy_device(serial, firmware), serial)
        self._cloud = cloud
        self._live = live
        self._settings = settings
        self._itc = itc_disclaimer
        self._machine = Maintenance(time.monotonic())
        self._attr_is_on = False
        self._issue_id = f"maintenance_charge_stuck_{cloud.config_entry.entry_id}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_is_on = last.state == STATE_ON
            owned = last.attributes.get("owned") is True
            self._machine = Maintenance(time.monotonic(), owned=owned)
        self.async_on_remove(self._delete_issue)
        # The coordinators have data by now, so the status is right before the next poll.
        self._tick()

    @property
    def available(self) -> bool:
        """A setting of the integration's own, so it can be changed whatever the Envoy does."""
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"status": self._machine.state.value, "owned": self._machine.owned}

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self._tick()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self._tick()
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._tick()
        super()._handle_coordinator_update()

    def _observe(self) -> Observation | None:
        """None when the Envoy data isn't there to decide on (spec 4.1)."""
        fast, live = self.coordinator.data, self._live.data
        if (
            fast is None
            or live is None
            or not self.coordinator.last_update_success
            or not self._live.entities_available
        ):
            return None
        settings = self._cloud.data if self._cloud.last_update_success else None
        livedata = live.livedata
        return Observation(
            enabled=bool(self._attr_is_on),
            start=self._settings.start,
            stop=self._settings.stop,
            full_backup=None if settings is None else settings.profile == FULL_BACKUP,
            soc=fast.secctrl.soc,
            allowed=fast.schedule.charge_from_grid_allowed,
            battery_w=None if livedata.storage is None else livedata.storage.power,
            pv_w=None if livedata.pv is None else livedata.pv.power,
            on_grid=live.relay is None or live.relay.grid_connected,
        )

    @callback
    def _tick(self) -> None:
        obs = self._observe()
        if obs is None:
            return
        before = self._machine.state
        for action in self._machine.update(time.monotonic(), obs):
            if action is Action.RAISE_ISSUE:
                self._raise_issue(obs)
            elif action is Action.CLEAR_ISSUE:
                _LOGGER.info("%s: the battery is charging again", self.entity_id)
                self._delete_issue()
            else:
                self.hass.async_create_task(self._async_write(action, obs))
        if self._machine.state is not before:
            _LOGGER.debug("%s: %s -> %s", self.entity_id, before.value, self._machine.state.value)

    async def _async_write(self, action: Action, obs: Observation) -> None:
        pending = self._machine.pending
        on = action is Action.TURN_ON
        if pending is not None and pending[1] is State.RETRYING:
            _LOGGER.warning(
                "%s: charge from grid is on but the battery (%s%%) hasn't charged for %d min; "
                "turning it off and on again",
                self.entity_id,
                obs.soc,
                STUCK_AFTER // 60,
            )
        else:
            _LOGGER.info(
                "%s: battery at %s%% (start %s%%, stop %s%%); turning charge from grid %s",
                self.entity_id,
                obs.soc,
                obs.start,
                obs.stop,
                "on" if on else "off",
            )
        ok = False
        try:
            await write_charge_from_grid(
                self._cloud.battery, on, self._cloud.data, itc_disclaimer=self._itc
            )
        except EnlightenAuthError:
            self._cloud.config_entry.async_start_reauth(self.hass)
            _LOGGER.warning(
                "%s: Enphase rejected the login, so charge from grid wasn't turned %s",
                self.entity_id,
                "on" if on else "off",
            )
        except EnlightenError as err:
            _LOGGER.warning(
                "%s: couldn't turn charge from grid %s (%s); trying again in %d min",
                self.entity_id,
                "on" if on else "off",
                err,
                WRITE_BACKOFF // 60,
            )
        else:
            ok = True
        finally:
            # Always settle the write, or an unexpected error would block maintenance for good.
            self._machine.done(time.monotonic(), ok)
            self.async_write_ha_state()
        if ok:
            await self._cloud.async_request_refresh()

    def _raise_issue(self, obs: Observation) -> None:
        mode = self.coordinator.data.schedule.mode if self.coordinator.data else None
        _LOGGER.warning(
            "%s: charge from grid is on but the battery (%s%%) still isn't charging after a "
            "retry; see Repairs",
            self.entity_id,
            obs.soc,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="maintenance_charge_stuck",
            translation_placeholders={
                "soc": str(obs.soc),
                "mode": mode or "unknown",
                "minutes": str(int(STUCK_AFTER // 60)),
            },
        )

    @callback
    def _delete_issue(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)


def _relay(d: LiveFeed) -> Relay:
    if d.relay is None:
        raise KeyError("relay")
    return d.relay


GRID_ENABLED = EnphaseSwitchDescription(
    key="grid_enabled",
    name="Grid enabled",
    icon="mdi:transmission-tower",
    # What the relay has been told, as the core integration's `enpower_grid_enabled` shows it.
    value_fn=lambda d: _relay(d).admin_state == "closed",
)


class GridRelaySwitch(ConfirmingControl[LiveFeed, bool], SwitchEntity):
    """On: the System Controller keeps the house on grid. Off: it opens the main relay and the
    house runs from the battery (spec 6.3)."""

    coordinator: LiveCoordinator

    def __init__(
        self,
        live: LiveCoordinator,
        enlighten: EnlightenSession,
        site_id: int,
        device: DeviceInfo,
        serial: str,
    ) -> None:
        super().__init__(live, GRID_ENABLED, device, serial, RELAY_CONFIRM_TIMEOUT)
        self._enlighten = enlighten
        self._site_id = site_id

    @property
    def is_on(self) -> bool | None:
        return self._shown()

    def _local(self) -> bool | None:
        """Confirmed only once the relay has actually moved, not just been told to: both
        `mains_admin_state` and `mains_oper_state` must match."""
        available, closed = self._value()
        if not available:
            return None
        relay = _relay(self.coordinator.data)
        return closed if relay.oper_state == relay.admin_state else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, closed: bool) -> None:
        if not self._confirm.pending and self._value()[1] == closed:
            return
        await self._async_check_cloud()
        action = "close (go on grid)" if closed else "open (go off grid)"
        _LOGGER.info("%s: asking the System Controller to %s", self.entity_id, action)
        try:
            await self.coordinator.client.set_relay(closed)
        except EnvoyError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="relay_write_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self._start_confirm(closed)
        await self.coordinator.async_request_refresh()

    async def _async_check_cloud(self) -> None:
        """The same pre-check the Enphase app makes. Any flag that is set, or no answer, refuses
        the write."""
        try:
            check = await self._enlighten.grid_control_check(self._site_id)
        except EnlightenAuthError as err:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="write_auth_failed"
            ) from err
        except EnlightenError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="grid_check_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if check.blockers:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="grid_control_blocked",
                translation_placeholders={"reasons": ", ".join(check.blockers)},
            )


def _grid_relay(entry: EnphaseConfigEntry) -> GridRelaySwitch | None:
    rt = entry.runtime_data
    if not rt.hardware.has_enpower or not entry.options.get(
        CONF_ALLOW_GRID_RELAY, DEFAULT_ALLOW_GRID_RELAY
    ):
        return None
    return GridRelaySwitch(
        rt.live, rt.enlighten, entry.data[CONF_SITE_ID], _controller_or_envoy(entry), rt.serial
    )


def _controller_or_envoy(entry: EnphaseConfigEntry) -> DeviceInfo:
    """The grid relay's device, and charge from grid's, as in the core integration."""
    rt = entry.runtime_data
    inventory = rt.slow.data.inventory
    controllers = inventory.system_controllers if inventory is not None else []
    return controller_or_envoy(
        [c.serial for c in controllers], rt.envoy_device_id, envoy_device(rt.serial, rt.firmware)
    )


class DryContactSwitch(DryContactControl[bool], SwitchEntity):
    """On: the contact's relay is closed (the core integration's `relay_status`)."""

    @property
    def is_on(self) -> bool | None:
        return self._shown()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, closed: bool) -> None:
        async def write() -> None:
            await self.coordinator.client.set_dry_contact(self._contact_id, closed)

        await self._async_write(closed, write)


def _dry_contact_switch(
    slow: SlowCoordinator, contact_id: str, device: DeviceInfo, serial: str
) -> DryContactSwitch:
    description = EnphaseSwitchDescription(
        key=f"dry_contact_{contact_id}",
        # The device's name, as in the core integration.
        name=None,
        icon="mdi:electric-switch",
        value_fn=lambda d: d.dry_contact_states[contact_id],
    )
    return DryContactSwitch(slow, description, device, serial, contact_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    relay = _grid_relay(entry)
    if relay is not None:
        async_add_entities([relay])

    rt = entry.runtime_data
    if contacts := dry_contact_controls(entry):
        async_add_entities(
            [
                _dry_contact_switch(rt.slow, c, contact_device(hass, entry, c), rt.serial)
                for c in contacts
            ]
        )

    if rt.cloud is None or rt.fast is None:
        return
    if rt.cloud.site is None:
        _LOGGER.info(
            "Enphase's site settings weren't available at setup, so it isn't known whether "
            "charging from the grid is allowed here; reload the integration to try again"
        )
        return
    itc = charge_from_grid_itc(entry)
    if itc is None:
        return
    async_add_entities(
        [
            ChargeFromGridSwitch(
                rt.fast, rt.cloud, _controller_or_envoy(entry), rt.serial, itc_disclaimer=itc
            ),
            MaintenanceSwitch(
                rt.fast,
                rt.cloud,
                rt.live,
                rt.maintenance,
                rt.serial,
                rt.firmware,
                itc_disclaimer=itc,
            ),
        ]
    )
