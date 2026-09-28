# Battery maintenance: grid charging between two levels in Full Backup

Status: spec, 2026-09-28.

## 1. Goal

In Full Backup the battery sits near 100% and does nothing, but the system's own draw
(about 13 W on the reference site, 1% every 3.9 h) runs it down slowly. With no PV, which on
the reference site is every day until PTO and on any site every night, nothing tops it up
unless charge from grid is on. Leaving charge from grid on all the time isn't what the owner
wants either.

Battery maintenance turns charge from grid on when the battery falls to a start level and off
when it reaches a stop level. It acts only in Full Backup, and only while PV produces nothing.

## 2. Why the integration checks the charging itself

On 2026-09-28 the reference site had charge from grid allowed, but the battery sat at 0 W for
hours. The Envoy's scheduler was stuck on "Charge From PV" (`/ivp/sc/sched`
`acb_current_mode`, shown by the Battery scheduler mode sensor). Changing the profile freed it
(docs/FINDINGS.md). A switch that only writes the setting would have failed silently in the
same way, so maintenance watches the battery power after turning charge from grid on, retries
once, and then warns.

Maintenance never changes the profile. That is the owner's decision.

## 3. Entities

All three belong to the Envoy device and use `EntityCategory.CONFIG`. They exist only where
the Charge from grid switch exists (a battery, the cloud's site settings read at setup, and
`charge_from_grid_available`), because maintenance writes the same setting.

| Entity | Key | Kind | Default | Range |
|---|---|---|---|---|
| Battery maintenance | `battery_maintenance` | switch, restored | off | |
| Maintenance charge start level | `maintenance_charge_start_level` | number, %, box, restored | 90 | 5–99 |
| Maintenance charge stop level | `maintenance_charge_stop_level` | number, %, box, restored | 100 | 6–100 |

- Setting the start level at or above the stop level (or the stop level at or below the start
  level) is refused with a `ServiceValidationError` (`maintenance_levels_invalid`).
- The values are the integration's own, not Enphase settings, so they don't use
  `ConfirmingControl`. They are restored with `RestoreEntity` / `RestoreNumber`.
- The switch's `status` attribute shows the state machine (section 4): `inactive`, `idle`,
  `charging`, `retrying`, `stuck` or `overridden`.
- The default is off. Nothing writes to the cloud until the owner turns it on.

## 4. State machine

The logic is a pure module, `maintenance.py`, free of Home Assistant like `confirm.py` and
`overhead.py`. It takes one observation per tick and returns the actions to take. The switch
entity feeds it on every FastCoordinator update (5 s) and carries out the actions.

### 4.1 Inputs

| Input | Source |
|---|---|
| enabled | the switch |
| start, stop | the two numbers |
| full_backup | `CloudCoordinator.data.profile == "backup_only"`; unknown when the cloud has no data |
| soc | `FastData.secctrl.soc` |
| allowed | `FastData.schedule.charge_from_grid_allowed`, the Envoy's local copy |
| battery power | `LiveData.storage.power`; negative is charging |
| pv | `LiveData.pv.power`; a site with no PV meter counts as 0 |
| on grid | `Relay.grid_connected`; a site with no relay counts as on grid |

A tick with the live or fast data missing does nothing. So does a tick where the profile, soc or
allowed is unknown: an unknown profile holds the current state rather than counting as "not Full
Backup", so a cloud outage doesn't end a charge. The switch also runs a tick when it is added,
so its status is right before the first poll.

### 4.2 Active

Maintenance is active when it is enabled, the profile is known to be Full Backup and PV has not
been above `PV_MAX` (10 W) for `PV_HOLD` (120 s). The hold stops a flicker of dawn PV from
ending a charge. PV at night reads a few watts either side of zero, hence the tolerance.

### 4.3 States

- **idle**: active, not charging on maintenance's behalf.
  - soc ≤ start and not allowed: turn charge from grid on, go to **charging**.
  - soc ≤ start and already allowed (the owner turned it on): adopt it and go to
    **charging**, so maintenance turns it off at the stop level.
