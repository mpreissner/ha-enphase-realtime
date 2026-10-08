# Repairing the lifetime battery energy statistics

Status: implemented, 2026-10-08.

## 1. Goal

Versions 1.0.1 to 1.0.3 reported the storage CT's lifetime counters at half their value for a
poll at a time on firmware 8.3.6000 and later (the one-channel fault in
`core-entity-parity.md`). The recorder took each drop for a meter reset and counted the whole
lifetime again, so the statistics of the two lifetime battery energy sensors, and the Energy
dashboard's battery figures built on them, are inflated. On the reference site five days added
3,480 kWh of charging to a battery that had taken 1.8 kWh.

Version 1.0.4 stops new damage. This feature removes the damage already recorded.

## 2. Offered, not automatic

The correction rewrites long-term statistics and can't be undone, so the integration raises a
fixable repair issue and changes nothing until the user confirms it. The issue says how much
energy will be removed. Sites without the damage never see it, and a user who wants to keep
their data can ignore it.

## 3. What a phantom reset looks like

For a `total_increasing` sensor the recorder treats a drop of more than 10 % as a reset and
starts a new cycle at zero. With the counter at `a` before the drop and `b` after it recovers,
the cycle adds `b` to the sum where the battery moved `b − a`. Every episode therefore adds the
counter's own value, `a`, to the sum.

The check reads the hourly statistics (`state` and `sum`) of both sensors from 2026-10-01, the
day of the first release, so older statistics are never touched. Going through the hours:

- `peak` is the highest `state` so far. A lifetime counter only rises, so an hour's true change
  is `max(0, state − peak)`, and the hour's **excess** is its change in `sum` minus that.
- Hours are grouped. A group ends with the first hour whose `state` is back at `peak` or
  above. Usually that is every hour by itself; when an hour ends inside a halved poll the
  group runs on until the counter has recovered.
- A group is a phantom reset only if its excess is a whole number of counter values: there is
  a `k ≥ 1` with `k × peak ≤ excess ≤ k × new peak`, within 0.5 %.

Nothing else is adjusted. In particular a real reset, where the counter drops and stays low,
never closes its group, and ordinary rounding noise is far below one counter value. A group
still open at the last hour is left for the next check.

Not detected: damage inside the first hour read, since there is no earlier row to compare
with.

## 4. The correction

Each hour of a matching group gets its excess taken out with the recorder's
`async_adjust_statistics`, the call behind **Developer tools → Statistics → Adjust sum**. It
shifts the sum of that hour and every later one, in the long-term and the short-term table, so
the hourly values that follow are compiled from corrected figures.

The adjustment is made at the start of the hour. Five-minute statistics inside a damaged hour
therefore keep a spike, now preceded by an equal dip; hourly and longer periods, which the
Energy dashboard uses, are right. The recorder purges five-minute statistics after ten days.

After the correction the hours' excess is zero, so running the check again finds nothing.

## 5. When the check runs

When the entry is set up, and once more 65 minutes later: the hour during which the upgrade
happened isn't compiled until the next hour starts, and it may hold one last episode. The
check needs the recorder and is skipped without it.

A finding creates the issue `battery_statistics_<entry id>`; a clean check deletes it. The fix
flow has one confirm step. On submit it runs the check again, queues the adjustments and waits
for the recorder to finish them.

## 6. Code

- `statistics_repair.py`: `phantom_resets` (the rule in section 3, no Home Assistant in it),
  the check and the correction.
- `repairs.py`: the fix flow.
