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
    BatteryPower,
    DryContactSettings,
    EnvoyInfo,
    EnvoyParseError,
    ExportLimit,
    Inventory,
    Inverter,
    InverterDetail,
    LifetimeEnergy,
    LiveData,
    Meter,
    PcsSettings,
    PhaseLayout,
    ProductionReport,
    Relay,
    Schedule,
    SecCtrl,
    detect_phase_layout,
    firmware_version,
    metered_phases,
    parse_ct_meters,
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
    assert sched.charge_from_grid_allowed is True
    assert sched.reserve_energy == 500
    assert sched.battery_count == 1
    assert sched.mode == "Charge From PV"


@pytest.mark.parametrize(
    ("index", "mode"),
    [(2, "Charge From Grid"), (10, "HEMS Charge from Grid"), (11, None), (-1, None), (None, None)],
)
def test_schedule_mode(index: int | None, mode: str | None) -> None:
    data = load_json("ivp_sc_sched.json")
    data["acb_current_mode"] = index
    assert Schedule.from_payload(data).mode == mode


def test_schedule_mode_without_keys() -> None:
    data = load_json("ivp_sc_sched.json")
    del data["sched_mode_key"]
    assert Schedule.from_payload(data).mode is None


@pytest.mark.parametrize(
    ("admin", "oper", "connected", "settled"),
    [
        ("closed", "closed", True, True),
        ("closed", "open", False, False),
        ("open", "closed", True, False),
        ("open", "open", False, True),
        # What the reference site reported while off grid (FINDINGS 2026-10-01).
        ("open", "open synchronizing", False, True),
        ("open", "open synchronized", False, True),
        ("closed", "open synchronized", False, False),
    ],
)
def test_relay(admin: str, oper: str, connected: bool, settled: bool) -> None:
    data = load_json("ivp_ensemble_relay.json")
    data.update(mains_admin_state=admin, mains_oper_state=oper)
    relay = Relay.from_payload(data)
    assert relay.grid_connected is connected
    assert relay.settled is settled


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


def test_inventory_unknown_values() -> None:
    """After an Envoy reboot the battery reported "unknown" temperatures (FINDINGS)."""
    data = load_json("ivp_ensemble_inventory.json")
    for group in data:
        for d in group.get("devices", []):
            d.update(temperature="unknown", maxCellTemp="unknown", percentFull=None)
    inv = Inventory.from_payload(data)
    [battery] = inv.batteries
    assert battery.temperature is None
    assert battery.max_cell_temperature is None
    assert battery.soc is None
    [controller] = inv.system_controllers
    assert controller.temperature is None


def test_inventory_without_storage() -> None:
    inv = Inventory.from_payload([])
    assert inv.batteries == []
    assert inv.system_controllers == []
    assert inv.collars == []
    assert inv.combiner_controllers == []


def test_inventory_with_a_collar() -> None:
    """pyenphase's capture of a site with an IQ Meter Collar and an IQ Combiner 6C, no System
    Controller (tests/fixtures/README.md)."""
    inv = Inventory.from_payload(load_json("ivp_ensemble_inventory.json", "collar"))
    assert inv.system_controllers == []
    # Its batteries report the same fields as the reference site's IQ Battery 5P.
    assert [b.serial for b in inv.batteries] == ["910000000001", "910000000002"]
    assert inv.batteries[0].soc == 92
    assert inv.batteries[0].status == "ENCMN_MDE_ENCHARGE_READY"
    assert inv.batteries[0].capacity == 5000
    [collar] = inv.collars
    assert collar.serial == "910000000003"
    assert collar.firmware == "3.0.6-D0"
    assert collar.temperature == 42
    assert collar.communicating
    assert collar.last_report == datetime.fromtimestamp(1752939759, UTC)
    assert collar.status == "ENCMN_MDE_ON_GRID"
    assert collar.mid_state == "close"
    assert collar.grid_state == "on_grid"
    assert collar.control_error == 0
    assert collar.collar_state == "Installed"
    [combiner] = inv.combiner_controllers
    assert combiner.serial == "910000000004"
    assert combiner.firmware == "0.1.20-D1"
    assert combiner.communicating
    assert combiner.last_report == datetime.fromtimestamp(1752945451, UTC)
    assert combiner.status == "ENCMN_C6_CC_READY"


