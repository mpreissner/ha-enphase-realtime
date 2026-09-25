"""Base entity and device builders shared by every platform."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN
from .coordinator import LiveCoordinator

MANUFACTURER = "Enphase"


def envoy_device(serial: str, firmware: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        manufacturer=MANUFACTURER,
        model="Envoy",
        name=f"Envoy {serial}",
        serial_number=serial,
        sw_version=firmware,
    )


# Model, and the device name's prefix. The prefix is the core integration's, so entity IDs,
# which Home Assistant derives from the device name, match its (docs/MIGRATION.md).
IQ_BATTERY = ("IQ Battery", "Encharge")
IQ_SYSTEM_CONTROLLER = ("IQ System Controller", "Enpower")
IQ_MICROINVERTER = ("IQ Microinverter", "Inverter")


def child_device(kind: tuple[str, str], serial: str, envoy_device_id: str) -> DeviceInfo:
    """System Controller, IQ Battery or microinverter, all hanging off the Envoy."""
    model, prefix = kind
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        manufacturer=MANUFACTURER,
        model=model,
        name=f"{prefix} {serial}",
        serial_number=serial,
        via_device_id=envoy_device_id,
    )


def controller_or_envoy(
    controller_serials: list[str], envoy_device_id: str, envoy: DeviceInfo
) -> DeviceInfo:
    """Where the core integration puts grid and reserve entities: the System Controller, or the
    Envoy on a site without one."""
    if not controller_serials:
        return envoy
    return child_device(IQ_SYSTEM_CONTROLLER, controller_serials[0], envoy_device_id)


class ValueDescription(Protocol):
    """What each platform's entity description adds: `value_fn` reads the state from the
    coordinator's data. Returning `None` means unknown; raising `LookupError` (a device or
    contact that has gone) means unavailable."""

    key: str
    value_fn: Callable[[Any], Any]


class EnphaseEntity[DataT](CoordinatorEntity[DataUpdateCoordinator[DataT]]):
    _attr_has_entity_name = True
    entity_description: ValueDescription  # type: ignore[assignment]

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[DataT],
        description: Any,
        device: DeviceInfo,
        unique_prefix: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_device_info = device
        self._attr_unique_id = f"{unique_prefix}_{description.key}"

    def _value(self) -> tuple[bool, Any]:
        """(available, value)."""
        if self.coordinator.data is None:
            return False, None
        try:
            return True, self.entity_description.value_fn(self.coordinator.data)
        except LookupError:
            return False, None

    @property
    def available(self) -> bool:
        if isinstance(self.coordinator, LiveCoordinator):
            # Rides out a short run of failed polls (spec 3.1).
            ok = self.coordinator.entities_available
        else:
            ok = super().available
        return ok and self._value()[0]


def by_serial[T](items: list[T], serial: str) -> T:
    """The item with this serial, or `KeyError` so the entity goes unavailable."""
    for item in items:
        if getattr(item, "serial", None) == serial:
            return item
    raise KeyError(serial)
