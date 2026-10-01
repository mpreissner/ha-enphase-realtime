"""Battery shutdown level (`veryLowSoc`) and reserve battery level (`batteryBackupPercentage`)
numbers (spec 6.2). The reserve's local confirmation is still to be checked live (spike S3).
Also battery maintenance's two levels (docs/specs/battery-maintenance.md) and the dry contacts'
cutoff and restore levels (docs/specs/dry-contacts.md)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EnphaseConfigEntry
from .const import DOMAIN, FULL_BACKUP
from .control import CloudControl, charge_from_grid_itc
from .dry_contact import DryContactControl, contact_device, dry_contact_controls
from .enlighten_client.battery import BatteryConfigClient
from .entity import controller_or_envoy, envoy_device
from .maintenance import MaintenanceSettings

PARALLEL_UPDATES = 1

# The range the cloud reported on the reference site, for when it leaves the limits out.
VERY_LOW_SOC_MIN = 5
VERY_LOW_SOC_MAX = 25
BACKUP_RESERVE_MIN = 5
BACKUP_RESERVE_MAX = 100


@dataclass(frozen=True, kw_only=True)
class EnphaseNumberDescription(NumberEntityDescription):
    value_fn: Callable[[Any], int | None]


VERY_LOW_SOC = EnphaseNumberDescription(
    key="battery_shutdown_level",
    name="Battery shutdown level",
    icon="mdi:battery-alert-variant-outline",
    native_unit_of_measurement=PERCENTAGE,
    native_step=1,
    mode=NumberMode.BOX,
    value_fn=lambda d: d.secctrl.very_low_soc,
)


BACKUP_RESERVE = EnphaseNumberDescription(
    key="reserve_battery_level",
    name="Reserve battery level",
    icon="mdi:battery-lock",
    native_unit_of_measurement=PERCENTAGE,
    native_step=1,
    # A slider, as in the core integration.
    mode=NumberMode.SLIDER,
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
        # In Full Backup the cloud pins the reserve at 100, so the number has nothing to set.
        settings = self._cloud.data
        return super().available and settings is not None and settings.profile != FULL_BACKUP

    def _limits(self) -> tuple[int | None, int | None]:
        settings = self._cloud.data
        if settings is None:
            return None, None
        return settings.backup_percentage_min, settings.backup_percentage_max


MAINTENANCE_START = NumberEntityDescription(
    key="maintenance_charge_start_level",
    name="Maintenance charge start level",
    icon="mdi:battery-arrow-down-outline",
    entity_category=EntityCategory.CONFIG,
    native_unit_of_measurement=PERCENTAGE,
    native_min_value=5,
    native_max_value=99,
    native_step=1,
    mode=NumberMode.BOX,
)

MAINTENANCE_STOP = NumberEntityDescription(
    key="maintenance_charge_stop_level",
    name="Maintenance charge stop level",
    icon="mdi:battery-arrow-up-outline",
    entity_category=EntityCategory.CONFIG,
    native_unit_of_measurement=PERCENTAGE,
    native_min_value=6,
    native_max_value=100,
    native_step=1,
    mode=NumberMode.BOX,
)


class MaintenanceLevelNumber(RestoreNumber):
    """One of battery maintenance's levels: the integration's own setting, restored across
    restarts. The start level must stay below the stop level."""

    _attr_has_entity_name = True

    def __init__(
        self,
        settings: MaintenanceSettings,
        description: NumberEntityDescription,
        serial: str,
        firmware: str,
    ) -> None:
        self.entity_description = description
        self._settings = settings
        self._is_start = description is MAINTENANCE_START
        self._attr_device_info = envoy_device(serial, firmware)
        self._attr_unique_id = f"{serial}_{description.key}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._store(round(last.native_value))

    @property
    def native_value(self) -> int:
        return self._settings.start if self._is_start else self._settings.stop

    async def async_set_native_value(self, value: float) -> None:
        level = round(value)
        start, stop = (
            (level, self._settings.stop) if self._is_start else (self._settings.start, level)
        )
        if start >= stop:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="maintenance_levels_invalid",
                translation_placeholders={"start": str(start), "stop": str(stop)},
            )
        self._store(level)
        self.async_write_ha_state()

    def _store(self, level: int) -> None:
        if self._is_start:
            self._settings.start = level
        else:
            self._settings.stop = level


# (Envoy field, name suffix, icon)
_CONTACT_LEVELS = (
    ("soc_low", "Cutoff battery level", "mdi:battery-arrow-down-outline"),
    ("soc_high", "Restore battery level", "mdi:battery-arrow-up-outline"),
)


def _contact_levels(contact_id: str) -> list[EnphaseNumberDescription]:
    return [
        EnphaseNumberDescription(
            key=f"dry_contact_{contact_id}_{field}",
            name=name,
            icon=icon,
            native_unit_of_measurement=PERCENTAGE,
            native_min_value=0,
            native_max_value=100,
            native_step=1,
            mode=NumberMode.BOX,
            entity_category=EntityCategory.CONFIG,
            value_fn=lambda d, f=field: _level(getattr(d.dry_contact_settings[contact_id], f)),
        )
        for field, name, icon in _CONTACT_LEVELS
    ]


def _level(value: float | None) -> int | None:
    return None if value is None else round(value)


class DryContactLevelNumber(DryContactControl[int], NumberEntity):
    """The contact's cutoff (`soc_low`) or restore (`soc_high`) battery level. The cutoff must
    stay below the restore level."""

    @property
    def native_value(self) -> int | None:
        return self._shown()

    async def async_set_native_value(self, value: float) -> None:
        self._check_allowed()
        level = round(value)
        field = self.entity_description.key.removeprefix(f"dry_contact_{self._contact_id}_")
        other = self.coordinator.dry_contact_setting(
            self._contact_id, "soc_high" if field == "soc_low" else "soc_low"
        )
        if other is not None:
            low, high = (level, other) if field == "soc_low" else (other, level)
            if low >= high:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="dry_contact_levels_invalid",
                    translation_placeholders={"low": str(_level(low)), "high": str(_level(high))},
                )

        async def write() -> None:
            # The Envoy reports the levels as floats, so they go back as floats.
            await self.coordinator.write_dry_contact_settings(
                self._contact_id, {field: float(level)}
            )

        await self._async_write(level, write)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    rt = entry.runtime_data
    if contacts := dry_contact_controls(entry):
        async_add_entities(
            DryContactLevelNumber(
                rt.slow, d, contact_device(hass, entry, contact_id), rt.serial, contact_id
            )
            for contact_id in contacts
            for d in _contact_levels(contact_id)
        )
    if rt.cloud is None or rt.fast is None:
        return
    envoy = envoy_device(rt.serial, rt.firmware)
    inventory = rt.slow.data.inventory
    controllers = inventory.system_controllers if inventory is not None else []
    # Where the core integration's reserve number is, so the entity ID matches.
    reserve_device = controller_or_envoy([c.serial for c in controllers], rt.envoy_device_id, envoy)
    async_add_entities(
        [
            VeryLowSocNumber(rt.fast, rt.cloud, VERY_LOW_SOC, envoy, rt.serial),
            BackupReserveNumber(rt.fast, rt.cloud, BACKUP_RESERVE, reserve_device, rt.serial),
        ]
    )
    # Battery maintenance writes charge from grid, so it exists where that switch does.
    if charge_from_grid_itc(entry) is not None:
        async_add_entities(
            [
                MaintenanceLevelNumber(rt.maintenance, description, rt.serial, rt.firmware)
                for description in (MAINTENANCE_START, MAINTENANCE_STOP)
            ]
        )
