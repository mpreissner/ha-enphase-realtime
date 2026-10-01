"""Sensors (spec 5.1 to 5.6)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import EnphaseConfigEntry
from .const import (
    BATTERY_TEMPERATURE_UNIT,
    CONF_BACKUP_LOAD_ENTITY,
    DOMAIN,
    ENPOWER_TEMPERATURE_UNIT,
    OVERHEAD_STALE_LOG_AFTER,
    PHASE_NAMES,
)
from .coordinator import FastData, LiveCoordinator, LiveFeed, SlowData
from .dry_contact import remove_read_only_contacts
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
from .overhead import Overhead, to_watts

_LOGGER = logging.getLogger(__name__)

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
        # Last commanded mode, not a live status (FINDINGS): shows when the scheduler hasn't
        # acted on charge from grid.
        EnphaseSensorDescription(
            key="battery_scheduler_mode",
            name="Battery scheduler mode",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda d: d.schedule.mode,
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


# --- Installer settings: export limit and PCS (docs/FINDINGS.md) --------------------------------


def _installer_sensors(data: SlowData) -> list[EnphaseSensorDescription]:
    def pel(d: SlowData):
        return _required(d.export_limit, "export limit")

    def pcs(d: SlowData):
        return _required(d.pcs, "PCS settings")

    def rating(key: str, name: str, value_fn: Callable[[SlowData], Any]):
        return EnphaseSensorDescription(
            key=key,
            name=name,
            device_class=SensorDeviceClass.CURRENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=0,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=value_fn,
        )

    out = []
    if data.export_limit is not None:
        out += [
            EnphaseSensorDescription(
                key="export_limit_mode",
                name="Export limit mode",
                device_class=SensorDeviceClass.ENUM,
                options=["off", "soft", "hard", "soft_and_hard", "on"],
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda d: pel(d).mode,
            ),
            # The unit isn't confirmed (models.ExportLimit), so none is claimed.
            EnphaseSensorDescription(
                key="export_limit",
                name="Export limit",
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda d: pel(d).limit,
            ),
            EnphaseSensorDescription(
                key="export_limit_type",
                name="Export limit type",
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda d: pel(d).limit_type,
            ),
        ]
    if data.pcs is not None:
        out += [
            rating("main_breaker_rating", "Main breaker rating", lambda d: pcs(d).main_breaker),
            rating("main_busbar_rating", "Main busbar rating", lambda d: pcs(d).main_busbar),
            rating("der_breaker_rating", "DER breaker rating", lambda d: pcs(d).der_breaker),
            EnphaseSensorDescription(
                key="consumption_meter_location",
                name="Consumption meter location",
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda d: pcs(d).consumption_meter_location,
            ),
        ]
    return out


# --- Enphase overhead (docs/specs/enphase-overhead.md) ------------------------------------------

OVERHEAD_POWER = "enphase_overhead_power"
OVERHEAD_ENERGY = "enphase_overhead_energy"


class OverheadFeed:
    """Takes one sample per live poll and then writes both overhead entities, so they always
    show the same sample whatever order the coordinator calls its listeners in."""

    def __init__(self, hass: HomeAssistant, live: LiveCoordinator, backup_entity: str) -> None:
        self.hass = hass
        self.live = live
        self.backup_entity = backup_entity
        self.overhead = Overhead()
        self.entities: list[SensorEntity] = []
        # Whether the current run of skipped (stale) polls has been logged.
        self._stale_logged = False
        # The backup load's last_changed, and the monotonic time this feed first saw it. Ages
        # are taken on the monotonic clock, so a wall-clock step can't make the value look stale.
        self._backup_changed: datetime | None = None
        self._backup_seen = 0.0

    @callback
    def sample(self) -> None:
        now = time.monotonic()
        load = None
        if self.live.last_update_success and self.live.data is not None:
            meter = self.live.data.livedata.load
            load = meter.power if meter is not None else None
        backup = None
        if (state := self.hass.states.get(self.backup_entity)) is not None:
            backup = to_watts(state.state, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
            if state.last_changed != self._backup_changed:
                self._backup_changed = state.last_changed
                self._backup_seen = now
        backup_age = now - self._backup_seen
        rejecting_since = self.overhead.rejecting_since
        stale_since = self.overhead.stale_since
        backup_bad_at = self.overhead.backup_bad_at
        self.overhead.add(now, load, backup, backup_age)
        if backup_bad_at is None and self.overhead.backup_bad_at is not None:
            if backup is None:
                _LOGGER.debug("Enphase overhead: backup load unavailable, skipping samples")
            else:
                _LOGGER.debug(
                    "Enphase overhead: backup load held at %s W for %.0f s, skipping samples",
                    backup,
                    backup_age,
                )
        elif backup_bad_at is not None and self.overhead.backup_bad_at is None:
            _LOGGER.debug(
                "Enphase overhead: backup load fresh for %.0f s, taking samples again",
                now - backup_bad_at,
            )
        if (since := self.overhead.stale_since) is not None:
            if not self._stale_logged and now - since >= OVERHEAD_STALE_LOG_AFTER:
                _LOGGER.debug(
                    "Enphase overhead: Envoy load held at %s W for %.1f s, skipping samples",
                    load,
                    now - since,
                )
                self._stale_logged = True
        elif stale_since is not None:
            if self._stale_logged:
                _LOGGER.debug(
                    "Enphase overhead: Envoy load moving again after %.1f s", now - stale_since
                )
            self._stale_logged = False
        if rejecting_since is None and self.overhead.rejecting_since is not None:
            _LOGGER.debug(
                "Enphase overhead: rejecting samples (Envoy load %s W, backup load %s W)",
                load,
                backup,
            )
        elif rejecting_since is not None and self.overhead.rejecting_since is None:
            _LOGGER.debug(
                "Enphase overhead: accepting samples again after %.1f s", now - rejecting_since
            )
        for entity in self.entities:
            if entity.hass is not None:
                entity.async_write_ha_state()


class OverheadPowerSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Enphase overhead power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0

    def __init__(self, feed: OverheadFeed, device: DeviceInfo, serial: str) -> None:
        self._feed = feed
        self._attr_device_info = device
        self._attr_unique_id = f"{serial}_{OVERHEAD_POWER}"

    @property
    def available(self) -> bool:
        # Unavailable with the other live entities after a run of failed polls.
        return self._feed.live.entities_available and self.native_value is not None

    @property
    def native_value(self) -> float | None:
        return self._feed.overhead.mean(time.monotonic())


class OverheadEnergySensor(RestoreSensor):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Enphase overhead energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    # Samples can be negative (meter error), and clipping them would bias the total (spec 5).
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    _attr_suggested_display_precision = 0

    def __init__(self, feed: OverheadFeed, device: DeviceInfo, serial: str) -> None:
        self._feed = feed
        self._attr_device_info = device
        self._attr_unique_id = f"{serial}_{OVERHEAD_ENERGY}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and isinstance(last.native_value, int | float):
            self._feed.overhead.energy_wh += float(last.native_value)

    @property
    def native_value(self) -> float:
        return self._feed.overhead.energy_wh


def _overhead_entities(
    hass: HomeAssistant, entry: EnphaseConfigEntry, device: DeviceInfo
) -> list[SensorEntity]:
    """The overhead entities when a backup-load sensor is set; otherwise drop any left over."""
    rt = entry.runtime_data
    backup_entity = rt.options.get(CONF_BACKUP_LOAD_ENTITY)
    if not backup_entity:
        registry = er.async_get(hass)
        for key in (OVERHEAD_POWER, OVERHEAD_ENERGY):
            if entity_id := registry.async_get_entity_id("sensor", DOMAIN, f"{rt.serial}_{key}"):
                registry.async_remove(entity_id)
        return []
    feed = OverheadFeed(hass, rt.live, backup_entity)
    feed.entities = [
        OverheadPowerSensor(feed, device, rt.serial),
        OverheadEnergySensor(feed, device, rt.serial),
    ]
    entry.async_on_unload(rt.live.async_add_listener(feed.sample))
    # The first poll ran before this listener existed.
    feed.sample()
    return feed.entities


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
    entities: list[SensorEntity] = []

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
    add(rt.slow, _installer_sensors(slow), envoy, rt.serial)
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
            add(rt.slow, _controller_sensors(controller.serial), device, controller.serial)
    for inverter in slow.inverters:
        add(
            rt.slow,
            _inverter_sensors(inverter.serial),
            child_device(IQ_MICROINVERTER, inverter.serial, rt.envoy_device_id),
            inverter.serial,
        )
    remove_read_only_contacts(hass, entry, "sensor")

    # Versions before 0.4 had a read-only storage mode sensor; the select replaces it.
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id("sensor", DOMAIN, f"{rt.serial}_storage_mode"):
        registry.async_remove(entity_id)

    entities.extend(_overhead_entities(hass, entry, envoy))
    async_add_entities(entities)
