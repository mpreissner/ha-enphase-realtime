"""Binary sensors (spec 5.3, 5.4, 5.6)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EnphaseConfigEntry
from .coordinator import FastData, SlowData
from .entity import EnphaseEntity, by_serial, child_device, envoy_device

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EnphaseBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[Any], bool | None]
    attrs_fn: Callable[[Any], dict[str, Any] | None] | None = None


def _relay(d: FastData):
    if d.relay is None:
        raise KeyError("relay")
    return d.relay


def _schedule(d: FastData):
    if d.schedule is None:
        raise KeyError("schedule")
    return d.schedule


_BATTERY_FAST = (
    EnphaseBinarySensorDescription(
        key="charge_from_grid",
        name="Charge from grid in effect",
        value_fn=lambda d: _schedule(d).charge_from_grid_allowed,
    ),
)

_ENPOWER_FAST = (
    EnphaseBinarySensorDescription(
        key="grid_status",
        name="Grid status",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda d: _relay(d).grid_connected,
    ),
    EnphaseBinarySensorDescription(
        key="grid_outage",
        name="Grid outage",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: _relay(d).grid_outage,
    ),
)


def _communicating(value_fn: Callable[[SlowData], bool]) -> EnphaseBinarySensorDescription:
    return EnphaseBinarySensorDescription(
        key="communicating",
        name="Communicating",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=value_fn,
    )


def _inventory(d: SlowData):
    if d.inventory is None:
        raise KeyError("inventory")
    return d.inventory


def _battery(serial: str) -> list[EnphaseBinarySensorDescription]:
    def battery(d: SlowData):
        return by_serial(_inventory(d).batteries, serial)

    def dc_switch(d: SlowData) -> bool | None:
        off = battery(d).dc_switch_off
        return None if off is None else not off

    return [
        _communicating(lambda d: battery(d).communicating),
        EnphaseBinarySensorDescription(
            key="dc_switch",
            name="DC switch",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=dc_switch,
        ),
    ]


def _controller(serial: str) -> list[EnphaseBinarySensorDescription]:
    return [
        _communicating(lambda d: by_serial(_inventory(d).system_controllers, serial).communicating)
    ]


def _contact(contact_id: str, label: str) -> EnphaseBinarySensorDescription:
    return EnphaseBinarySensorDescription(
        key=f"dry_contact_{contact_id}",
        name=label,
        value_fn=lambda d: d.dry_contact_states[contact_id],
    )


_CLOUD = (
    EnphaseBinarySensorDescription(
        key="pending_cloud_change",
        name="Pending cloud change",
        value_fn=lambda s: s.has_pending_change,
        attrs_fn=lambda s: s.requested_config or None,
    ),
)


class EnphaseBinarySensor(EnphaseEntity[Any], BinarySensorEntity):
    entity_description: EnphaseBinarySensorDescription  # type: ignore[assignment]

    @property
    def is_on(self) -> bool | None:
        return self._value()[1]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs_fn = self.entity_description.attrs_fn
        if attrs_fn is None or self.coordinator.data is None:
            return None
        return attrs_fn(self.coordinator.data)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    rt = entry.runtime_data
    envoy = envoy_device(rt.serial, rt.firmware)
    entities: list[EnphaseBinarySensor] = []

    if rt.hardware.has_battery:
        entities += [EnphaseBinarySensor(rt.fast, d, envoy, rt.serial) for d in _BATTERY_FAST]
    if rt.hardware.has_enpower:
        entities += [EnphaseBinarySensor(rt.fast, d, envoy, rt.serial) for d in _ENPOWER_FAST]

    slow = rt.slow.data
    contacts_device = envoy
    if slow.inventory is not None:
        for battery in slow.inventory.batteries:
            device = child_device("IQ Battery", battery.serial, rt.serial)
            entities += [
                EnphaseBinarySensor(rt.slow, d, device, battery.serial)
                for d in _battery(battery.serial)
            ]
        for controller in slow.inventory.system_controllers:
            contacts_device = child_device("IQ System Controller", controller.serial, rt.serial)
            entities += [
                EnphaseBinarySensor(rt.slow, d, contacts_device, controller.serial)
                for d in _controller(controller.serial)
            ]
    for contact_id in slow.dry_contact_states:
        settings = slow.dry_contact_settings.get(contact_id)
        label = settings.load_name if settings and settings.load_name else contact_id
        entities.append(
            EnphaseBinarySensor(rt.slow, _contact(contact_id, label), contacts_device, rt.serial)
        )

    if rt.cloud is not None:
        entities += [EnphaseBinarySensor(rt.cloud, d, envoy, rt.serial) for d in _CLOUD]

    async_add_entities(entities)
