"""Typed views of the Envoy's local payloads.

Every parser takes the decoded body (JSON already loaded, or XML text for `/info`) and returns a
frozen dataclass. Units are converted here, once: `livedata` milliwatts become watts, and Unix
timestamps become aware UTC datetimes. A payload missing a field the model needs raises
`EnvoyParseError`; optional fields come back as `None`.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .errors import EnvoyParseError

__all__ = ["EnvoyParseError"]  # re-exported: parsers raise it, callers catch it from here


@contextmanager
def _parsing(what: str) -> Iterator[None]:
    """Turn the lookup errors a malformed payload causes into one exception type."""
    try:
        yield
    # ET.ParseError is a SyntaxError, not a ValueError, so it's listed separately.
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, ET.ParseError) as err:
        if isinstance(err, EnvoyParseError):
            raise
        raise EnvoyParseError(f"unexpected {what} payload: {err!r}") from err


def _timestamp(value: Any) -> datetime | None:
    """Envoy timestamps are Unix seconds, with 0 meaning "never"."""
    if not value:
        return None
    return datetime.fromtimestamp(int(value), UTC)


# --- /info --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnvoyInfo:
    serial: str
    firmware: str
    part_number: str | None

    @property
    def firmware_major(self) -> int | None:
        """`D8.3.6086` → 8. `None` if the version string has no leading number."""
        match = re.match(r"\D*(\d+)", self.firmware)
        return int(match.group(1)) if match else None

    @classmethod
    def from_xml(cls, text: str) -> EnvoyInfo:
        with _parsing("/info"):
            # The document comes from the user's own Envoy on the LAN, is a few KB, and Python's
            # bundled expat rejects entity-expansion attacks, so defusedxml isn't needed.
            device = ET.fromstring(text).find("device")  # noqa: S314
            if device is None:
                raise EnvoyParseError("/info has no <device> element")
            serial = device.findtext("sn")
            firmware = device.findtext("software")
            if not serial or not firmware:
                raise EnvoyParseError("/info is missing <sn> or <software>")
            return cls(serial=serial, firmware=firmware, part_number=device.findtext("pn"))


# --- Phase layout (spec 3.3) --------------------------------------------------------------------


class PhaseLayout(StrEnum):
    SINGLE = "single"
    SPLIT = "split"
    THREE = "three"

    @property
    def phases(self) -> tuple[str, ...]:
        """The frame keys that get per-phase entities. Single-phase gets totals only."""
        return {
            PhaseLayout.SINGLE: (),
            PhaseLayout.SPLIT: ("ph-a", "ph-b"),
            PhaseLayout.THREE: ("ph-a", "ph-b", "ph-c"),
        }[self]


# Only "split" has been seen on a real Envoy; the other two spellings are guesses (spike S9).
# Anything else falls through to the phase counts.
_PHASE_MODES = {mode.value: mode for mode in PhaseLayout}


def detect_phase_layout(
    meters: Sequence[Meter], livedata: LiveData | None = None
) -> PhaseLayout | None:
    """Work out the site's layout from its CT configuration.

    Prefers the enabled production meter's `phaseMode`, then any enabled meter's, then the phase
    counts. Returns `None` when nothing identifies the layout (for example two phases that aren't
    split-phase), so the caller can refuse setup instead of guessing.
    """
    enabled = sorted(
        (m for m in meters if m.enabled), key=lambda m: m.measurement_type != "production"
    )
    for meter in enabled:
        if meter.phase_mode in _PHASE_MODES:
            return _PHASE_MODES[meter.phase_mode]

    counts = [(m.phase_count, None) for m in enabled if m.phase_count]
    if livedata is not None and livedata.phase_count:
        counts.append((livedata.phase_count, livedata.is_split_phase))
    for count, is_split in counts:
        if count == 1:
            return PhaseLayout.SINGLE
        if count == 3:
            return PhaseLayout.THREE
        if count == 2 and is_split:
            return PhaseLayout.SPLIT
    return None


# --- /ivp/meters, /ivp/meters/readings, /ivp/meters/reports (spec 5.2) --------------------------


@dataclass(frozen=True, slots=True)
class Meter:
    eid: int
    measurement_type: str
    enabled: bool
    phase_mode: str | None
    phase_count: int | None

    @classmethod
    def parse_list(cls, data: Any) -> list[Meter]:
        with _parsing("/ivp/meters"):
            return [
                cls(
                    eid=int(m["eid"]),
                    measurement_type=m["measurementType"],
                    enabled=m["state"] == "enabled",
                    phase_mode=m.get("phaseMode"),
                    phase_count=m.get("phaseCount"),
                )
                for m in data
            ]


@dataclass(frozen=True, slots=True)
class LifetimeEnergy:
    """Lifetime counters in Wh. `None` means the site has no enabled meter for it.

    Storage keeps the Envoy's names: which of delivered and received is "charged" is spike S5.
    """

    production: float | None
    grid_import: float | None
    grid_export: float | None
    consumption: float | None
    storage_delivered: float | None
    storage_received: float | None

    @classmethod
    def from_payloads(cls, meters: Sequence[Meter], readings: Any, reports: Any) -> LifetimeEnergy:
        """Combine readings (per-eid import/export) with reports (calculated consumption).

        Readings for eids that `/ivp/meters` doesn't list, or lists as disabled, are ignored.
        Consumption is only reported when the net-consumption CT is enabled, since the Envoy
        derives total consumption from it.
        """
        types = {m.eid: m.measurement_type for m in meters if m.enabled}
        with _parsing("/ivp/meters/readings"):
            by_type = {
                types[int(r["eid"])]: (float(r["actEnergyDlvd"]), float(r["actEnergyRcvd"]))
                for r in readings
                if int(r["eid"]) in types
            }
        with _parsing("/ivp/meters/reports"):
            reported = {
                r["reportType"]: float(r["cumulative"]["whDlvdCum"])
                for r in reports
                if "cumulative" in r
            }

        production = by_type.get("production")
        net = by_type.get("net-consumption")
        storage = by_type.get("storage")
        return cls(
            production=production[0] if production else None,
            grid_import=net[0] if net else None,
            grid_export=net[1] if net else None,
            consumption=reported.get("total-consumption") if net else None,
            storage_delivered=storage[0] if storage else None,
            storage_received=storage[1] if storage else None,
        )


# --- /ivp/livedata/status (spec 5.1) ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LivePower:
    """One `livedata` meter, in W and VA. Keys of the per-phase dicts are `ph-a` to `ph-c`."""

    power: float
    apparent_power: float
    phase_power: dict[str, float]

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> LivePower:
        return cls(
            power=data["agg_p_mw"] / 1000,
            apparent_power=data["agg_s_mva"] / 1000,
            phase_power={
                f"ph-{p}": data[f"agg_p_ph_{p}_mw"] / 1000
                for p in "abc"
                if f"agg_p_ph_{p}_mw" in data
            },
        )


@dataclass(frozen=True, slots=True)
class LiveData:
    """Storage power keeps the Envoy's sign; which way is charging is fixed in the entity."""

    last_update: datetime | None
    soc: int | None
    backup_soc: int | None
    is_split_phase: bool
    phase_count: int | None
    grid: LivePower | None
    load: LivePower | None
    pv: LivePower | None
    storage: LivePower | None
    generator: LivePower | None
    # `connection.sc_stream`: "enabled" while the System Controller pushes fresh values.
    sc_stream: str | None = None

    @classmethod
    def from_payload(cls, data: Any) -> LiveData:
        with _parsing("/ivp/livedata/status"):
            meters = data["meters"]

            def power(key: str) -> LivePower | None:
                return LivePower.from_payload(meters[key]) if key in meters else None

            return cls(
                last_update=_timestamp(meters.get("last_update")),
                soc=meters.get("soc"),
                backup_soc=meters.get("backup_soc"),
                is_split_phase=bool(meters.get("is_split_phase")),
                phase_count=meters.get("phase_count"),
                grid=power("grid"),
                load=power("load"),
                pv=power("pv"),
                storage=power("storage"),
                generator=power("generator"),
                sc_stream=(data.get("connection") or {}).get("sc_stream"),
            )


