"""Battery shutdown level (`veryLowSoc`) and backup reserve (`batteryBackupPercentage`) numbers
(spec 6.2). The backup reserve's local confirmation (`configured_backup_soc`) is still to be checked
on a live system (spike S3)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EnphaseConfigEntry
from .control import CloudControl
from .enlighten_client.battery import BatteryConfigClient
from .entity import envoy_device

PARALLEL_UPDATES = 1

# The range the cloud reported on the reference site, for when it leaves the limits out.
VERY_LOW_SOC_MIN = 5
VERY_LOW_SOC_MAX = 25
BACKUP_RESERVE_MIN = 5
BACKUP_RESERVE_MAX = 100

# In Full Backup the cloud pins the reserve at 100, so the number has nothing to set.
FULL_BACKUP = "backup_only"


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
    value_fn=lambda d: d.secctrl.very_low_soc,
)


BACKUP_RESERVE = EnphaseNumberDescription(
    key="backup_reserve",
    name="Backup reserve",
    icon="mdi:battery-lock",
    native_unit_of_measurement=PERCENTAGE,
    native_step=1,
    mode=NumberMode.BOX,
    value_fn=lambda d: d.secctrl.configured_backup_soc,
)


class CloudPercentNumber(CloudControl[int], NumberEntity):
    """A percentage written as one `batterySettings` field, with limits from the cloud."""

    _field: str
    _default_min: int
    _default_max: int

    @property
    def native_value(self) -> int | None:
        return self._shown()

    @property
    def native_min_value(self) -> float:
        return self._limit(self._limits()[0], self._default_min)

    @property
    def native_max_value(self) -> float:
        return self._limit(self._limits()[1], self._default_max)

    def _limits(self) -> tuple[int | None, int | None]:
        raise NotImplementedError

    @staticmethod
    def _limit(limit: int | None, default: int) -> int:
        return default if limit is None else limit

    async def async_set_native_value(self, value: float) -> None:
        requested = round(value)

        async def write(battery: BatteryConfigClient) -> None:
            await battery.update_battery_settings({self._field: requested})

        await self._async_write(requested, write)


class VeryLowSocNumber(CloudPercentNumber):
    _field = "veryLowSoc"
    _default_min = VERY_LOW_SOC_MIN
    _default_max = VERY_LOW_SOC_MAX

    def _limits(self) -> tuple[int | None, int | None]:
        settings = self._cloud.data
        if settings is None:
            return None, None
        return settings.very_low_soc_min, settings.very_low_soc_max


class BackupReserveNumber(CloudPercentNumber):
    _field = "batteryBackupPercentage"
    _default_min = BACKUP_RESERVE_MIN
    _default_max = BACKUP_RESERVE_MAX

    @property
    def available(self) -> bool:
        settings = self._cloud.data
        return super().available and settings is not None and settings.profile != FULL_BACKUP

    def _limits(self) -> tuple[int | None, int | None]:
        settings = self._cloud.data
        if settings is None:
            return None, None
        return settings.backup_percentage_min, settings.backup_percentage_max


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    rt = entry.runtime_data
    if rt.cloud is None or rt.fast is None:
        return
    envoy = envoy_device(rt.serial, rt.firmware)
    async_add_entities(
        [
            VeryLowSocNumber(rt.fast, rt.cloud, VERY_LOW_SOC, envoy, rt.serial),
            BackupReserveNumber(rt.fast, rt.cloud, BACKUP_RESERVE, envoy, rt.serial),
        ]
    )