def test_collar_with_missing_fields() -> None:
    """Firmware may drop or rename fields; the collar's device stays, with unknown values."""
    inv = Inventory.from_payload([{"type": "COLLAR", "devices": [{"serial_num": "910000000003"}]}])
    [collar] = inv.collars
    assert collar.temperature is None
    assert collar.mid_state is None
    assert collar.control_error is None
    assert not collar.communicating


def test_dry_contacts() -> None:
    states = parse_dry_contact_states(load_json("ivp_ensemble_dry_contacts.json"))
    assert states == {"NC1": True, "NC2": True, "NO1": False, "NO2": False}
    settings = DryContactSettings.parse_dict(load_json("ivp_ss_dry_contact_settings.json"))
    assert set(settings) == set(states)
    assert settings["NC1"].mode == "manual"
    assert (settings["NC1"].soc_low, settings["NC1"].soc_high) == (30.0, 40.0)
    # Kept whole for writes, strings and all (docs/specs/dry-contacts.md 2).
    raw = load_json("ivp_ss_dry_contact_settings.json")["dry_contacts"][0]
    assert settings["NC1"].raw == raw
    assert settings["NC1"].raw["override"] == "false"
    assert settings["NC1"].manual_override is True  # sent as "true"
    flags = [
        DryContactSettings.parse_dict({"dry_contacts": [{"id": "X", "manual_override": v}]})["X"]
        for v in ("false", True, "yes", None)
    ]
    assert [f.manual_override for f in flags] == [False, True, None, None]


def test_export_limit() -> None:
    pel = ExportLimit.from_payload(load_json("ivp_ss_pel_settings.json"))
    assert (pel.enabled, pel.soft, pel.hard) == (True, True, False)
    assert pel.mode == "soft"
    assert pel.limit == 0.0
    assert pel.limit_type == "Aggregate"
    assert not pel.percent
    # SYNTHETIC: other combinations.
    assert ExportLimit.from_payload({"PEL": False, "Soft_PEL": True}).mode == "off"
    assert ExportLimit.from_payload({"PEL": True, "Hard_PEL": True}).mode == "hard"
    both = {"PEL": True, "Hard_PEL": True, "Soft_PEL": True}
    assert ExportLimit.from_payload(both).mode == "soft_and_hard"
    with pytest.raises(EnvoyParseError):
        ExportLimit.from_payload({})


def test_pcs_settings() -> None:
    pcs = PcsSettings.from_payload(load_json("ivp_ss_pcs_settings.json"))
    assert pcs.offerings == {
        "PVOversubscription": False,
        "EnchargeOversubscription": False,
        "EVSEMBTAvoidance": False,
        "MPUAvoidance": True,
        "BusbarPCS": False,
    }
    assert (pcs.main_breaker, pcs.main_busbar, pcs.der_breaker) == (200.0, 200.0, 40.0)
    assert pcs.consumption_meter_location == "Between_Mains_Supply_and_Main_Load_Panel"
    with pytest.raises(EnvoyParseError):
        PcsSettings.from_payload({"mainCircuitBreaker": 200.0})


def test_inverters() -> None:
    inverters = Inverter.parse_list(load_json("api_v1_production_inverters.json"))
    assert len(inverters) == 25
    assert len({i.serial for i in inverters}) == 25
    first = inverters[0]
    assert first.serial == "900000000004"
    assert first.last_report is None  # lastReportDate 0 means never


# --- What the core integration's entities read (docs/specs/core-entity-parity.md) ---------------


