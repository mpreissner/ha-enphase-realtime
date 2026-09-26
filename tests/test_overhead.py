"""Enphase overhead arithmetic (docs/specs/enphase-overhead.md, section 5)."""

from __future__ import annotations

import pytest
from overhead import MAX_GAP, WINDOW, Overhead, to_watts


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
