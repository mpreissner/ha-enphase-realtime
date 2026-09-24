"""Sensors (spec 5.1 to 5.6)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import EnphaseConfigEntry
from .const import BATTERY_TEMPERATURE_UNIT, ENPOWER_TEMPERATURE_UNIT, PHASE_NAMES
from .coordinator import FastData, SlowData
from .entity import EnphaseEntity, by_serial, child_device, envoy_device
from .envoy_client.models import LivePower, PhaseLayout, StreamFrame, StreamMeter

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EnphaseSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Any], Any]


def _power(
    key: str, name: str | None, value_fn: Callable[[Any], Any], **kw: Any
) -> EnphaseSensorDescription:
    return EnphaseSensorDescription(
        key=key,
        name=name,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=value_fn,
        **kw,
    )


def _k(phase: str) -> str:
    """`ph-a` → `ph_a` for unique IDs (spec 3.3)."""
    return phase.replace("-", "_")


def _required[T](value: T | None, what: str) -> T:
    """Missing sections make the entity unavailable rather than unknown."""
    if value is None:
        raise KeyError(what)
    return value


# --- Stream (5.1) -------------------------------------------------------------------------------

# Frame attribute, unique-ID key, name.
_STREAM_METERS = (
    ("production", "production", "Production"),
    ("total_consumption", "consumption", "Consumption"),
    ("net_consumption", "net", "Net"),
)


def _stream_meter(attr: str) -> Callable[[StreamFrame], StreamMeter]:
    return lambda frame: _required(getattr(frame, attr), attr)


def _stream_sensors(layout: PhaseLayout) -> list[EnphaseSensorDescription]:
    out: list[EnphaseSensorDescription] = []
    for attr, key, name in _STREAM_METERS:
        meter = _stream_meter(attr)
        out.append(_power(f"{key}_power", f"{name} power", lambda f, m=meter: m(f).power))
        for ph in layout.phases:
            out.append(
                _power(
                    f"{key}_power_{_k(ph)}",
                    f"{name} power {PHASE_NAMES[ph]}",
                    lambda f, m=meter, ph=ph: m(f).phases[ph].power,
                )
            )
            out.append(
                EnphaseSensorDescription(
                    key=f"{key}_current_{_k(ph)}",
                    name=f"{name} current {PHASE_NAMES[ph]}",
                    device_class=SensorDeviceClass.CURRENT,
                    state_class=SensorStateClass.MEASUREMENT,
                    native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
                    suggested_display_precision=2,
                    entity_registry_enabled_default=False,
                    value_fn=lambda f, m=meter, ph=ph: m(f).phases[ph].current,
                )
            )
            out.append(
                EnphaseSensorDescription(
                    key=f"{key}_power_factor_{_k(ph)}",
                    name=f"{name} power factor {PHASE_NAMES[ph]}",
                    device_class=SensorDeviceClass.POWER_FACTOR,
                    state_class=SensorStateClass.MEASUREMENT,
                    suggested_display_precision=2,
                    entity_registry_enabled_default=False,
                    value_fn=lambda f, m=meter, ph=ph: m(f).phases[ph].power_factor,
                )
            )

    # Voltage and frequency are the same at every CT on a leg; read them off net-consumption.
    net = _stream_meter("net_consumption")
    # A single-phase site gets unsuffixed readings from its one phase.
    voltage_phases = layout.phases or ("ph-a",)
    for ph in voltage_phases:
        suffix = f" {PHASE_NAMES[ph]}" if layout.phases else ""
        key_suffix = f"_{_k(ph)}" if layout.phases else ""
        out.append(
            EnphaseSensorDescription(
                key=f"voltage{key_suffix}",
                name=f"Voltage{suffix}",
                device_class=SensorDeviceClass.VOLTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                native_unit_of_measurement=UnitOfElectricPotential.VOLT,
                suggested_display_precision=1,
                entity_registry_enabled_default=False,
                value_fn=lambda f, ph=ph: net(f).phases[ph].voltage,
            )
        )
    out.append(
        EnphaseSensorDescription(
            key="frequency",
            name="Frequency",
            device_class=SensorDeviceClass.FREQUENCY,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfFrequency.HERTZ,
            suggested_display_precision=2,
            entity_registry_enabled_default=False,
            value_fn=lambda f: net(f).phases["ph-a"].frequency,
        )
    )
    if layout is PhaseLayout.SPLIT:
        out.append(
            EnphaseSensorDescription(
                key="voltage_l1_l2",
                name="Voltage L1-L2",
                device_class=SensorDeviceClass.VOLTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                native_unit_of_measurement=UnitOfElectricPotential.VOLT,
                suggested_display_precision=1,
                entity_registry_enabled_default=False,
                value_fn=lambda f: f.split_phase_voltage(),
            )
        )
    return out


# --- Fast (5.1, 5.3) ----------------------------------------------------------------------------

# LiveData attribute, unique-ID key, name.
_LIVE_METERS = (
    ("grid", "grid", "Grid"),
    ("load", "load", "Load"),
    ("pv", "pv", "PV"),
)


def _live(attr: str) -> Callable[[FastData], LivePower]:
    return lambda d: _required(getattr(d.livedata, attr), attr)


def _fast_sensors(
    layout: PhaseLayout, has_battery: bool, stream_on: bool
) -> list[EnphaseSensorDescription]:
    out: list[EnphaseSensorDescription] = []
    for attr, key, name in _LIVE_METERS:
        meter = _live(attr)
        out.append(_power(f"{key}_power", f"{name} power", lambda d, m=meter: m(d).power))
        if stream_on:
            continue
        # With no stream these are the only per-phase readings.
        for ph in layout.phases:
            out.append(
                _power(
                    f"{key}_power_{_k(ph)}",
                    f"{name} power {PHASE_NAMES[ph]}",
                    lambda d, m=meter, ph=ph: m(d).phase_power[ph],
                    entity_registry_enabled_default=False,
                )
            )
    if not has_battery:
        return out

    storage = _live("storage")
    # Raw Envoy sign: positive is discharging (spec 5.1).
    out.append(_power("battery_power", "Battery power", lambda d: storage(d).power))

    def secctrl(d: FastData):
        return _required(d.secctrl, "secctrl")

    def schedule(d: FastData):
        return _required(d.schedule, "schedule")

    def energy(
        key: str, name: str, value_fn: Callable[[FastData], Any]
    ) -> EnphaseSensorDescription:
        return EnphaseSensorDescription(
            key=key,
            name=name,
            device_class=SensorDeviceClass.ENERGY_STORAGE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
            value_fn=value_fn,
        )

    def percent(key: str, name: str, value_fn: Callable[[FastData], Any], **kw: Any):
        return EnphaseSensorDescription(
            key=key,
            name=name,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
            value_fn=value_fn,
            **kw,
        )

    out += [
        percent(
            "battery_soc",
            "Battery",
            lambda d: secctrl(d).soc,
            device_class=SensorDeviceClass.BATTERY,
        ),
        energy(
            "available_battery_energy",
            "Available battery energy",
            lambda d: secctrl(d).available_energy,
        ),
        energy("battery_capacity", "Battery capacity", lambda d: secctrl(d).max_energy),
        energy(
            "reserve_battery_energy", "Reserve battery energy", lambda d: schedule(d).reserve_energy
        ),
        # The unique ID predates the name the Enphase app uses.
        percent(
            "reserve_battery_level", "Battery shutdown level", lambda d: secctrl(d).very_low_soc
        ),
        percent(
            "backup_soc_target", "Backup SoC target", lambda d: secctrl(d).configured_backup_soc
        ),
        percent(
            "battery_state_of_health",
            "Battery state of health",
            lambda d: secctrl(d).state_of_health,
        ),
        # A plain string: an enum would break on a mode code we haven't seen.
        EnphaseSensorDescription(
            key="controller_mode",
            name="Controller mode",
            value_fn=lambda d: schedule(d).mode,
        ),
    ]
    return out


# --- Slow (5.2, 5.3, 5.5, 5.6) ------------------------------------------------------------------

_LIFETIME = (
    ("production", "lifetime_production", "Lifetime production"),
    ("grid_import", "lifetime_grid_import", "Lifetime grid import"),
    ("grid_export", "lifetime_grid_export", "Lifetime grid export"),
    ("consumption", "lifetime_consumption", "Lifetime consumption"),
    ("storage_delivered", "lifetime_battery_discharged", "Lifetime battery discharged"),
    ("storage_received", "lifetime_battery_charged", "Lifetime battery charged"),
)


def _lifetime_sensors(data: SlowData) -> list[EnphaseSensorDescription]:
    """Only the counters whose meter is enabled (spec 5.2)."""
    return [
        EnphaseSensorDescription(
            key=key,
            name=name,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
            suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            value_fn=lambda d, a=attr: getattr(d.energy, a),
        )
        for attr, key, name in _LIFETIME
        if getattr(data.energy, attr) is not None
    ]


def _last_reported(value_fn: Callable[[SlowData], Any]) -> EnphaseSensorDescription:
    return EnphaseSensorDescription(
        key="last_reported",
        name="Last reported",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=value_fn,
    )


def _temperature(key: str, name: str, unit: str, value_fn: Callable[[SlowData], Any], **kw: Any):
    return EnphaseSensorDescription(
        key=key,
        name=name,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=unit,
        value_fn=value_fn,
        **kw,
    )


def _battery_sensors(serial: str) -> list[EnphaseSensorDescription]:
    def battery(d: SlowData):
        return by_serial(_required(d.inventory, "inventory").batteries, serial)

    return [
        EnphaseSensorDescription(
            key="soc",
            name=None,
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
            value_fn=lambda d: battery(d).soc,
        ),
        _temperature(
            "temperature", "Temperature", BATTERY_TEMPERATURE_UNIT, lambda d: battery(d).temperature
        ),
        _temperature(
            "max_cell_temperature",
            "Max cell temperature",
            BATTERY_TEMPERATURE_UNIT,
            lambda d: battery(d).max_cell_temperature,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        _last_reported(lambda d: battery(d).last_report),
        EnphaseSensorDescription(
            key="status",
            name="Status",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda d: battery(d).status,
        ),
    ]


def _controller_sensors(serial: str) -> list[EnphaseSensorDescription]:
    def controller(d: SlowData):
        return by_serial(_required(d.inventory, "inventory").system_controllers, serial)

    return [
        _temperature(
            "temperature",
            "Temperature",
            ENPOWER_TEMPERATURE_UNIT,
            lambda d: controller(d).temperature,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        _last_reported(lambda d: controller(d).last_report),
    ]


def _inverter_sensors(serial: str) -> list[EnphaseSensorDescription]:
    def inverter(d: SlowData):
        return by_serial(d.inverters, serial)

    return [
        _power(
            "power",
            None,
            lambda d: inverter(d).last_report_watts,
            entity_registry_enabled_default=False,
        ),
        _last_reported(lambda d: inverter(d).last_report),
    ]


_CONTACT_SETTINGS = (
    ("mode", "mode"),
    ("grid_action", "grid action"),
    ("micro_grid_action", "microgrid action"),
    ("gen_action", "generator action"),
    ("soc_low", "cutoff battery level"),
    ("soc_high", "restore battery level"),
)


def _contact_sensors(contact_id: str, label: str) -> list[EnphaseSensorDescription]:
    out = []
    for attr, name in _CONTACT_SETTINGS:
        is_level = attr.startswith("soc_")
        out.append(
            EnphaseSensorDescription(
                key=f"dry_contact_{contact_id}_{attr}",
                name=f"{label} {name}",
                entity_category=EntityCategory.DIAGNOSTIC,
                native_unit_of_measurement=PERCENTAGE if is_level else None,
                value_fn=lambda d, a=attr: getattr(d.dry_contact_settings[contact_id], a),
            )
        )
    return out


# --- Cloud (5.3) --------------------------------------------------------------------------------

_CLOUD_SENSORS = (
    EnphaseSensorDescription(
        key="storage_mode",
        name="Storage mode",
        value_fn=lambda s: s.profile,
    ),
)


# --- Platform -----------------------------------------------------------------------------------


class EnphaseSensor(EnphaseEntity[Any], SensorEntity):
    @property
    def native_value(self) -> Any:
        return self._value()[1]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    rt = entry.runtime_data
    envoy = envoy_device(rt.serial, rt.firmware)
    entities: list[EnphaseSensor] = []

    def add(
        coordinator: DataUpdateCoordinator[Any],
        descriptions: list[EnphaseSensorDescription] | tuple[EnphaseSensorDescription, ...],
        device: DeviceInfo,
        prefix: str,
    ) -> None:
        entities.extend(EnphaseSensor(coordinator, d, device, prefix) for d in descriptions)

    if rt.stream is not None:
        add(rt.stream, _stream_sensors(rt.phase_layout), envoy, rt.serial)
    add(
        rt.fast,
        _fast_sensors(rt.phase_layout, rt.hardware.has_battery, rt.stream is not None),
        envoy,
        rt.serial,
    )

    slow = rt.slow.data
    add(rt.slow, _lifetime_sensors(slow), envoy, rt.serial)
    contacts_device = envoy
    if slow.inventory is not None:
        for battery in slow.inventory.batteries:
            add(
                rt.slow,
                _battery_sensors(battery.serial),
                child_device("IQ Battery", battery.serial, rt.serial),
                battery.serial,
            )
        for controller in slow.inventory.system_controllers:
            device = child_device("IQ System Controller", controller.serial, rt.serial)
            contacts_device = device
            add(rt.slow, _controller_sensors(controller.serial), device, controller.serial)
    for inverter in slow.inverters:
        add(
            rt.slow,
            _inverter_sensors(inverter.serial),
            child_device("IQ Microinverter", inverter.serial, rt.serial),
            inverter.serial,
        )
    for contact_id, settings in slow.dry_contact_settings.items():
        add(
            rt.slow,
            _contact_sensors(contact_id, settings.load_name or contact_id),
            contacts_device,
            rt.serial,
        )

    if rt.cloud is not None:
        add(rt.cloud, _CLOUD_SENSORS, envoy, rt.serial)

    async_add_entities(entities)
