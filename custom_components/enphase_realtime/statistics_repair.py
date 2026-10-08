"""Finds and removes phantom meter resets in the lifetime battery energy statistics.

See docs/specs/battery-statistics-repair.md.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later

from .const import CONF_SERIAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

CHARGED = "lifetime_battery_energy_charged"
DISCHARGED = "lifetime_battery_energy_discharged"

# The day of the first release. Nothing older can carry the fault, so it is never read.
_FIRST_RELEASE = datetime(2026, 10, 1, tzinfo=UTC)
# The hour the upgrade happened in is compiled when the next one starts (spec 5).
RECHECK_AFTER = timedelta(minutes=65)
_UNIT = "kWh"
_TOLERANCE = 0.005
# Below this an hour's sum and state agree; the difference is float noise.
_NOISE = 1e-6


@dataclass(frozen=True, slots=True)
class Repair:
    statistic_id: str
    # (start of the hour, kWh to add to the sum from that hour on). All negative.
    adjustments: tuple[tuple[datetime, float], ...]

    @property
    def phantom_energy(self) -> float:
        """kWh the statistics hold that the battery never moved."""
        return -sum(amount for _, amount in self.adjustments)


def phantom_resets(
    rows: Iterable[tuple[float, float | None, float | None]],
) -> list[tuple[float, float]]:
    """The sum adjustments that undo the phantom resets in a lifetime counter's statistics.

    `rows` are (start, state, sum) per hour, oldest first. Returns (start, adjustment) for the
    hours to correct (spec 3).
    """
    found: list[tuple[float, float]] = []
    group: list[tuple[float, float]] = []
    excess = 0.0
    peak = previous_sum = None
    for start, state, total in rows:
        if state is None or total is None:
            continue
        if peak is None or previous_sum is None:
            peak, previous_sum = state, total
            continue
        recorded = total - previous_sum
        previous_sum = total
        true = max(0.0, state - peak)
        group.append((start, true - recorded))
        excess += recorded - true
        if state < peak:
            # Still inside a halved poll, or a real reset: the group stays open.
            continue
        if _whole_counters(excess, peak, state):
            found.extend(fix for fix in group if abs(fix[1]) > _NOISE)
        group, excess, peak = [], 0.0, state
    return found


def _whole_counters(excess: float, low: float, high: float) -> bool:
    """True if `excess` is the counter counted again a whole number of times."""
    if low <= 0:
        return False
    fewest = max(1, math.ceil(excess / (high * (1 + _TOLERANCE))))
    return fewest <= math.floor(excess / (low * (1 - _TOLERANCE)))


async def async_find_repairs(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Repair]:
    """The repairs the entry's battery statistics need, by sensor key."""
    registry = er.async_get(hass)
    serial = entry.data[CONF_SERIAL]
    ids = {
        key: entity_id
        for key in (CHARGED, DISCHARGED)
        if (entity_id := registry.async_get_entity_id("sensor", DOMAIN, f"{serial}_{key}"))
    }
    if not ids:
        return {}
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        _FIRST_RELEASE,
        None,
        set(ids.values()),
        "hour",
        {"energy": _UNIT},
        {"state", "sum"},
    )
    repairs = {}
    for key, entity_id in ids.items():
        rows = (
            (row["start"], row.get("state"), row.get("sum")) for row in stats.get(entity_id, [])
        )
        if fixes := phantom_resets(rows):
            repairs[key] = Repair(
                entity_id,
                tuple((datetime.fromtimestamp(start, UTC), amount) for start, amount in fixes),
            )
    return repairs


def issue_id(entry: ConfigEntry) -> str:
    return f"battery_statistics_{entry.entry_id}"


async def async_check(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise the repair issue if the statistics are damaged, and clear it if they aren't."""
    repairs = await async_find_repairs(hass, entry)
    if not repairs:
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry))
        return
    _LOGGER.warning(
        "Lifetime battery energy statistics hold phantom meter resets (%s); see Repairs",
        ", ".join(f"{r.statistic_id}: {r.phantom_energy:.1f} kWh" for r in repairs.values()),
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry),
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="battery_statistics",
        data={"entry_id": entry.entry_id},
    )


async def async_apply(hass: HomeAssistant, repairs: Iterable[Repair]) -> None:
    """Take the phantom energy out of the statistics and wait for the recorder to finish."""
    recorder = get_instance(hass)
    for repair in repairs:
        for start, amount in repair.adjustments:
            recorder.async_adjust_statistics(repair.statistic_id, start, amount, _UNIT)
    await recorder.async_block_till_done()


@callback
def async_start(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Check now and once more after the next hourly compile (spec 5)."""
    if "recorder" not in hass.config.components:
        return

    def check(_: datetime | None = None) -> None:
        entry.async_create_task(hass, async_check(hass, entry), f"{DOMAIN} statistics check")

    check()
    entry.async_on_unload(async_call_later(hass, RECHECK_AFTER, callback(check)))