# --- /ivp/ensemble/secctrl, /ivp/sc/sched, /ivp/ensemble/relay (spec 5.3, 5.4) -----------------


@dataclass(frozen=True, slots=True)
class SecCtrl:
    soc: int
    state_of_health: int | None
    available_energy: int
    max_energy: int
    backup_energy: int | None
    very_low_soc: int
    configured_backup_soc: int
    # The reserve the Envoy applies now; differs from the configured one, e.g. during Storm Guard.
    adjusted_backup_soc: int | None

    @classmethod
    def from_payload(cls, data: Any) -> SecCtrl:
        with _parsing("/ivp/ensemble/secctrl"):
            return cls(
                soc=data["agg_soc"],
                state_of_health=data.get("ENC_agg_soh"),
                available_energy=data["ENC_agg_avail_energy"],
                max_energy=data["Max_energy"],
                backup_energy=data.get("ENC_agg_backup_energy"),
                very_low_soc=data["VLS_Limit"],
                configured_backup_soc=data["configured_backup_soc"],
                adjusted_backup_soc=data.get("adjusted_backup_soc"),
            )


@dataclass(frozen=True, slots=True)
class Schedule:
    """`/ivp/sc/sched`. `mode` is the scheduler's last commanded mode, not a live status: it
    can read Charge From Grid after the battery has stopped, and it stays at Charge From PV
    when the scheduler hasn't acted on charge from grid (docs/FINDINGS.md)."""

    charge_from_grid_allowed: bool
    reserve_energy: int | None
    battery_count: int | None
    mode: str | None

    @classmethod
    def from_payload(cls, data: Any) -> Schedule:
        with _parsing("/ivp/sc/sched"):
            return cls(
                charge_from_grid_allowed=bool(data["Charge From Grid Allowed"]),
                reserve_energy=data.get("Agg VLS Energy"),
                battery_count=data.get("Num_of_enc"),
                mode=_sched_mode(data.get("acb_current_mode"), data.get("sched_mode_key")),
            )


