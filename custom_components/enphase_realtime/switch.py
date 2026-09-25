"""Charge-from-grid switch (spec 6.2)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EnphaseConfigEntry
from .const import CONF_COUNTRY
from .control import CloudControl
from .coordinator import CloudCoordinator, FastCoordinator
from .enlighten_client.battery import BatteryConfigClient
from .enlighten_client.models import charge_from_grid_available
from .entity import envoy_device

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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
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