- **charging**: maintenance owns charge from grid.
  - soc ≥ stop: turn it off, go to **idle**.
  - Not allowed any more, once `CONFIRM_TIMEOUT` (90 s) has passed since maintenance's last
    write: the owner turned it off. Go to **overridden**.
  - Stuck (section 4.4): the first time, turn it off and go to **retrying**. The second time,
    raise the repair issue and go to **stuck**.
- **retrying**: waits for the Envoy to report charge from grid off, or 90 s, then turns it on
  again and goes back to **charging**.
- **stuck**: the repair issue is up. No more writes, except turning charge from grid off at
  the stop level or when maintenance stops being active. The battery charging again (the owner
  changed the profile, or the scheduler recovered) clears the issue and goes back to
  **charging**.
- **overridden**: waits for soc to rise above the start level, then goes to **idle**, so
  turning charge from grid off by hand isn't undone 5 s later.
- **inactive**: not active. Entering it from **charging**, **retrying** or **stuck** turns
  charge from grid off, because maintenance turned it on (or adopted it). From **idle** or
  **overridden** it writes nothing. The repair issue is cleared.

Only one retry is made per charge. The retry count resets when maintenance goes back to
**idle**.

### 4.4 Stuck

In **charging**, while allowed is true and the site is on grid, the battery counts as
charging when its power is at or below −`CHARGING_W` (−50 W). It is stuck when it hasn't
charged for `STUCK_AFTER` (10 min) since the later of maintenance's last write and the last
tick it charged. Off grid, the timer restarts: the grid can't charge the battery. The Battery
scheduler mode goes in the repair issue's text, since it shows the likely cause.

### 4.5 Failed writes

A write that Enphase refuses (or a cloud outage) is logged as a warning and retried after
`WRITE_BACKOFF` (5 min); the state doesn't change until a write succeeds. A rejected login
starts reauth, as the other controls do.

### 4.6 Restart

The switch restores its on/off state and whether maintenance owned charge from grid
(`owned` in the restored attributes). An owned charge restarts in **charging** with a fresh
stuck timer; otherwise maintenance starts in **idle**. The repair issue is removed when the
entity is removed and raised again if the battery is still stuck.

## 5. Writes

The write bodies move out of `ChargeFromGridSwitch` into shared functions in `control.py`,
used by both the switch and maintenance:

- On: `accept_disclaimer("itc")` when the ITC disclaimer applies (US), then
  `{"chargeFromGrid": true, "chargeFromGridScheduleEnabled": false}` with the existing begin
  and end times, plus `"acceptedItcDisclaimer": true` for the US.
- Off: `{"chargeFromGrid": false}`.

After a write, maintenance refreshes the CloudCoordinator, like the switch.

## 6. Repair issue

`maintenance_charge_stuck_<entry_id>`, a warning, not fixable. Placeholders: `soc`, `mode`
(the scheduler mode, or "unknown"), `minutes`. It says that charge from grid is on, that the
battery hasn't charged, that turning it off and on didn't help, and that changing the battery
profile in the Enphase app and back usually frees the scheduler. Maintenance won't retry until
the battery charges or maintenance is turned off and on.

## 7. Logging

Info on every write maintenance makes, with soc and the reason; a warning for the retry, the
stuck issue and failed writes; debug on state changes. No secrets are involved.

## 8. Tests

- `tests/test_maintenance.py`: the state machine alone: start and stop, hysteresis between
  them, adoption, override, PV hold, profile changes, off grid, stuck to retry to stuck, the
  issue clearing on charging, write backoff and restart.
- `tests_ha`: the entities exist with the Charge from grid switch and not without it; level
  validation; a full cycle against a mocked `BatteryConfigClient` (on, then off at the stop
  level); the repair issue after two stuck periods; no writes while the switch is off.

## 9. Later

- Maintenance in Self-Consumption or Savings, if wanted, would need its own rules: there the
  reserve and charge from grid already interact.
- A notification as well as the repair issue.
