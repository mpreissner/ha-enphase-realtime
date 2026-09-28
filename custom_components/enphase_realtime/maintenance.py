"""Battery maintenance: charge from grid between two levels in Full Backup, with no PV
(docs/specs/battery-maintenance.md).

This module is the state machine alone, free of Home Assistant, so it can be tested as a function
of (time, observation). The switch entity feeds it and carries out the actions. A write stays in
flight until the entity reports how it went; only a write Enphase accepted changes the state.
Times are monotonic seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# PV above PV_MAX W for PV_HOLD s ends maintenance (spec 4.2). PV at night reads a few watts
# either side of zero, and the hold keeps a flicker of dawn PV from ending a charge.
PV_MAX = 10.0
PV_HOLD = 120.0
# The battery counts as charging at or below -CHARGING_W W (spec 4.4).
CHARGING_W = 50.0
# Not charging for this long after a write, or since it last charged, is stuck (spec 4.4).
STUCK_AFTER = 600.0
# The time the Envoy gets to pick up a write, as the controls' CONFIRM_TIMEOUT (spec 4.3).
CONFIRM_AFTER = 90.0
# After a failed write, the next attempt waits this long (spec 4.5).
WRITE_BACKOFF = 300.0


class State(StrEnum):
    INACTIVE = "inactive"
    IDLE = "idle"
    CHARGING = "charging"
    RETRYING = "retrying"
    STUCK = "stuck"
    OVERRIDDEN = "overridden"


class Action(StrEnum):
    TURN_ON = "turn_on"
    TURN_OFF = "turn_off"
    RAISE_ISSUE = "raise_issue"
    CLEAR_ISSUE = "clear_issue"


# States in which maintenance turned charge from grid on (or adopted it), so it turns it off.
OWNED = frozenset({State.CHARGING, State.RETRYING, State.STUCK})


@dataclass(frozen=True, slots=True)
class Observation:
    """One tick's inputs (spec 4.1). `None` means unknown, and a tick with the profile, the
    battery level or the Envoy's charge-from-grid setting unknown does nothing."""

    enabled: bool
    start: int
    stop: int
    full_backup: bool | None
    soc: int | None
    # The Envoy's local copy of the charge-from-grid setting.
    allowed: bool | None
    # The Envoy's sign: negative is charging.
    battery_w: float | None
    # None on a site with no PV meter, which counts as no PV.
    pv_w: float | None
    on_grid: bool = True


# Levels when the numbers have nothing to restore (spec 3).
DEFAULT_START = 90
DEFAULT_STOP = 100


@dataclass(slots=True)
class MaintenanceSettings:
    """The two levels, set by the number entities and read by the switch on every tick."""

    start: int = DEFAULT_START
    stop: int = DEFAULT_STOP


class Maintenance:
    def __init__(self, now: float, *, owned: bool = False) -> None:
        # An owned charge survives a restart with a fresh stuck timer (spec 4.6).
        self.state = State.CHARGING if owned else State.IDLE
        self._retried = False
        self._last_write = now
        self._last_charging = now
        self._pv_since: float | None = None
        self._pending: tuple[Action, State] | None = None
        self._backoff_until: float | None = None
        self._issue = False

    @property
    def owned(self) -> bool:
        return self.state in OWNED

    @property
    def pending(self) -> tuple[Action, State] | None:
        """The write in flight, and the state it leads to."""
        return self._pending

    def update(self, now: float, obs: Observation) -> list[Action]:
        """The actions to take now. At most one of them is a write."""
        if self._pending is not None:
            return []
        if obs.pv_w is not None and obs.pv_w > PV_MAX:
            if self._pv_since is None:
                self._pv_since = now
        else:
            self._pv_since = None
        if obs.full_backup is None or obs.soc is None or obs.allowed is None:
            return []
        pv = self._pv_since is not None and now - self._pv_since >= PV_HOLD
        if not obs.enabled or not obs.full_backup or pv:
            return self._deactivate(now)
        if self.state is State.INACTIVE:
            self._enter_idle()
        match self.state:
            case State.IDLE:
                return self._idle(now, obs)
            case State.OVERRIDDEN:
                if obs.soc > obs.start:
                    self._enter_idle()
                return []
            case State.RETRYING:
                return self._retrying(now, obs)
            case _:
                return self._charging(now, obs)

    def done(self, now: float, ok: bool) -> None:
        """Report how the write `update` asked for went."""
        if self._pending is None:
            return
        _, next_state = self._pending
        self._pending = None
        if not ok:
            self._backoff_until = now + WRITE_BACKOFF
            return
        self._backoff_until = None
        self._last_write = self._last_charging = now
        if next_state in OWNED:
            self.state = next_state
            if next_state is State.RETRYING:
                self._retried = True
        elif next_state is State.IDLE:
            self._enter_idle()
        else:
            self.state = next_state
            self._retried = False

    def _deactivate(self, now: float) -> list[Action]:
        actions = self._clear_issue()
        if self.owned:
            return actions + self._write(now, Action.TURN_OFF, State.INACTIVE)
        self.state = State.INACTIVE
        return actions

    def _enter_idle(self) -> None:
        self.state = State.IDLE
        self._retried = False

    def _idle(self, now: float, obs: Observation) -> list[Action]:
        if obs.soc > obs.start:
            return []
        if obs.allowed:
            # The owner turned charge from grid on: adopt it, so it goes off at the stop level.
            self.state = State.CHARGING
            self._last_write = self._last_charging = now
            return []
        return self._write(now, Action.TURN_ON, State.CHARGING)

    def _retrying(self, now: float, obs: Observation) -> list[Action]:
        if obs.soc >= obs.stop:
            # Charge from grid went off for the retry, so there is nothing to write.
            self._enter_idle()
            return []
        if obs.allowed and now - self._last_write < CONFIRM_AFTER:
            return []
        return self._write(now, Action.TURN_ON, State.CHARGING)

    def _charging(self, now: float, obs: Observation) -> list[Action]:
        if obs.soc >= obs.stop:
            return self._clear_issue() + self._write(now, Action.TURN_OFF, State.IDLE)
        if not obs.allowed and now - self._last_write >= CONFIRM_AFTER:
            # The owner turned it off; don't undo that 5 s later (spec 4.3).
            self.state = State.OVERRIDDEN
            return self._clear_issue()
        charging = obs.battery_w is not None and obs.battery_w <= -CHARGING_W
        if self.state is State.STUCK:
            if not charging:
                return []
            self.state = State.CHARGING
            self._last_charging = now
            return self._clear_issue()
        if charging or not obs.allowed or not obs.on_grid:
            self._last_charging = now
            return []
        if now - max(self._last_write, self._last_charging) < STUCK_AFTER:
            return []
        if not self._retried:
            return self._write(now, Action.TURN_OFF, State.RETRYING)
        self.state = State.STUCK
        self._issue = True
        return [Action.RAISE_ISSUE]

    def _write(self, now: float, action: Action, next_state: State) -> list[Action]:
        if self._backoff_until is not None and now < self._backoff_until:
            return []
        self._pending = (action, next_state)
        return [action]

    def _clear_issue(self) -> list[Action]:
        if not self._issue:
            return []
        self._issue = False
        return [Action.CLEAR_ISSUE]
