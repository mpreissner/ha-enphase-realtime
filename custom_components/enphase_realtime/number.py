"""Battery shutdown level (`veryLowSoc`) number (spec 6.2). The backup reserve joins it once spike
S3 is settled."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EnphaseConfigEntry
from .control import CloudControl, secctrl
from .enlighten_client.battery import BatteryConfigClient
from .entity import envoy_device

PARALLEL_UPDATES = 1

# The range the cloud reported on the reference site, for when it leaves the limits out.
VERY_LOW_SOC_MIN = 5
VERY_LOW_SOC_MAX = 25


@dataclass(frozen=True, kw_only=True)
class EnphaseNumberDescription(NumberEntityDescription):
    value_fn: Callable[[Any], int | None]


VERY_LOW_SOC = EnphaseNumberDescription(
    key="very_low_soc",
    name="Battery shutdown level",
    icon="mdi:battery-alert-variant-outline",
    native_unit_of_measurement=PERCENTAGE,
    native_step=1,
    mode=NumberMode.BOX,
    value_fn=lambda d: secctrl(d).very_low_soc,
)


class VeryLowSocNumber(CloudControl[int], NumberEntity):
    @property
    def native_value(self) -> int | None:
        return self._shown()

    @property
    def native_min_value(self) -> float:
        settings = self._cloud.data
        limit = settings.very_low_soc_min if settings is not None else None
        return VERY_LOW_SOC_MIN if limit is None else limit

    @property
    def native_max_value(self) -> float:
        settings = self._cloud.data
        limit = settings.very_low_soc_max if settings is not None else None
        return VERY_LOW_SOC_MAX if limit is None else limit

    async def async_set_native_value(self, value: float) -> None:
        requested = round(value)

        async def write(battery: BatteryConfigClient) -> None:
            await battery.update_battery_settings({"veryLowSoc": requested})

        await self._async_write(requested, write)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    rt = entry.runtime_data
    if rt.cloud is None:
        return
    envoy = envoy_device(rt.serial, rt.firmware)
    async_add_entities([VeryLowSocNumber(rt.fast, rt.cloud, VERY_LOW_SOC, envoy, rt.serial)])
