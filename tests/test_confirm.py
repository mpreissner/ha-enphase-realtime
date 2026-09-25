"""The write-then-confirm state machine (spec 6.1), as a function of (requested, local, elapsed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from confirm import (
    CONFIRM_TIMEOUT,
    RELAY_CONFIRM_TIMEOUT,
    Confirmation,
    LocalConfirm,
    confirmation_state,
)

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("requested", "local", "elapsed", "expected"),
    [
        (15, 15, 0, Confirmation.CONFIRMED),
        (15, 10, 0, Confirmation.PENDING),
        (15, 10, 89, Confirmation.PENDING),
        (15, 10, 90, Confirmation.FAILED),
        (15, 15, 90, Confirmation.CONFIRMED),  # a match on the last tick still counts
        (15, 15, 500, Confirmation.CONFIRMED),
        (15, None, 30, Confirmation.PENDING),  # the Envoy didn't answer
        (15, None, 90, Confirmation.FAILED),
        (True, True, 12, Confirmation.CONFIRMED),
        (False, True, 12, Confirmation.PENDING),
        (False, None, 0, Confirmation.PENDING),  # None isn't False
    ],
)
def test_confirmation_state(
    requested: object, local: object, elapsed: int, expected: Confirmation
) -> None:
    assert confirmation_state(requested, local, timedelta(seconds=elapsed)) is expected


def test_timeout_is_90_seconds() -> None:
    assert timedelta(seconds=90) == CONFIRM_TIMEOUT


def test_idle_shows_local_value() -> None:
    confirm: LocalConfirm[int] = LocalConfirm()
    assert confirm.status is None
    assert confirm.shown(10) == 10
    assert confirm.check(10, T0) is None


def test_pending_shows_requested_until_confirmed() -> None:
    confirm: LocalConfirm[int] = LocalConfirm()
    confirm.start(15, T0)
    assert confirm.pending
    assert confirm.shown(10) == 15

    assert confirm.check(10, T0 + timedelta(seconds=5)) is None
    assert confirm.shown(10) == 15

    assert confirm.check(15, T0 + timedelta(seconds=20)) is Confirmation.CONFIRMED
    assert confirm.status is Confirmation.CONFIRMED
    assert confirm.shown(15) == 15
    # Settled: later ticks change nothing, and the state follows the Envoy again.
    assert confirm.check(12, T0 + timedelta(seconds=200)) is None
    assert confirm.shown(12) == 12


def test_no_match_in_time_reverts_to_local() -> None:
    confirm: LocalConfirm[bool] = LocalConfirm()
    confirm.start(True, T0)
    assert confirm.check(False, T0 + timedelta(seconds=60)) is None
    assert confirm.check(False, T0 + timedelta(seconds=90)) is Confirmation.FAILED
    assert confirm.status is Confirmation.FAILED
    assert confirm.shown(False) is False


def test_new_write_restarts_the_clock() -> None:
    confirm: LocalConfirm[int] = LocalConfirm()
    confirm.start(15, T0)
    confirm.start(20, T0 + timedelta(seconds=80))
    assert confirm.check(15, T0 + timedelta(seconds=100)) is None
    assert confirm.shown(15) == 20
    assert confirm.check(20, T0 + timedelta(seconds=110)) is Confirmation.CONFIRMED


def test_relay_uses_its_own_30_second_timeout() -> None:
    confirm: LocalConfirm[bool] = LocalConfirm(timeout=RELAY_CONFIRM_TIMEOUT)
    confirm.start(False, T0)
    assert confirm.check(True, T0 + timedelta(seconds=29)) is None
    assert confirm.check(True, T0 + timedelta(seconds=30)) is Confirmation.FAILED
    assert confirm.shown(True) is True
