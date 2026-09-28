"""Battery maintenance's state machine (docs/specs/battery-maintenance.md section 4)."""

from __future__ import annotations

from dataclasses import replace

from maintenance import (
    CONFIRM_AFTER,
    PV_HOLD,
    STUCK_AFTER,
    WRITE_BACKOFF,
    Action,
    Maintenance,
    Observation,
    State,
)

ON, OFF = Action.TURN_ON, Action.TURN_OFF
BASE = Observation(
    enabled=True,
    start=90,
    stop=100,
    full_backup=True,
    soc=95,
    allowed=False,
    battery_w=0.0,
    pv_w=0.0,
)


def obs(**changes: object) -> Observation:
    return replace(BASE, **changes)  # type: ignore[arg-type]


def charging(m: Maintenance, now: float = 0.0) -> Maintenance:
    """Takes a fresh machine through turning charge from grid on at `now`."""
    assert m.update(now, obs(soc=90)) == [ON]
    m.done(now, ok=True)
    assert m.state is State.CHARGING
    return m


def test_hysteresis() -> None:
    m = Maintenance(0)
    assert m.update(0, obs(soc=95)) == []
    assert m.update(5, obs(soc=91)) == []
    assert m.update(10, obs(soc=90)) == [ON]
    # A write in flight blocks everything else.
    assert m.update(15, obs(soc=90)) == []
    m.done(15, ok=True)
    assert m.owned
    assert m.update(20, obs(soc=95, allowed=True, battery_w=-3800)) == []
    assert m.update(25, obs(soc=100, allowed=True, battery_w=-3800)) == [OFF]
    m.done(25, ok=True)
    assert (m.state, m.owned) == (State.IDLE, False)
    # Between the levels with charge from grid off: nothing.
    assert m.update(30, obs(soc=99)) == []


def test_adopts_charge_from_grid_turned_on_by_hand() -> None:
    m = Maintenance(0)
    assert m.update(0, obs(soc=85, allowed=True, battery_w=-3000)) == []
    assert m.state is State.CHARGING
    assert m.update(5, obs(soc=100, allowed=True)) == [OFF]


def test_manual_off_is_respected() -> None:
    m = charging(Maintenance(0))
    # Within the Envoy's confirmation window, "not allowed" is just lag.
    assert m.update(CONFIRM_AFTER - 1, obs(soc=89)) == []
    assert m.state is State.CHARGING
    m.update(CONFIRM_AFTER, obs(soc=89))
    assert m.state is State.OVERRIDDEN
    assert m.update(CONFIRM_AFTER + 5, obs(soc=89)) == []
    m.update(CONFIRM_AFTER + 10, obs(soc=91))
    assert m.state is State.IDLE
    assert m.update(CONFIRM_AFTER + 15, obs(soc=90)) == [ON]


def test_inactive_turns_off_only_what_it_owns() -> None:
    m = Maintenance(0)
    assert m.update(0, obs(full_backup=False, soc=50)) == []
    assert m.state is State.INACTIVE
    m = charging(Maintenance(0))
    assert m.update(5, obs(full_backup=False, soc=91, allowed=True)) == [OFF]
    m.done(5, ok=True)
    assert m.state is State.INACTIVE
    # Back in Full Backup, from idle again.
    assert m.update(10, obs(soc=90)) == [ON]


def test_disabled_turns_off() -> None:
    m = charging(Maintenance(0))
    assert m.update(5, obs(enabled=False, soc=91, allowed=True)) == [OFF]


def test_unknown_inputs_hold() -> None:
    m = charging(Maintenance(0))
    for change in ({"full_backup": None}, {"soc": None}, {"allowed": None}):
        assert m.update(5, obs(enabled=False, **change)) == []  # type: ignore[arg-type]
    assert m.state is State.CHARGING


def test_pv_needs_to_hold() -> None:
    m = charging(Maintenance(0))
    live = {"soc": 91, "allowed": True, "battery_w": -3000}
    assert m.update(10, obs(pv_w=500, **live)) == []  # type: ignore[arg-type]
    assert m.update(10 + PV_HOLD - 1, obs(pv_w=500, **live)) == []  # type: ignore[arg-type]
    # A dip resets the hold.
    assert m.update(10 + PV_HOLD, obs(pv_w=3, **live)) == []  # type: ignore[arg-type]
    assert m.update(20 + PV_HOLD, obs(pv_w=500, **live)) == []  # type: ignore[arg-type]
    assert m.update(20 + 2 * PV_HOLD, obs(pv_w=500, **live)) == [OFF]  # type: ignore[arg-type]
    # A site with no PV meter counts as no PV.
    m = Maintenance(0)
    assert m.update(0, obs(pv_w=None, soc=90)) == [ON]