@pytest.mark.parametrize(
    ("firmware", "version"),
    [("D8.3.6086", (8, 3, 6086)), ("R4.10.35", (4, 10, 35)), ("8", (8,)), ("x", ()), ("", ())],
)
def test_firmware_version(firmware: str, version: tuple[int, ...]) -> None:
    assert firmware_version(firmware) == version


def test_meter_status_fields() -> None:
    meters = Meter.parse_list(load_json("ivp_meters.json"))
    assert meters[0].metering_status == "normal"
    assert meters[0].status_flags == ()
    data = load_json("ivp_meters.json")
    data[0].update(meteringStatus="check-wiring", statusFlags=["production-imbalance"])
    del data[1]["meteringStatus"], data[1]["statusFlags"]
    flagged, bare, *_ = Meter.parse_list(data)
    assert flagged.metering_status == "check-wiring"
    assert flagged.status_flags == ("production-imbalance",)
    assert bare.metering_status is None
    assert bare.status_flags is None


def test_metered_phases() -> None:
    assert metered_phases(Meter.parse_list(load_json("ivp_meters.json"))) == ("ph-a", "ph-b")
    assert metered_phases(_synthetic_meters(phaseCount=3)) == ("ph-a", "ph-b", "ph-c")
    # A single-phase site has no per-phase entities, as in pyenphase.
    assert metered_phases(_synthetic_meters(phaseCount=1)) == ()
    assert metered_phases([]) == ()


def test_ct_meters_reference() -> None:
    meters = Meter.parse_list(load_json("ivp_meters.json"))
    cts = parse_ct_meters(meters, load_json("ivp_meters_readings.json"), "D8.3.6086")
    # Only the enabled CTs; the disabled and unmapped eids' all-zero readings are skipped.
    assert set(cts) == {"production", "net-consumption", "storage"}

    net = cts["net-consumption"]
    assert net.total is not None
    assert net.total.energy_delivered == 1124885
    assert net.total.energy_received == 371245
    assert net.total.metering_status == "normal"
    assert net.total.status_flags == ()
    assert list(net.phases) == ["ph-a", "ph-b"]
    first = net.phases["ph-a"]
    assert first is not None
    assert first.energy_delivered == 512411
    assert first.voltage == pytest.approx(120, abs=10)

    # Both storage legs carry energy, so the one-channel guard leaves them alone.
    storage = cts["storage"]
    assert storage.total is not None
    assert (storage.total.energy_delivered, storage.total.energy_received) == (626, 13560)
    assert all(leg is not None for leg in storage.phases.values())


def _one_channel_storage_readings() -> Any:
    """SYNTHETIC: the reference readings with the storage CT's second leg reading nothing."""
    readings = load_json("ivp_meters_readings.json")
    storage = next(r for r in readings if r["eid"] == 704643840)
    live, dead = storage["channels"][0], storage["channels"][1]
    live.update(actEnergyDlvd=storage["actEnergyDlvd"], actEnergyRcvd=storage["actEnergyRcvd"])
    dead.update(actEnergyDlvd=0.0, actEnergyRcvd=0.0)
    return readings


def test_one_channel_storage_ct_is_blanked() -> None:
    """Firmware 8.3.6xxx: the total and the dead leg mean nothing (pyenphase does the same)."""
    meters = Meter.parse_list(load_json("ivp_meters.json"))
    storage = parse_ct_meters(meters, _one_channel_storage_readings(), "D8.3.6086")["storage"]
    assert storage.total is None
    assert storage.phases["ph-a"] is not None
    assert storage.phases["ph-b"] is None
    # The other CTs are untouched.
    cts = parse_ct_meters(meters, _one_channel_storage_readings(), "D8.3.6086")
    assert cts["production"].total is not None


