"""Enphase overhead: the Envoy's load less a measured backup load (docs/specs/enphase-overhead.md).

The Envoy calculates load as grid + PV + battery, so the equipment's own draw is inside it. A
sensor on the backed-up panel's feed measures the load without it; the difference is the
overhead. This module is the arithmetic alone, free of Home Assistant, so it can be tested as a
function of (time, Envoy load, backup load).
"""

from __future__ import annotations

from collections import deque

# The power entity shows the mean over this window (spec 5).
WINDOW = 60.0
# Longer than this between samples is a gap, not something to integrate across (spec 5).
MAX_GAP = 30.0

_TO_WATTS = {"mW": 1e-3, "W": 1.0, "kW": 1e3, "MW": 1e6, "GW": 1e9}


def to_watts(state: str | None, unit: str | None) -> float | None:
    """A sensor state in W, or None when it isn't a number in a power unit."""
    factor = _TO_WATTS.get(unit or "")
    if factor is None or state is None:
        return None
    try:
        value = float(state)
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value * factor


class Overhead:
    """Samples in, a windowed mean and an energy total out. Times are monotonic seconds."""

    def __init__(self, energy_wh: float = 0.0) -> None:
        self.energy_wh = energy_wh
        self._window: deque[tuple[float, float]] = deque()
        # The previous sample, for the energy sum; None after a missing one.
        self._last: tuple[float, float] | None = None

    def add(self, now: float, envoy_load: float | None, backup_load: float | None) -> None:
        """One live poll. A missing reading is a gap: no sample, and no energy for it."""
        self._expire(now)
        if envoy_load is None or backup_load is None:
            self._last = None
            return
        value = envoy_load - backup_load
        if self._last is not None:
            then, previous = self._last
            if 0 < now - then <= MAX_GAP:
                self.energy_wh += previous * (now - then) / 3600
        self._last = (now, value)
        self._window.append((now, value))

    def mean(self, now: float) -> float | None:
        """Mean over the last `WINDOW` seconds, or None when there is no sample in it."""
        self._expire(now)
        if not self._window:
            return None
        return sum(v for _, v in self._window) / len(self._window)

    def _expire(self, now: float) -> None:
        while self._window and now - self._window[0][0] > WINDOW:
            self._window.popleft()