def test_stuck_retries_once_then_raises() -> None:
    m = charging(Maintenance(0))
    stuck = obs(soc=85, allowed=True, battery_w=0)
    assert m.update(STUCK_AFTER - 1, stuck) == []
    t = STUCK_AFTER
    assert m.update(t, stuck) == [OFF]
    m.done(t, ok=True)
    assert m.state is State.RETRYING
    # Waits for the Envoy to see it off, then turns it on again.
    assert m.update(t + 5, stuck) == []
    assert m.update(t + 10, obs(soc=85, allowed=False)) == [ON]
    m.done(t + 10, ok=True)
    assert m.state is State.CHARGING
    t += 10
    assert m.update(t + STUCK_AFTER, stuck) == [Action.RAISE_ISSUE]
    assert m.state is State.STUCK
    # No more writes while stuck.
    assert m.update(t + 2 * STUCK_AFTER, stuck) == []
    # The owner frees the scheduler: the issue clears.
    assert m.update(t + 2 * STUCK_AFTER + 5, replace(stuck, battery_w=-3800)) == [
        Action.CLEAR_ISSUE
    ]
    assert m.state is State.CHARGING
    # Only one retry per charge: stuck again raises straight away.
    t += 2 * STUCK_AFTER + 5
    assert m.update(t + STUCK_AFTER, stuck) == [Action.RAISE_ISSUE]
    # Reaching the stop level clears the issue and turns off.
    assert m.update(t + STUCK_AFTER + 5, obs(soc=100, allowed=True)) == [
        Action.CLEAR_ISSUE,
        OFF,
    ]
    m.done(t + STUCK_AFTER + 5, ok=True)
    assert m.state is State.IDLE
    # The next charge gets its retry back.
    t += STUCK_AFTER + 10
    charging_again = charging(m, t)
    assert charging_again.update(t + STUCK_AFTER, stuck) == [OFF]


def test_retry_times_out_waiting_for_off() -> None:
    m = charging(Maintenance(0))
    stuck = obs(soc=85, allowed=True, battery_w=0)
    m.update(STUCK_AFTER, stuck)
    m.done(STUCK_AFTER, ok=True)
    assert m.update(STUCK_AFTER + CONFIRM_AFTER, stuck) == [ON]


def test_charging_resets_the_stuck_timer() -> None:
    m = charging(Maintenance(0))
    assert m.update(STUCK_AFTER - 10, obs(soc=85, allowed=True, battery_w=-60)) == []
    assert m.update(STUCK_AFTER + 10, obs(soc=85, allowed=True, battery_w=-10)) == []
    assert m.update(2 * STUCK_AFTER - 10, obs(soc=85, allowed=True, battery_w=-10)) == [OFF]


def test_off_grid_isnt_stuck() -> None:
    m = charging(Maintenance(0))
    off_grid = obs(soc=85, allowed=True, battery_w=0, on_grid=False)
    assert m.update(STUCK_AFTER, off_grid) == []
    assert m.update(STUCK_AFTER + 5, replace(off_grid, on_grid=True)) == []
    assert m.state is State.CHARGING


def test_stuck_then_turned_off_by_hand() -> None:
    m = charging(Maintenance(0))
    stuck = obs(soc=85, allowed=True, battery_w=0)
    m.update(STUCK_AFTER, stuck)
    m.done(STUCK_AFTER, ok=True)
    m.update(STUCK_AFTER + 5, obs(soc=85))
    m.done(STUCK_AFTER + 5, ok=True)
    assert m.update(2 * STUCK_AFTER + 5, stuck) == [Action.RAISE_ISSUE]
    assert m.update(2 * STUCK_AFTER + 10, obs(soc=85)) == [Action.CLEAR_ISSUE]
    assert m.state is State.OVERRIDDEN


def test_failed_write_backs_off() -> None:
    m = Maintenance(0)
    assert m.update(0, obs(soc=90)) == [ON]
    m.done(1, ok=False)
    assert m.state is State.IDLE
    assert m.update(5, obs(soc=90)) == []
    assert m.update(1 + WRITE_BACKOFF, obs(soc=90)) == [ON]


def test_failed_retry_write_keeps_the_retry() -> None:
    m = charging(Maintenance(0))
    stuck = obs(soc=85, allowed=True, battery_w=0)
    assert m.update(STUCK_AFTER, stuck) == [OFF]
    m.done(STUCK_AFTER, ok=False)
    assert m.state is State.CHARGING
    assert m.update(STUCK_AFTER + WRITE_BACKOFF, stuck) == [OFF]


def test_restart_restores_ownership() -> None:
    m = Maintenance(1000, owned=True)
    assert m.state is State.CHARGING
    # A fresh stuck timer.
    assert m.update(1000 + STUCK_AFTER - 1, obs(soc=85, allowed=True)) == []
    assert m.update(1005 + STUCK_AFTER, obs(soc=100, allowed=True)) == [OFF]
    assert Maintenance(0).state is State.IDLE
