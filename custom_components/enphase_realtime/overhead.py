"""Enphase overhead: the Envoy's load less a measured backup load (docs/specs/enphase-overhead.md).

The Envoy calculates load as grid + PV + battery, so the equipment's own draw is inside it. A
sensor on the backed-up panel's feed measures the load without it; the difference is the
overhead. This module is the arithmetic alone, free of Home Assistant, so it can be tested as a
function of (time, Envoy load, backup load).
"""

from __future__ import annotations

from collections import deque
from statistics import median

# The power entity shows the mean over this window (spec 5).
WINDOW = 300.0
# Longer than this between samples is a gap, not something to integrate across (spec 5).
MAX_GAP = 30.0

# Outlier rejection (spec 6). A sample further than REJECT from the baseline, the median of the
# last BASELINE accepted samples, is a timing artifact: the two meters caught a step at
# different moments. The baseline stands in for it.
REJECT = 150.0
BASELINE = 30
# The first samples are taken as they are, until the baseline has this many.
MIN_BASELINE = 5
# After REBASE_AFTER s of rejections, a difference that has held within REJECT for STEADY s is
# real (the overhead itself changed), so it is accepted and the baseline starts again from it.
REBASE_AFTER = 20.0
STEADY = 5.0

# Stale snapshots (spec 6). During a stall the Envoy keeps answering, with a fresh
# `meters.last_update` but the same power values; a real load doesn't repeat to the milliwatt.
# A poll whose Envoy load equals the previous one is skipped.

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
        # Accepted raw differences; kept across gaps, so a restart of polling mid-step is judged
        # against the old baseline rather than taken as the new one.
        self._accepted: deque[float] = deque(maxlen=BASELINE)
        # Raw differences over the last STEADY s, rejected or not.
        self._recent: deque[tuple[float, float]] = deque()
        # When the current run of rejections began; None while samples are accepted.
        self.rejecting_since: float | None = None
        # The previous poll's Envoy load, and when the current run of repeats began.
        self._envoy_load: float | None = None
        self.stale_since: float | None = None

    def add(self, now: float, envoy_load: float | None, backup_load: float | None) -> None:
        """One live poll. A missing reading is a gap: no sample, and no energy for it. A stale
        one is skipped: the previous sample stands, and carries the energy across a stall no
        longer than MAX_GAP."""
        self._expire(now)
        stale = envoy_load is not None and envoy_load == self._envoy_load
        self._envoy_load = envoy_load
        if stale:
            if self.stale_since is None:
                self.stale_since = now
            return
        self.stale_since = None
        if envoy_load is None or backup_load is None:
            self._last = None
            return
        value = self._filter(now, envoy_load - backup_load)
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

    def _filter(self, now: float, raw: float) -> float:
        """The raw difference, or the baseline in its place when it is an outlier."""
        self._recent.append((now, raw))
        while now - self._recent[0][0] > STEADY:
            self._recent.popleft()
        if len(self._accepted) < MIN_BASELINE:
            return self._accept(raw)
        baseline = median(self._accepted)
        if abs(raw - baseline) < REJECT:
            return self._accept(raw)
        if self.rejecting_since is None:
            self.rejecting_since = now
        elif now - self.rejecting_since >= REBASE_AFTER and self._steady():
            self._accepted.clear()
            return self._accept(raw)
        return baseline

    def _steady(self) -> bool:
        values = [v for _, v in self._recent]
        return max(values) - min(values) < REJECT

    def _accept(self, raw: float) -> float:
        self.rejecting_since = None
        self._accepted.append(raw)
        return raw

    def _expire(self, now: float) -> None:
        while self._window and now - self._window[0][0] > WINDOW:
            self._window.popleft()
