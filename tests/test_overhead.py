"""Enphase overhead arithmetic (docs/specs/enphase-overhead.md, section 5)."""

from __future__ import annotations

import pytest
from overhead import (
    BASELINE,
    MAX_GAP,
    MIN_BASELINE,
    REBASE_AFTER,
    REJECT,
    STEADY,
    WINDOW,
    Overhead,
    to_watts,
)


@pytest.mark.parametrize(
    ("state", "unit", "expected"),
    [
        ("1200", "W", 1200.0),
        ("1.2", "kW", 1200.0),
        ("-0.5", "kW", -500.0),
        ("1200000", "mW", 1200.0),
        ("0.0012", "MW", 1200.0),
        ("unavailable", "W", None),
        ("unknown", "W", None),
        ("nan", "W", None),
        ("inf", "W", None),
        ("1200", "Wh", None),
        ("1200", None, None),
        (None, "W", None),
    ],
)
def test_to_watts(state: str | None, unit: str | None, expected: float | None) -> None:
    assert to_watts(state, unit) == expected


def test_mean_over_the_window() -> None:
    o = Overhead()
    o.add(0, 1100, 1000)
    o.add(1, 1130, 1100)
    assert o.mean(1) == pytest.approx(65)


def test_old_samples_leave_the_window() -> None:
    o = Overhead()
    o.add(0, 1100, 1000)
    o.add(WINDOW, 1050, 1000)
    assert o.mean(WINDOW) == pytest.approx(75)
    assert o.mean(WINDOW + 0.5) == pytest.approx(50)
    assert o.mean(2 * WINDOW + 1) is None


def test_empty_window_is_none() -> None:
    assert Overhead().mean(0) is None


def test_energy_is_a_left_sum() -> None:
    o = Overhead(energy_wh=10)
    o.add(0, 1100, 1000)  # 100 W
    o.add(18, 1200, 1000)  # 100 W for 18 s = 0.5 Wh
    o.add(36, 1000, 1000)  # 200 W for 18 s = 1 Wh
    assert o.energy_wh == pytest.approx(11.5)


def test_negative_samples_are_counted() -> None:
    o = Overhead()
    o.add(0, 900, 1000)
    o.add(18, 900, 1000)
    assert o.energy_wh == pytest.approx(-0.5)


def test_a_long_gap_adds_nothing() -> None:
    o = Overhead()
    o.add(0, 1100, 1000)
    o.add(MAX_GAP + 1, 1100, 1000)
    assert o.energy_wh == 0
    o.add(MAX_GAP + 19, 1100, 1000)
    assert o.energy_wh == pytest.approx(0.5)


@pytest.mark.parametrize(("load", "backup"), [(None, 1000), (1100, None)])
def test_a_missing_reading_is_a_gap(load: float | None, backup: float | None) -> None:
    o = Overhead()
    o.add(0, 1100, 1000)
    o.add(1, load, backup)
    o.add(2, 1100, 1000)
    assert o.energy_wh == 0
    # The missing reading isn't a sample, but the one before it stays in the window.
    assert o.mean(2) == pytest.approx(100)


def _warm(o: Overhead, watts: float = 10, start: float = 0) -> float:
    """Fill the baseline with `watts`; returns the time of the last sample."""
    for i in range(BASELINE):
        o.add(start + i, 1000 + watts, 1000)
    return start + BASELINE - 1


def test_the_first_samples_are_taken_as_they_are() -> None:
    o = Overhead()
    samples = [10, 900] * MIN_BASELINE
    for i, watts in enumerate(samples[:MIN_BASELINE]):
        o.add(i, 1000 + watts, 1000)
    assert o.rejecting_since is None
    assert o.mean(MIN_BASELINE) == pytest.approx(sum(samples[:MIN_BASELINE]) / MIN_BASELINE)


def test_an_outlier_is_replaced_by_the_baseline() -> None:
    o = Overhead()
    t = _warm(o)
    # A 5 kW step the Envoy saw a second before the backup sensor.
    o.add(t + 1, 6010, 1000)
    o.add(t + 2, 6010, 6000)
    assert o.mean(t + 2) == pytest.approx(10)
    assert o.rejecting_since is None


def test_a_rejected_sample_counts_as_the_baseline_for_energy() -> None:
    o = Overhead()
    t = _warm(o)
    before = o.energy_wh
    o.add(t + 18, 1000 - 4000, 1000)  # backup sensor ahead by 4 kW
    o.add(t + 36, 1010, 1000)
    assert o.energy_wh - before == pytest.approx(10 * 36 / 3600)


def test_differences_within_the_limit_are_accepted() -> None:
    o = Overhead()
    t = _warm(o)
    o.add(t + 1, 1000 + 10 + REJECT - 1, 1000)
    assert o.rejecting_since is None


def test_a_step_reported_as_a_ramp_is_rejected_throughout() -> None:
    o = Overhead()
    t = _warm(o)
    # The Envoy takes the 2 kW step at once; the backup sensor ramps in three updates.
    for dt, backup in ((1, 1000), (2, 1400), (3, 2200), (4, 3000)):
        o.add(t + dt, 3010, backup)
    assert o.mean(t + 4) == pytest.approx(10)


def test_a_lasting_change_is_accepted_once_steady() -> None:
    o = Overhead()
    t = _warm(o)
    # The overhead really rises by 400 W: rejected at first, then accepted as the new baseline.
    end = int(REBASE_AFTER) + MIN_BASELINE + 2
    for i in range(1, end):
        o.add(t + i, 1410, 1000)
        assert (o.rejecting_since is None) == (i > REBASE_AFTER)
    # The old level is now the outlier.
    o.add(t + end, 1010, 1000)
    assert o.rejecting_since == t + end


def test_an_unsteady_difference_is_not_accepted() -> None:
    o = Overhead()
    t = _warm(o)
    for i in range(1, int(REBASE_AFTER + 2 * STEADY)):
        o.add(t + i, 1000 + (400 if i % 2 else 800), 1000)
    assert o.rejecting_since == t + 1
    assert o.mean(t + REBASE_AFTER + 2 * STEADY) == pytest.approx(10)


def test_the_baseline_survives_a_gap() -> None:
    o = Overhead()
    t = _warm(o)
    o.add(t + 1, None, 1000)
    # Polling resumes a minute later in the middle of a step.
    o.add(t + 61, 3010, 1000)
    assert o.rejecting_since == t + 61