def _sched_mode(index: Any, keys: Any) -> str | None:
    """`acb_current_mode` indexes into `sched_mode_key`, e.g. "CG - Charge From Grid". Returns
    the name without its code, or None when the index isn't in the list."""
    if not isinstance(index, int) or not isinstance(keys, list) or not 0 <= index < len(keys):
        return None
    return str(keys[index]).split(" - ", 1)[-1]


@dataclass(frozen=True, slots=True)
class Relay:
    admin_state: str
    oper_state: str

    @property
    def grid_connected(self) -> bool:
        return self.oper_state == "closed"

    @classmethod
    def from_payload(cls, data: Any) -> Relay:
        with _parsing("/ivp/ensemble/relay"):
            return cls(admin_state=data["mains_admin_state"], oper_state=data["mains_oper_state"])


# --- /ivp/ensemble/inventory (spec 5.3) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Battery:
    """Temperatures are in the unit the device reports; see `const.py` for which that is."""

    serial: str
    soc: int | None
    temperature: float | None
    max_cell_temperature: float | None
    communicating: bool
    dc_switch_off: bool | None
    last_report: datetime | None
    status: str | None
    capacity: int | None


@dataclass(frozen=True, slots=True)
class SystemController:
    serial: str
    temperature: float | None
    communicating: bool
    last_report: datetime | None


@dataclass(frozen=True, slots=True)
class Inventory:
    batteries: list[Battery]
    system_controllers: list[SystemController]

    @classmethod
    def from_payload(cls, data: Any) -> Inventory:
        with _parsing("/ivp/ensemble/inventory"):
            devices = {group["type"]: group.get("devices", []) for group in data}
            return cls(
                batteries=[
                    Battery(
                        serial=d["serial_num"],
                        soc=d.get("percentFull"),
                        temperature=d.get("temperature"),
                        max_cell_temperature=d.get("maxCellTemp"),
                        communicating=bool(d.get("communicating")),
                        dc_switch_off=d.get("dc_switch_off"),
                        last_report=_timestamp(d.get("last_rpt_date")),
                        status=d.get("admin_state_str"),
                        capacity=d.get("encharge_capacity"),
                    )
                    for d in devices.get("ENCHARGE", [])
                ],
                system_controllers=[
                    SystemController(
                        serial=d["serial_num"],
                        temperature=d.get("temperature"),
                        communicating=bool(d.get("communicating")),
                        last_report=_timestamp(d.get("last_rpt_date")),
                    )
                    for d in devices.get("ENPOWER", [])
                ],
            )


