"""Write, then confirm locally (spec 6.1).

The cloud takes a write and hands it to the gateways, but its own fields lag 5-15 min. So a
control shows the requested value at once and watches the Envoy until the local value matches,
or until the time runs out. This module is the state machine alone, free of Home Assistant, so it
can be tested as a function of (requested, local value, elapsed time).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

CONFIRM_TIMEOUT = timedelta(seconds=90)
# The grid relay is local, so it has no cloud lag to wait out, but it is slow to open: live on
# 2026-10-01 it took about 28 s to stop reporting closed, and longer to report open (spec 6.3).
RELAY_CONFIRM_TIMEOUT = timedelta(seconds=90)
# Dry contacts are local too, and switch within about 3 s (docs/specs/dry-contacts.md 5).
DRY_CONTACT_CONFIRM_TIMEOUT = timedelta(seconds=30)


class Confirmation(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"


def confirmation_state(
    requested: object,
    local: object,
    elapsed: timedelta,
    timeout: timedelta = CONFIRM_TIMEOUT,
) -> Confirmation:
    """A match confirms even on the last tick; no match by the deadline fails. `local` is `None`
    when the Envoy didn't answer, which never matches."""
    if local is not None and local == requested:
        return Confirmation.CONFIRMED
    if elapsed >= timeout:
        return Confirmation.FAILED
    return Confirmation.PENDING


@dataclass(slots=True)
class LocalConfirm[T]:
    """One control's confirmation. `shown` is what the entity's state should be."""

    timeout: timedelta = CONFIRM_TIMEOUT
    requested: T | None = None
    started: datetime | None = None
    status: Confirmation | None = None

    def start(self, requested: T, now: datetime) -> None:
        self.requested = requested
        self.started = now
        self.status = Confirmation.PENDING

    @property
    def pending(self) -> bool:
        return self.status is Confirmation.PENDING

    def check(self, local: T | None, now: datetime) -> Confirmation | None:
        """Advance with the latest local value. Returns the new status when this call settled
        it (confirmed or failed), `None` otherwise."""
        if not self.pending or self.started is None:
            return None
        status = confirmation_state(self.requested, local, now - self.started, self.timeout)
        if status is Confirmation.PENDING:
            return None
        self.status = status
        return status

    def shown(self, local: T | None) -> T | None:
        """The requested value while pending; the local value otherwise, even after a failure."""
        return self.requested if self.pending else local
