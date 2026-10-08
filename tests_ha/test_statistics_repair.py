"""The lifetime battery energy statistics repair (docs/specs/battery-statistics-repair.md)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.components.recorder import Recorder, get_instance, migration
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import recorder as recorder_helper
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from sqlalchemy.orm.session import Session

from custom_components.enphase_realtime.const import DOMAIN
from custom_components.enphase_realtime.statistics_repair import (
    RECHECK_AFTER,
    async_check,
)

from .conftest import SERIAL, FakeEnphase

CHARGED = f"sensor.envoy_{SERIAL}_lifetime_battery_energy_charged"
DISCHARGED = f"sensor.envoy_{SERIAL}_lifetime_battery_energy_discharged"
START = datetime(2026, 10, 2, tzinfo=UTC)

# The recorder fixture autospecs these modules' functions, which on Python 3.14 evaluates their
# annotations, and they import these two names for type checking only.
migration.Recorder = Recorder  # type: ignore[attr-defined]
recorder_helper.Session = Session  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    fake: FakeEnphase, recorder_mock: Any, enable_custom_integrations: None
) -> None:
    """Replaces conftest's: the recorder has to be set up before `hass` is.

    The fake comes first so that it outlives `hass`, whose teardown waits on the recorder long
    enough for a poll to run.
    """


async def _import(hass: HomeAssistant, statistic_id: str, *hours: tuple[float, float]) -> None:
    async_import_statistics(
        hass,
        {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": None,
            "source": "recorder",
            "statistic_id": statistic_id,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        [
            {"start": START + timedelta(hours=i), "state": state, "sum": total}
            for i, (state, total) in enumerate(hours)
        ],
    )
    await async_wait_recording_done(hass)


async def _sums(hass: HomeAssistant, statistic_id: str) -> list[float]:
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        START,
        None,
        {statistic_id},
        "hour",
        {"energy": "kWh"},
        {"sum"},
    )
    return [round(row["sum"], 6) for row in stats[statistic_id]]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"battery_statistics_{entry.entry_id}")


async def test_repair_is_offered_and_fixes_the_sums(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    fake: FakeEnphase,
    config_entry: MockConfigEntry,
) -> None:
    await _import(hass, CHARGED, (18.0, 100.0), (18.2, 118.3), (9.1, 127.5), (18.4, 136.8))
    await _import(hass, DISCHARGED, (0.7, 5.0), (0.7, 6.4), (0.7, 6.4), (0.7, 6.4))
    assert await async_setup_component(hass, "repairs", {})
    await _setup(hass, config_entry)

    issue = _issue(hass, config_entry)
    assert issue is not None
    assert issue.is_fixable
    # Offered only: nothing changes before the user confirms.
    assert await _sums(hass, CHARGED) == [100.0, 118.3, 127.5, 136.8]

    client = await hass_client()
    response = await client.post(
        "/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": issue.issue_id}
    )
    form = await response.json()
    assert form["step_id"] == "confirm"
    assert form["description_placeholders"] == {"charged": "36.4", "discharged": "1.4"}
    assert await _sums(hass, CHARGED) == [100.0, 118.3, 127.5, 136.8]

    response = await client.post(f"/api/repairs/issues/fix/{form['flow_id']}", json={})
    assert (await response.json())["type"] == "create_entry"
    await hass.async_block_till_done()
    assert await _sums(hass, CHARGED) == [100.0, 100.2, 100.2, 100.4]
    assert await _sums(hass, DISCHARGED) == [5.0, 5.0, 5.0, 5.0]
    assert _issue(hass, config_entry) is None

    await async_check(hass, config_entry)
    assert _issue(hass, config_entry) is None


async def test_no_repair_for_clean_statistics(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _import(hass, CHARGED, (18.0, 100.0), (18.2, 100.2), (18.4, 100.4))
    await _setup(hass, config_entry)
    assert _issue(hass, config_entry) is None


async def test_damage_compiled_after_setup_is_found_by_the_second_check(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    await _import(hass, CHARGED, (18.0, 100.0), (18.2, 100.2))
    await _setup(hass, config_entry)
    assert _issue(hass, config_entry) is None

    await _import(hass, CHARGED, (18.0, 100.0), (18.2, 100.2), (18.3, 118.5))
    async_fire_time_changed(hass, datetime.now(UTC) + RECHECK_AFTER + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert _issue(hass, config_entry) is not None

    # A repaired or clean check takes the issue away again.
    await _import(hass, CHARGED, (18.0, 100.0), (18.2, 100.2), (18.3, 100.3))
    await async_check(hass, config_entry)
    assert _issue(hass, config_entry) is None
