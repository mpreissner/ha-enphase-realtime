"""The rule that finds phantom meter resets (docs/specs/battery-statistics-repair.md, 3)."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_realtime.const import DOMAIN
from custom_components.enphase_realtime.statistics_repair import phantom_resets

from .conftest import FakeEnphase


def _rows(*hours: tuple[float, float]) -> list[tuple[float, float, float]]:
    """(state, sum) per hour as the rows `phantom_resets` reads."""
    return [(float(i), state, total) for i, (state, total) in enumerate(hours)]


def _corrected(hours: list[tuple[float, float, float]]) -> list[float]:
    """The sums after applying what `phantom_resets` found, as the recorder would."""
    fixes = phantom_resets(hours)
    return [
        pytest.approx(total + sum(amount for start, amount in fixes if start <= hour))
        for hour, _, total in hours
    ]


def test_clean_statistics_need_nothing() -> None:
    assert phantom_resets(_rows((18.0, 0.0), (18.2, 0.2), (18.2, 0.2), (18.5, 0.5))) == []
    assert phantom_resets([]) == []
    assert phantom_resets(_rows((18.0, 0.0))) == []


def test_halved_poll_inside_an_hour() -> None:
    # Hour 1: the counter drops from 18.1 to 9.05 and recovers to 18.2, which adds 18.2 where
    # the battery moved 0.2.
    hours = _rows((18.0, 0.0), (18.2, 18.3), (18.3, 18.4))
    assert phantom_resets(hours) == [(1.0, pytest.approx(-18.1))]
    assert _corrected(hours) == [0.0, 0.2, 0.3]


def test_several_halved_polls_in_an_hour() -> None:
    hours = _rows((18.0, 0.0), (18.0, 54.0), (18.5, 54.5))
    assert phantom_resets(hours) == [(1.0, pytest.approx(-54.0))]


def test_hour_ending_inside_a_halved_poll() -> None:
    # The drop lands in hour 1 and the recovery in hour 2: neither hour's excess is a whole
    # counter, the two together are.
    hours = _rows((18.0, 0.0), (9.0, 9.0), (18.4, 18.4), (18.5, 18.5))
    assert phantom_resets(hours) == [(1.0, pytest.approx(-9.0)), (2.0, pytest.approx(-9.0))]
    assert _corrected(hours) == [0.0, 0.0, 0.4, 0.5]


def test_still_halved_at_the_last_hour_waits() -> None:
    assert phantom_resets(_rows((18.0, 0.0), (9.0, 9.0))) == []


def test_real_meter_reset_is_left_alone() -> None:
    # A replaced meter starts over and stays low; its sum is right as it is.
    hours = _rows((18.0, 0.0), (0.5, 0.5), (1.5, 1.5), (9.0, 9.0), (12.0, 12.0))
    assert phantom_resets(hours) == []


def test_excess_that_is_not_a_whole_counter_is_left_alone() -> None:
    assert phantom_resets(_rows((18.0, 0.0), (18.2, 5.0), (18.3, 5.1))) == []
    assert phantom_resets(_rows((18.0, 0.0), (18.2, 27.2), (18.3, 27.3))) == []


def test_counter_at_zero_is_left_alone() -> None:
    assert phantom_resets(_rows((0.0, 0.0), (0.0, 3.0), (0.0, 3.0))) == []


def test_missing_values_are_skipped() -> None:
    hours = [(0.0, 18.0, 0.0), (1.0, None, None), (2.0, 18.2, 18.3)]
    assert phantom_resets(hours) == [(2.0, pytest.approx(-18.1))]


def test_corrected_statistics_need_nothing_more() -> None:
    hours = _rows((18.0, 0.0), (18.2, 18.3), (9.1, 27.5), (18.4, 36.8), (18.5, 36.9))
    sums = _corrected(hours)
    fixed = [
        (hour, state, total.expected) for (hour, state, _), total in zip(hours, sums, strict=True)
    ]
    assert [round(total, 6) for _, _, total in fixed] == [0.0, 0.2, 0.2, 0.4, 0.5]
    assert phantom_resets(fixed) == []


async def test_without_the_recorder_nothing_is_checked(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert "recorder" not in hass.config.components
    issue_id = f"battery_statistics_{config_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