def test_one_channel_storage_guard_needs_the_firmware_and_split_phase() -> None:
    readings = _one_channel_storage_readings()
    meters = Meter.parse_list(load_json("ivp_meters.json"))
    for firmware in ("D8.2.4345", ""):
        assert parse_ct_meters(meters, readings, firmware)["storage"].total is not None
    three_phase = _synthetic_meters(phaseMode="three", phaseCount=3)
    assert parse_ct_meters(three_phase, readings, "D8.3.6086")["storage"].total is not None


def test_ct_meters_malformed() -> None:
    meters = Meter.parse_list(load_json("ivp_meters.json"))
    with pytest.raises(EnvoyParseError):
        parse_ct_meters(meters, [{"eid": 704643328}])


def test_production_report_reference() -> None:
    meters = Meter.parse_list(load_json("ivp_meters.json"))
    report = ProductionReport.from_payload(load_json("production_details.json"), meters)
    assert report.production is not None
    assert report.production.energy_lifetime == 742844
    assert report.consumption is not None
    assert report.consumption.power == 1133
    assert report.consumption.energy_lifetime == 1496557
    assert report.net_consumption is not None
    assert report.net_consumption.energy_lifetime == 753713
    assert report.has_consumption and report.has_net_consumption
    assert report.active_phase_count == 2
    for phases in (
        report.production_phases,
        report.consumption_phases,
        report.net_consumption_phases,
    ):
        assert list(phases) == ["ph-a", "ph-b"]
    assert report.consumption_phases["ph-a"].energy_lifetime == 689968


def test_production_report_without_a_production_ct_uses_the_inverters() -> None:
    """SYNTHETIC: the eim section idle and the production CT disabled."""
    data = load_json("production_details.json")
    eim = next(p for p in data["production"] if p["type"] == "eim")
    eim["activeCount"] = 0
    meters_data = load_json("ivp_meters.json")
    meters_data[0]["state"] = "disabled"
    report = ProductionReport.from_payload(data, Meter.parse_list(meters_data))
    assert report.production is not None
    assert report.production.energy_lifetime == 720423
    # With the CT enabled but idle the inverters' count is no substitute, and there is none.
    report = ProductionReport.from_payload(data, Meter.parse_list(load_json("ivp_meters.json")))
    assert report.production is None


def test_production_report_skips_inactive_consumption() -> None:
    data = load_json("production_details.json")
    for section in data["consumption"]:
        section["activeCount"] = 0
    meters = Meter.parse_list(load_json("ivp_meters.json"))
    report = ProductionReport.from_payload(data, meters)
    assert report.consumption is None and report.net_consumption is None
    assert not report.has_consumption and not report.has_net_consumption
    assert report.consumption_phases == {}


def _total_is_net(data: Any) -> Any:
    """SYNTHETIC: total-consumption repeating net-consumption, the firmware 8.3.5433 fault."""
    total, net = data["consumption"]
    for target, source in [(total, net), *zip(total["lines"], net["lines"], strict=True)]:
        target.update(wNow=source["wNow"], whLifetime=source["whLifetime"])
    return data


def test_total_consumption_repeating_net_is_repaired() -> None:
    meters = Meter.parse_list(load_json("ivp_meters.json"))
    data = _total_is_net(load_json("production_details.json"))
    data["production"][1]["wNow"] = 500.0
    report = ProductionReport.from_payload(data, meters, "D8.3.6086")
    assert report.consumption is not None and report.production is not None
    assert report.consumption.energy_lifetime == 753713 + 742844
    assert report.consumption.power == 1133 + 500
    produced = report.production_phases["ph-a"].energy_lifetime
    assert report.consumption_phases["ph-a"].energy_lifetime == 318881 + produced
    # Older firmware doesn't have the fault, so the figures stand.
    report = ProductionReport.from_payload(data, meters, "D7.6.175")
    assert report.consumption is not None
    assert report.consumption.energy_lifetime == 753713


