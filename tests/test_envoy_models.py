"""Envoy payload parsers, against the reference-site captures.

Tests for single- and three-phase layouts edit the reference payloads, since no real captures
exist yet (spike S9). Those payloads are built by `_synthetic_meters` and say so.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest
from envoy_client.models import (
    DryContactSettings,
    EnvoyInfo,
    EnvoyParseError,
    Inventory,
    Inverter,
    LifetimeEnergy,
    LiveData,
    Meter,
    PhaseLayout,
    Relay,
    Schedule,
    SecCtrl,
    detect_phase_layout,
    parse_dry_contact_states,
)

from tests.helpers import load_json, load_text

UNMAPPED_EID = 1023410688


def _synthetic_meters(**changes: Any) -> list[Meter]:
    """SYNTHETIC: the reference `/ivp/meters` with every meter's fields overridden."""
    data = load_json("ivp_meters.json")
    for meter in data:
        meter.update(changes)
    return Meter.parse_list(data)


# --- /info --------------------------------------------------------------------------------------


def test_info_xml() -> None:
    info = EnvoyInfo.from_xml(load_text("info.xml"))
    assert info.serial == "900000000001"
    assert info.firmware == "D8.3.6086"
    assert info.firmware_major == 8
    assert info.part_number == "800-00664-r05"


@pytest.mark.parametrize(
    ("firmware", "major"), [("D7.0.88", 7), ("R4.10.35", 4), ("8.2.4264", 8), ("unknown", None)]
)
def test_firmware_major(firmware: str, major: int | None) -> None:
    assert EnvoyInfo("1", firmware, None).firmware_major == major


@pytest.mark.parametrize(
    "text", ["<envoy_info", "<envoy_info/>", "<envoy_info><device><sn>1</sn></device></envoy_info>"]
)
def test_info_xml_malformed(text: str) -> None:
    with pytest.raises(EnvoyParseError):
        EnvoyInfo.from_xml(text)


# --- Phase layout -------------------------------------------------------------------------------


def test_reference_site_is_split_phase() -> None:
    meters = Meter.parse_list(load_json("ivp_meters.json"))
    livedata = LiveData.from_payload(load_json("ivp_livedata_status.json"))
    assert detect_phase_layout(meters, livedata) is PhaseLayout.SPLIT
    assert PhaseLayout.SPLIT.phases == ("ph-a", "ph-b")


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"phaseMode": "three", "phaseCount": 3}, PhaseLayout.THREE),
        ({"phaseMode": "single", "phaseCount": 1}, PhaseLayout.SINGLE),
        # Unseen spellings fall back to the count instead of failing.
        ({"phaseMode": "3ph-wye", "phaseCount": 3}, PhaseLayout.THREE),
        ({"phaseMode": "1ph", "phaseCount": 1}, PhaseLayout.SINGLE),
        # Two phases without split-phase confirmation isn't a layout we can name.
        ({"phaseMode": "2ph", "phaseCount": 2}, None),
    ],
)
def test_phase_layout_synthetic(changes: dict[str, Any], expected: PhaseLayout | None) -> None:
    assert detect_phase_layout(_synthetic_meters(**changes)) is expected


def test_phase_layout_uses_livedata_split_flag() -> None:
    meters = _synthetic_meters(phaseMode="2ph", phaseCount=None)
    livedata = LiveData.from_payload(load_json("ivp_livedata_status.json"))
    assert detect_phase_layout(meters, livedata) is PhaseLayout.SPLIT


def test_phase_layout_ignores_disabled_meters() -> None:
    data = load_json("ivp_meters.json")
    for meter in data:
        if meter["state"] == "disabled":
            meter["phaseMode"] = "three"
    assert detect_phase_layout(Meter.parse_list(data)) is PhaseLayout.SPLIT


def test_phase_layout_unknown_without_meters() -> None:
    assert detect_phase_layout([]) is None


# --- Lifetime energy ----------------------------------------------------------------------------


def _energy(meters: Any = None, readings: Any = None, reports: Any = None) -> LifetimeEnergy:
    return LifetimeEnergy.from_payloads(
        Meter.parse_list(meters if meters is not None else load_json("ivp_meters.json")),
        readings if readings is not None else load_json("ivp_meters_readings.json"),
        reports if reports is not None else load_json("ivp_meters_reports.json"),
    )


def test_lifetime_energy_reference() -> None:
    energy = _energy()
    assert energy.production == pytest.approx(742844.057)
    assert energy.grid_import == pytest.approx(1124885.476)
    assert energy.grid_export == pytest.approx(371245.494)
    assert energy.consumption == pytest.approx(1496484.23)
    assert energy.storage_delivered == pytest.approx(626.383)
    assert energy.storage_received == pytest.approx(13560.114)


def test_reports_net_is_readings_import_minus_export() -> None:
    """Why import/export come from readings: reports only has the net figure (spec 5.2)."""
    # The two captures were taken a moment apart, so the totals differ by a fraction of a Wh.
    energy = _energy()
    net = next(
        r for r in load_json("ivp_meters_reports.json") if r["reportType"] == "net-consumption"
    )
    assert net["cumulative"]["whRcvdCum"] == 0
    assert net["cumulative"]["whDlvdCum"] == pytest.approx(
        energy.grid_import - energy.grid_export, abs=1
    )


def test_unmapped_eid_is_ignored() -> None:
    readings = load_json("ivp_meters_readings.json")
    assert any(r["eid"] == UNMAPPED_EID for r in readings)
    without = [r for r in readings if r["eid"] != UNMAPPED_EID]
    tampered = copy.deepcopy(readings)
    for r in tampered:
        if r["eid"] == UNMAPPED_EID:
            r["actEnergyDlvd"] = 9e9
    assert _energy(readings=tampered) == _energy(readings=without) == _energy()


