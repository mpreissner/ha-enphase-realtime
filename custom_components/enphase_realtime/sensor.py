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
from .coordinator import FastData, LiveFeed, SlowData
from .entity import (
    IQ_BATTERY,
    IQ_MICROINVERTER,
    IQ_SYSTEM_CONTROLLER,
    EnphaseEntity,
    by_serial,
    child_device,
    envoy_device,
)
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


def _key(name: str) -> str:
    """A unique-ID key that matches the entity ID Home Assistant derives from `name`."""
    return name.lower().replace(" ", "_")


def _required[T](value: T | None, what: str) -> T:
    """Missing sections make the entity unavailable rather than unknown."""
    if value is None:
        raise KeyError(what)
    return value


# --- Stream (5.1) -------------------------------------------------------------------------------

# Frame attribute, power sensor name and CT name, as the core integration names them.
_STREAM_METERS = (
    ("production", "Current power production", "production CT"),
    ("total_consumption", "Current power consumption", "total consumption CT"),
    ("net_consumption", "Current net power consumption", "net consumption CT"),
)


def _stream_meter(attr: str) -> Callable[[StreamFrame], StreamMeter]:
    return lambda frame: _required(getattr(frame, attr), attr)


def _stream_sensors(layout: PhaseLayout) -> list[EnphaseSensorDescription]:
    out: list[EnphaseSensorDescription] = []
    for attr, name, ct in _STREAM_METERS:
        meter = _stream_meter(attr)
        out.append(_power(_key(name), name, lambda f, m=meter: m(f).power))
        for ph in layout.phases:
            phase = PHASE_NAMES[ph]
            out.append(
                _power(
                    _key(f"{name} {phase}"),
                    f"{name} {phase}",
                    lambda f, m=meter, ph=ph: m(f).phases[ph].power,
                )
            )
            current = f"{ct[0].upper()}{ct[1:]} current {phase}"
            out.append(
                EnphaseSensorDescription(
                    key=_key(current),
                    name=current,
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
                    key=_key(f"Power factor {ct} {phase}"),
                    name=f"Power factor {ct} {phase}",
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
        name = "Voltage net consumption CT" + (f" {PHASE_NAMES[ph]}" if layout.phases else "")
        out.append(
            EnphaseSensorDescription(
                key=_key(name),
                name=name,
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
            key="frequency_net_consumption_ct",
            name="Frequency net consumption CT",
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


# --- Live (5.1) ---------------------------------------------------------------------------------

# LiveData attribute, unique-ID key, name.
_LIVE_METERS = (
    ("grid", "grid", "Grid"),
    ("load", "load", "Load"),
    ("pv", "pv", "PV"),
)


def _live(attr: str) -> Callable[[LiveFeed], LivePower]:
    return lambda d: _required(getattr(d.livedata, attr), attr)


def _live_sensors(
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
    if has_battery:
        storage = _live("storage")
        # Raw Envoy sign: positive is discharging (spec 5.1), as the core integration's.
        out.append(
            _power(
                "current_battery_discharge",
                "Current battery discharge",
                lambda d: storage(d).power,
            )
        )
    return out


# --- Fast (5.3) ---------------------------------------------------------------------------------


def _fast_sensors() -> list[EnphaseSensorDescription]:
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

    return [
        percent(
            "battery_soc",
            "Battery",
            lambda d: d.secctrl.soc,
            device_class=SensorDeviceClass.BATTERY,
        ),
        energy(
            "available_battery_energy",
            "Available battery energy",
            lambda d: d.secctrl.available_energy,
        ),
        energy("battery_capacity", "Battery capacity", lambda d: d.secctrl.max_energy),
        energy(
            "reserve_battery_energy", "Reserve battery energy", lambda d: d.schedule.reserve_energy
        ),
        # The Enphase app's name; the core integration doesn't have it.
        percent(
            "battery_shutdown_level", "Battery shutdown level", lambda d: d.secctrl.very_low_soc
        ),
        percent(
            "reserve_battery_level",
            "Reserve battery level",
            lambda d: d.secctrl.adjusted_backup_soc,
        ),
        percent(
            "configured_reserve_battery_level",
            "Configured reserve battery level",
            lambda d: d.secctrl.configured_backup_soc,
        ),
        percent(
            "battery_state_of_health",
            "Battery state of health",
            lambda d: d.secctrl.state_of_health,
        ),
    ]


# --- Slow (5.2, 5.3, 5.5, 5.6) ------------------------------------------------------------------

# The core integration's names, so the Energy dashboard keeps its history across a move. Its
# "net energy consumption" is grid import and "net energy production" grid export.
_LIFETIME = (
    ("production", "Lifetime energy production"),
    ("grid_import", "Lifetime net energy consumption"),
    ("grid_export", "Lifetime net energy production"),
    ("consumption", "Lifetime energy consumption"),
    ("storage_delivered", "Lifetime battery energy discharged"),
    ("storage_received", "Lifetime battery energy charged"),
)


def _lifetime_sensors(data: SlowData) -> list[EnphaseSensorDescription]:
    """Only the counters whose meter is enabled (spec 5.2)."""
    return [
        EnphaseSensorDescription(
            key=_key(name),
            name=name,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
            suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            value_fn=lambda d, a=attr: getattr(d.energy, a),
        )
        for attr, name in _LIFETIME
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
            # Named by its device class, "Battery", as in the core integration.
            key="soc",
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
        rt.live,
        _live_sensors(rt.phase_layout, rt.hardware.has_battery, rt.stream is not None),
        envoy,
        rt.serial,
    )
    if rt.fast is not None:
        add(rt.fast, _fast_sensors(), envoy, rt.serial)

    slow = rt.slow.data
    add(rt.slow, _lifetime_sensors(slow), envoy, rt.serial)
    contacts_device = envoy
    if slow.inventory is not None:
        for battery in slow.inventory.batteries:
            add(
                rt.slow,
                _battery_sensors(battery.serial),
                child_device(IQ_BATTERY, battery.serial, rt.envoy_device_id),
                battery.serial,
            )
        for controller in slow.inventory.system_controllers:
            device = child_device(IQ_SYSTEM_CONTROLLER, controller.serial, rt.envoy_device_id)
            contacts_device = device
            add(rt.slow, _controller_sensors(controller.serial), device, controller.serial)
    for inverter in slow.inverters:
        add(
            rt.slow,
            _inverter_sensors(inverter.serial),
            child_device(IQ_MICROINVERTER, inverter.serial, rt.envoy_device_id),
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