# --- Dry contacts (spec 5.6) --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DryContactSettings:
    id: str
    load_name: str
    mode: str | None
    grid_action: str | None
    micro_grid_action: str | None
    gen_action: str | None
    soc_low: float | None
    soc_high: float | None

    @classmethod
    def parse_dict(cls, data: Any) -> dict[str, DryContactSettings]:
        with _parsing("/ivp/ss/dry_contact_settings"):
            return {
                c["id"]: cls(
                    id=c["id"],
                    load_name=c.get("load_name") or "",
                    mode=c.get("mode"),
                    grid_action=c.get("grid_action"),
                    micro_grid_action=c.get("micro_grid_action"),
                    gen_action=c.get("gen_action"),
                    soc_low=c.get("soc_low"),
                    soc_high=c.get("soc_high"),
                )
                for c in data["dry_contacts"]
            }


def parse_dry_contact_states(data: Any) -> dict[str, bool]:
    """`/ivp/ensemble/dry_contacts` → contact id to "relay closed"."""
    with _parsing("/ivp/ensemble/dry_contacts"):
        return {c["id"]: c["status"] == "closed" for c in data["dry_contacts"]}


# --- /api/v1/production/inverters (spec 5.5) ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Inverter:
    serial: str
    last_report_watts: int
    max_report_watts: int
    last_report: datetime | None

    @classmethod
    def parse_list(cls, data: Any) -> list[Inverter]:
        with _parsing("/api/v1/production/inverters"):
            return [
                cls(
                    serial=i["serialNumber"],
                    last_report_watts=i["lastReportWatts"],
                    max_report_watts=i["maxReportWatts"],
                    last_report=_timestamp(i.get("lastReportDate")),
                )
                for i in data
            ]


# --- /stream/meter (spec 3.1, 5.1) --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PhaseReading:
    """One phase of one stream meter: W, var, VA, V, A, power factor, Hz."""

    power: float
    reactive_power: float
    apparent_power: float
    voltage: float
    current: float
    power_factor: float
    frequency: float

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> PhaseReading:
        return cls(
            power=data["p"],
            reactive_power=data["q"],
            apparent_power=data["s"],
            voltage=data["v"],
            current=data["i"],
            power_factor=data["pf"],
            frequency=data["f"],
        )


@dataclass(frozen=True, slots=True)
class StreamMeter:
    """All phases the frame carried. Frames include `ph-c` even on split-phase sites."""

    phases: dict[str, PhaseReading]

    @property
    def power(self) -> float:
        """Total across phases; an unused phase reads 0, so summing all of them is safe."""
        return sum(p.power for p in self.phases.values())


@dataclass(frozen=True, slots=True)
class StreamFrame:
    production: StreamMeter | None
    net_consumption: StreamMeter | None
    total_consumption: StreamMeter | None

    def split_phase_voltage(self) -> float | None:
        """L1–L2 voltage on a split-phase site: the legs are 180° apart, so it's `v_a + v_b`."""
        for meter in (self.net_consumption, self.production, self.total_consumption):
            if meter and "ph-a" in meter.phases and "ph-b" in meter.phases:
                return meter.phases["ph-a"].voltage + meter.phases["ph-b"].voltage
        return None

    @classmethod
    def from_payload(cls, data: Any) -> StreamFrame:
        with _parsing("/stream/meter"):

            def meter(key: str) -> StreamMeter | None:
                if key not in data:
                    return None
                return StreamMeter(
                    {ph: PhaseReading.from_payload(v) for ph, v in data[key].items()}
                )

            return cls(
                production=meter("production"),
                net_consumption=meter("net-consumption"),
                total_consumption=meter("total-consumption"),
            )
