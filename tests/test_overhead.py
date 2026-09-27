"""Enphase overhead arithmetic (docs/specs/enphase-overhead.md, section 5)."""

from __future__ import annotations

import pytest
from overhead import (
    BACKUP_SETTLE,
    BACKUP_STALE_AFTER,
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
    o.add(18, 901, 1001)
    assert o.energy_wh == pytest.approx(-0.5)


def test_a_long_gap_adds_nothing() -> None:
    o = Overhead()
    o.add(0, 1100, 1000)
    o.add(MAX_GAP + 1, 1101, 1001)
    assert o.energy_wh == 0
    o.add(MAX_GAP + 19, 1102, 1002)
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
    """Fill the baseline with `watts`; returns the time of the last sample. The loads move, as
    real ones do, so no sample is taken as stale."""
    for i in range(BASELINE):
        o.add(start + i, 1000 + i + watts, 1000 + i)
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
    o.add(t + 2, 6011, 6001)
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
        o.add(t + dt, 3010 + dt, backup + dt)
    assert o.mean(t + 4) == pytest.approx(10)


def test_a_lasting_change_is_accepted_once_steady() -> None:
    o = Overhead()
    t = _warm(o)
    # The overhead really rises by 400 W: rejected at first, then accepted as the new baseline.
    end = int(REBASE_AFTER) + MIN_BASELINE + 2
    for i in range(1, end):
        o.add(t + i, 1410 + i, 1000 + i)
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


def test_a_repeated_envoy_load_is_skipped() -> None:
    o = Overhead()
    o.add(0, 1100, 1000)
    # The Envoy holds 1100 W while the backup load moves: the difference is meaningless.
    o.add(1, 1100, 1060)
    o.add(2, 1100, 950)
    assert o.stale_since == 1
    assert o.mean(2) == pytest.approx(100)
    o.add(3, 1101, 1000)
    assert o.stale_since is None
    assert o.mean(3) == pytest.approx(100.5)


def test_energy_carries_across_a_short_stall() -> None:
    o = Overhead()
    o.add(0, 1100, 1000)
    for i in range(1, 18):
        o.add(i, 1100, 1000 + i * 10)
    o.add(18, 1101, 1001)
    assert o.energy_wh == pytest.approx(0.5)


def test_a_long_stall_adds_nothing() -> None:
    o = Overhead()
    o.add(0, 1100, 1000)
    for i in range(1, int(MAX_GAP) + 2):
        o.add(i, 1100, 1000)
    o.add(MAX_GAP + 2, 1101, 1001)
    assert o.energy_wh == 0


def test_a_stall_is_not_taken_as_a_new_level() -> None:
    o = Overhead()
    t = _warm(o)
    # The backup load steps up 2 kW while the Envoy is stalled; the held value must not become
    # the baseline however long the stall lasts.
    o.add(t + 1, 1500, 1000)
    for i in range(2, int(REBASE_AFTER + 2 * STEADY)):
        o.add(t + i, 1500, 3000)
    assert o.mean(t + REBASE_AFTER + 2 * STEADY) < 30


def test_a_stale_backup_load_is_a_gap() -> None:
    o = Overhead()
    t = _warm(o)
    energy = o.energy_wh
    # The backup load stopped changing BACKUP_STALE_AFTER s ago: its value is not trusted.
    o.add(t + 1, 1500, 1000, backup_age=BACKUP_STALE_AFTER + 1)
    assert o.backup_bad_at == t + 1
    assert o.mean(t + 1) == pytest.approx(10)
    assert o.energy_wh == energy


def test_samples_resume_once_the_backup_load_has_settled() -> None:
    o = Overhead()
    t = _warm(o)
    o.add(t + 1, 1500, 1000, backup_age=BACKUP_STALE_AFTER + 1)
    energy = o.energy_wh
    # Fresh again, but a backlog can still be replaying: the samples wait BACKUP_SETTLE s.
    for i in range(2, int(BACKUP_SETTLE) + 1):
        o.add(t + i, 1500 + i, 1000 + i)
        assert o.backup_bad_at == t + 1
    assert o.energy_wh == energy
    assert o.mean(t + BACKUP_SETTLE) == pytest.approx(10)
    o.add(t + 1 + BACKUP_SETTLE, 1010, 1000)
    assert o.backup_bad_at is None
    o.add(t + 2 + BACKUP_SETTLE, 1011, 1001)
    assert o.energy_wh == pytest.approx(energy + 10 / 3600)


def test_a_missing_backup_load_starts_the_settling() -> None:
    o = Overhead()
    t = _warm(o)
    o.add(t + 1, 1500, None)
    o.add(t + 2, 1500, 1000)
    assert o.backup_bad_at == t + 1
    assert o.mean(t + 2) == pytest.approx(10)


def test_a_missing_envoy_load_does_not_start_the_settling() -> None:
    o = Overhead()
    t = _warm(o)
    o.add(t + 1, None, 1000)
    assert o.backup_bad_at is None
    o.add(t + 2, 1030, 1000)
    o.add(t + 3, 1031, 1001)
    assert o.mean(t + 3) == pytest.approx((10 * BASELINE + 30 + 30) / (BASELINE + 2))


def test_a_backup_load_changing_within_the_limit_is_used() -> None:
    o = Overhead()
    t = _warm(o)
    o.add(t + 1, 1030, 1000, backup_age=BACKUP_STALE_AFTER)
    assert o.backup_bad_at is None
    assert o.mean(t + 1) == pytest.approx((10 * BASELINE + 30) / (BASELINE + 1))