def test_disabled_storage_meter_has_no_counters() -> None:
    meters = load_json("ivp_meters.json")
    for m in meters:
        if m["measurementType"] == "storage":
            m["state"] = "disabled"
    energy = _energy(meters=meters)
    assert energy.storage_delivered is None
    assert energy.storage_received is None
    assert energy.production is not None


def test_no_consumption_ct_means_no_import_export_or_consumption() -> None:
    meters = [m for m in load_json("ivp_meters.json") if m["measurementType"] != "net-consumption"]
    energy = _energy(meters=meters)
    assert (energy.grid_import, energy.grid_export, energy.consumption) == (None, None, None)


def test_malformed_readings() -> None:
    with pytest.raises(EnvoyParseError):
        _energy(readings=[{"eid": 704643328}])


# --- livedata -----------------------------------------------------------------------------------


def test_livedata_converts_milliwatts() -> None:
    live = LiveData.from_payload(load_json("ivp_livedata_status.json"))
    assert live.grid is not None
    assert live.grid.power == pytest.approx(1213.8)
    assert live.grid.phase_power == pytest.approx({"ph-a": 628.518, "ph-b": 585.281, "ph-c": 0.0})
    assert live.storage is not None
    assert live.storage.apparent_power == pytest.approx(-2.981)
    assert live.soc == 59
    assert live.is_split_phase
    assert live.phase_count == 2
    assert live.last_update == datetime.fromtimestamp(1790257937, UTC)
    assert live.sc_stream == "enabled"


def test_livedata_without_connection_section() -> None:
    payload = load_json("ivp_livedata_status.json")
    del payload["connection"]
    assert LiveData.from_payload(payload).sc_stream is None


def test_livedata_missing_meters() -> None:
    with pytest.raises(EnvoyParseError):
        LiveData.from_payload({"connection": {}})


# --- secctrl, sched, relay ----------------------------------------------------------------------


def test_secctrl() -> None:
    sec = SecCtrl.from_payload(load_json("ivp_ensemble_secctrl.json"))
    assert (sec.soc, sec.state_of_health, sec.available_energy, sec.max_energy) == (
        59,
        100,
        2950,
        5000,
    )
    assert sec.very_low_soc == 10
    assert sec.configured_backup_soc == 100
    assert sec.adjusted_backup_soc == 100


def test_secctrl_malformed() -> None:
    with pytest.raises(EnvoyParseError):
        SecCtrl.from_payload({})


def test_schedule_reference() -> None:
    sched = Schedule.from_payload(load_json("ivp_sc_sched.json"))
    assert sched.mode_index == 6
    assert sched.mode == "Charge From PV"
    assert sched.mode_label == "CP - Charge From PV"
    assert sched.charge_from_grid_allowed is True
    assert sched.reserve_energy == 500
    assert sched.battery_count == 1


@pytest.mark.parametrize(
    ("index", "mode"),
    [(0, "Idle"), (2, "Charge From Grid"), (7, "HEMS Discharge"), (42, None), (-1, None)],
)
def test_schedule_mode_labels(index: int, mode: str | None) -> None:
    data = load_json("ivp_sc_sched.json")
    data["acb_current_mode"] = index
    assert Schedule.from_payload(data).mode == mode


@pytest.mark.parametrize(
    ("admin", "oper", "connected"),
    [("closed", "closed", True), ("closed", "open", False), ("open", "open", False)],
)
def test_relay(admin: str, oper: str, connected: bool) -> None:
    data = load_json("ivp_ensemble_relay.json")
    data.update(mains_admin_state=admin, mains_oper_state=oper)
    relay = Relay.from_payload(data)
    assert relay.grid_connected is connected


# --- inventory, dry contacts, inverters ---------------------------------------------------------


def test_inventory() -> None:
    inv = Inventory.from_payload(load_json("ivp_ensemble_inventory.json"))
    [battery] = inv.batteries
    assert battery.serial == "900000000002"
    assert battery.soc == 59
    # Reported in °C by the battery and °F by the System Controller (spec 5.3).
    assert battery.temperature == 23
    assert battery.max_cell_temperature == 23
    assert battery.status == "ENCHG_STATE_READY"
    assert battery.capacity == 5000
    assert battery.dc_switch_off is False
    assert battery.last_report == datetime.fromtimestamp(1790257720, UTC)
    [controller] = inv.system_controllers
    assert controller.serial == "900000000003"
    assert controller.temperature == 77
    assert controller.communicating


def test_inventory_without_storage() -> None:
    inv = Inventory.from_payload([])
    assert inv.batteries == []
    assert inv.system_controllers == []


def test_dry_contacts() -> None:
    states = parse_dry_contact_states(load_json("ivp_ensemble_dry_contacts.json"))
    assert states == {"NC1": True, "NC2": True, "NO1": False, "NO2": False}
    settings = DryContactSettings.parse_dict(load_json("ivp_ss_dry_contact_settings.json"))
    assert set(settings) == set(states)
    assert settings["NC1"].mode == "manual"
    assert (settings["NC1"].soc_low, settings["NC1"].soc_high) == (30.0, 40.0)


def test_inverters() -> None:
    inverters = Inverter.parse_list(load_json("api_v1_production_inverters.json"))
    assert len(inverters) == 25
    assert len({i.serial for i in inverters}) == 25
    first = inverters[0]
    assert first.serial == "900000000004"
    assert first.last_report is None  # lastReportDate 0 means never