def test_total_consumption_is_dropped_when_it_cannot_be_repaired() -> None:
    data = _total_is_net(load_json("production_details.json"))
    data["production"][1]["activeCount"] = 0  # the production CT is enabled but idle
    meters = Meter.parse_list(load_json("ivp_meters.json"))
    report = ProductionReport.from_payload(data, meters, "D8.3.6086")
    assert report.production is None
    assert report.consumption is None
    assert report.consumption_phases == {}
    assert report.net_consumption is not None


def test_production_report_malformed() -> None:
    with pytest.raises(EnvoyParseError):
        ProductionReport.from_payload([], [])


def test_inverter_details_reference() -> None:
    """Captured at night: every inverter is listed and none has a last reading."""
    details = InverterDetail.parse_dict(load_json("ivp_pdm_device_data.json"))
    assert len(details) == 25
    first = details["900000000004"]
    assert first.serial == "900000000004"
    assert first.lifetime_energy == 34745
    assert first.energy_today == 0
    for value in (
        first.dc_voltage,
        first.dc_current,
        first.ac_voltage,
        first.ac_current,
        first.ac_frequency,
        first.temperature,
        first.energy_produced,
        first.last_report_duration,
    ):
        assert value is None


def test_inverter_details_last_reading() -> None:
    """SYNTHETIC: a daytime `lastReading`, with field names and scale as pyenphase reads them."""
    data = load_json("ivp_pdm_device_data.json")
    device = next(d for d in data.values() if isinstance(d, dict) and d.get("devName") == "pcu")
    device["channels"][0]["lastReading"] = {
        "endDate": 1790258158,
        "duration": 903,
        "flags": 0,
        "flags_hex": "0x0000000000000000",
        "joulesProduced": 164443,
        "acVoltageINmV": 244566,
        "acFrequencyINmHz": 60000,
        "dcVoltageINmV": 37570,
        "dcCurrentINmA": 5000,
        "channelTemp": 31,
        "pwrConvErrSecs": 0,
        "pwrConvMaxErrCycles": 0,
        "joulesUsed": 0,
        "leadingVArs": 0,
        "laggingVArs": 0,
        "acCurrentInmA": 730,
        "l1NAcVoltageInmV": 0,
        "l2NAcVoltageInmV": 0,
        "l3NAcVoltageInmV": 0,
        "rssi": 0,
        "issi": 0,
    }
    detail = InverterDetail.parse_dict(data)[device["sn"]]
    assert detail.dc_voltage == 37.57
    assert detail.dc_current == 5.0
    assert detail.ac_voltage == 244.566
    assert detail.ac_current == 0.73
    assert detail.ac_frequency == 60.0
    assert detail.temperature == 31
    assert detail.last_report_duration == 903
    assert detail.energy_produced == round(164443 / 903 / 3.6, 3)


def test_inverter_details_filtering() -> None:
    data = load_json("ivp_pdm_device_data.json")
    serials = [d["sn"] for d in data.values() if isinstance(d, dict) and d.get("devName") == "pcu"]
    inactive, nameless = serials[0], serials[1]
    for device in data.values():
        if isinstance(device, dict) and device.get("sn") == inactive:
            device["active"] = False
        if isinstance(device, dict) and device.get("sn") == nameless:
            del device["sn"]
    details = InverterDetail.parse_dict(data)
    assert len(details) == 23
    assert inactive not in details

    # At its device limit the Envoy truncates the list, so none of it is used (as pyenphase).
    data["deviceDataLimit"] = data["deviceCount"]
    assert InverterDetail.parse_dict(data) == {}
    with pytest.raises(EnvoyParseError):
        InverterDetail.parse_dict({"deviceCount": 1})


def test_battery_power() -> None:
    power = BatteryPower.parse_dict(load_json("ivp_ensemble_power.json"))
    assert power == {"900000000002": BatteryPower("900000000002", 23.0, 23.0)}
    with pytest.raises(EnvoyParseError):
        BatteryPower.parse_dict({"devices": []})
