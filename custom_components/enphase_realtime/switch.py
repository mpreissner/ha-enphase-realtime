"""Charge-from-grid switch (spec 6.2) and the grid relay (spec 6.3)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EnphaseConfigEntry
from .confirm import RELAY_CONFIRM_TIMEOUT
from .const import (
    CONF_ALLOW_GRID_RELAY,
    CONF_COUNTRY,
    CONF_SITE_ID,
    DEFAULT_ALLOW_GRID_RELAY,
    DOMAIN,
)
from .control import CloudControl, ConfirmingControl
from .coordinator import CloudCoordinator, FastCoordinator, LiveCoordinator, LiveFeed
from .enlighten_client.battery import BatteryConfigClient
from .enlighten_client.errors import EnlightenAuthError, EnlightenError
from .enlighten_client.models import charge_from_grid_available
from .enlighten_client.session import EnlightenSession
from .entity import child_device, envoy_device
from .envoy_client.errors import EnvoyError
from .envoy_client.models import Relay

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
        serial: str,
        firmware: str,
        *,
        itc_disclaimer: bool,
    ) -> None:
        super().__init__(fast, cloud, CHARGE_FROM_GRID, envoy_device(serial, firmware), serial)
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
        # Keep the begin and end times from the last GET; the app sends them with every "on".
        settings = self._cloud.data
        body: dict[str, Any] = {"chargeFromGrid": True, "chargeFromGridScheduleEnabled": False}
        if settings is not None:
            for key, value in (
                ("chargeBeginTime", settings.charge_begin_time),
                ("chargeEndTime", settings.charge_end_time),
            ):
                if value is not None:
                    body[key] = value
        if self._itc:
            body["acceptedItcDisclaimer"] = True

        async def write(battery: BatteryConfigClient) -> None:
            if self._itc:
                await battery.accept_disclaimer("itc")
            await battery.update_battery_settings(body)

        await self._async_write(True, write)

    async def async_turn_off(self, **kwargs: Any) -> None:
        async def write(battery: BatteryConfigClient) -> None:
            await battery.update_battery_settings({"chargeFromGrid": False})

        await self._async_write(False, write)


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
    inventory = rt.slow.data.inventory
    controllers = inventory.system_controllers if inventory is not None else []
    device = (
        child_device("IQ System Controller", controllers[0].serial, rt.envoy_device_id)
        if controllers
        else envoy_device(rt.serial, rt.firmware)
    )
    return GridRelaySwitch(rt.live, rt.enlighten, entry.data[CONF_SITE_ID], device, rt.serial)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    relay = _grid_relay(entry)
    if relay is not None:
        async_add_entities([relay])

    rt = entry.runtime_data
    if rt.cloud is None or rt.fast is None:
        return
    site = rt.cloud.site
    if site is None:
        _LOGGER.info(
            "Enphase's site settings weren't available at setup, so it isn't known whether "
            "charging from the grid is allowed here; reload the integration to try again"
        )
        return
    if not charge_from_grid_available(site, rt.cloud.data):
        return
    # The ITC disclaimer is the US Investment Tax Credit. The user's confirmed country wins over
    # the registered one (spec 3.3).
    country = entry.options.get(CONF_COUNTRY) or site.country_code
    async_add_entities(
        [
            ChargeFromGridSwitch(
                rt.fast, rt.cloud, rt.serial, rt.firmware, itc_disclaimer=country == "US"
            )
        ]
    )
