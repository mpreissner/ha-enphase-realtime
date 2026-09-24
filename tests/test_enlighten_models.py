"""Enlighten cloud payload parsers, against responses from the app capture."""

from __future__ import annotations

import pytest
from enlighten_client.models import (
    BatterySettings,
    EnlightenParseError,
    GridControlCheck,
    Site,
    SiteSettings,
    parse_search_sites,
)

from tests.helpers import load_json


def test_site_settings_reference() -> None:
    settings = SiteSettings.from_payload(load_json("cloud_site_settings.json"))
    assert settings.country_code == "US"
    assert settings.region == "US"
    assert settings.timezone == "US/Eastern"
    assert settings.locale == "en"
    assert not settings.is_emea
    assert settings.has_encharge
    assert settings.has_enpower
    assert settings.needs_itc_disclaimer


@pytest.mark.parametrize(
    ("show", "restrict", "available"),
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_charge_from_grid_gating(show: bool, restrict: bool, available: bool) -> None:
    payload = load_json("cloud_site_settings.json")
    payload["data"].update(showChargeFromGrid=show, restrictCfg=restrict)
    assert SiteSettings.from_payload(payload).charge_from_grid_available is available


def test_itc_disclaimer_is_us_only() -> None:
    """SYNTHETIC: the reference site re-registered in Germany."""
    payload = load_json("cloud_site_settings.json")
    payload["data"].update(countryCode="DE", region="EU", isEmea=True, timezone="Europe/Berlin")
    settings = SiteSettings.from_payload(payload)
    assert not settings.needs_itc_disclaimer
    assert settings.is_emea


def test_site_settings_unwrapped_body_raises() -> None:
    with pytest.raises(EnlightenParseError):
        SiteSettings.from_payload(load_json("cloud_site_settings.json")["data"])


def test_battery_settings_reference() -> None:
    settings = BatterySettings.from_payload(load_json("cloud_battery_settings.json"))
    assert settings.profile == "backup_only"
    assert settings.backup_percentage == 100
    assert (settings.very_low_soc, settings.very_low_soc_min, settings.very_low_soc_max) == (
        10,
        5,
        25,
    )
    assert settings.charge_from_grid is False
    assert (settings.charge_begin_time, settings.charge_end_time) == (120, 300)
    assert settings.accepted_itc_disclaimer == "2026-09-24T19:13:11.950Z"


def test_no_pending_change_when_requested_config_empty() -> None:
    settings = BatterySettings.from_payload(load_json("cloud_battery_settings.json"))
    assert settings.requested_config == {}
    assert settings.pending_gateways == []
    assert not settings.has_pending_change


def test_no_pending_change_when_requested_config_missing() -> None:
    payload = load_json("cloud_battery_settings.json")
    del payload["data"]["requestedConfig"]
    assert not BatterySettings.from_payload(payload).has_pending_change


def test_pending_change() -> None:
    """SYNTHETIC: the shape of `pendingGateways` hasn't been captured, only its name."""
    payload = load_json("cloud_battery_settings.json")
    payload["data"]["requestedConfig"] = {
        "veryLowSoc": 15,
        "pendingGateways": ["900000000001"],
    }
    settings = BatterySettings.from_payload(payload)
    assert settings.has_pending_change
    assert settings.pending_gateways == ["900000000001"]
    assert settings.requested_config["veryLowSoc"] == 15


def test_search_sites_dedupes() -> None:
    raw = load_json("cloud_search_sites.json")
    assert len(raw["sites"]) == 2
    assert parse_search_sites(raw) == [Site(id=1234567, title="Reference Site")]


def test_search_sites_empty() -> None:
    assert parse_search_sites(load_json("cloud_search_sites_empty.json")) == []


def test_search_sites_malformed() -> None:
    with pytest.raises(EnlightenParseError):
        parse_search_sites({"sites": [{"title": "no id"}]})


def test_grid_control_check_clear() -> None:
    check = GridControlCheck.from_payload(load_json("cloud_grid_control_check.json"))
    assert check.blockers == []


def test_grid_control_check_blocked() -> None:
    payload = load_json("cloud_grid_control_check.json")
    payload["activeDownload"] = True
    payload["gridOutageCheck"] = True
    check = GridControlCheck.from_payload(payload)
    assert check.blockers == ["activeDownload", "gridOutageCheck"]
