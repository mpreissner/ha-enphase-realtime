# Findings

Verified against a live system on 2026-09-24: an IQ System Controller and one IQ Battery 5P
(5 kWh, 3840 W), split-phase, Envoy firmware D8.3.6086, owner token. Serials, site IDs and
user IDs appear below only as placeholders.

## Summary

- **You can read battery state locally, but not control it.** Local `/ivp/*` endpoints answer
  in under 0.3 s and show what the controller is actually doing.
- **Control has to go through the Enlighten cloud.** A cloud write reaches the Envoy within
  about 10–20 s.
- **The cloud is slow to confirm a write.** The cloud's own view of a setting lags until the
  Envoy next reports in, which takes 5–15 min. Confirm changes from the local values instead.

## Local Envoy (read)

Base `https://<envoy>/`, header `Authorization: Bearer <owner JWT>`, self-signed TLS.

| What | Endpoint | Field |
|---|---|---|
| Charge-from-grid in effect | `/ivp/sc/sched` | `"Charge From Grid Allowed"` (bool) |
| Controller mode | `/ivp/sc/sched` | `acb_current_mode`, an index into `sched_mode_key` (2 = CG, 6 = CP). Not a live status: on 2026-09-25 it still read CG a day after charge from grid was turned off, with the battery at 0 W. Exposed as the diagnostic "Battery scheduler mode" because it shows when the scheduler hasn't acted on charge from grid (2026-09-28, below). |
| Commanded setpoint | `/ivp/sc/status` | `response.groups[ENC].setpoint_val` (-100 = full charge, 0 = idle); `acbstats.encharge_feedback.raw_setpt` |
| Reserve (very-low SoC) | `/ivp/ensemble/secctrl` | `VLS_Limit` (%) = cloud `veryLowSoc` |
| Backup SoC target | `/ivp/ensemble/secctrl` | `configured_backup_soc`, `adjusted_backup_soc` |
| Battery power / SoC / grid | `/ivp/livedata/status` | `meters.storage.agg_p_mw` (negative = charging), `meters.grid.agg_p_mw`, `meters.soc` |
| Grid relay | `/ivp/ensemble/relay` | `mains_admin_state` (commanded) vs `mains_oper_state` (actual) |

`/ivp/ensemble/secctrl` `freq_bias_hz` and `voltage_bias_v` are nonzero while charging from
grid. That is probably how the controller drives the battery (not yet confirmed).

Slow endpoints to avoid polling: `production.json` (30–55 s), `inventory.json` (~20 s),
`home.json` (over 60 s). Installer-only endpoints return 401 with an owner token: `/ivp/peb/*`,
`/ivp/tpm/*`, `/ivp/meters/cts`, `/installer/*`, `/ivp/mod/<eid>/mode/power`.

### Update rates (measured 2026-09-25, D8.3.6086, MQTT add-on running)

- **`/ivp/livedata/status` polled at 1 s** (plus `/ivp/ensemble/relay`): 152 polls, 0
  failures. Median gap 1.10 s, mean 1.21 s. Fetch mean 0.36 s, worst 1.58 s. The Envoy kept
  answering through every stream stall below. This is also where the MQTT project gets its
  1 Hz on battery sites: it polls this endpoint in a loop with a 0.6 s sleep. It reads
  `/stream/meter` only on v5 Envoys.
- **`/stream/meter`**: about one frame a second on average, and none lost. But the Envoy
  regularly holds frames for 3–13 s and then sends them as a burst of up to 8 frames within
  30 ms. There were 6 stalls in 3 minutes, whether the live poll ran at 1 s (33 s stalled in
  total) or slower (39 s). The client reads with `iter_any()`, so the buffering happens on the
  Envoy's side. Treat the stream as detail for dashboards, not as a trigger that needs low
  latency.

**Dry contacts: no live write without the user.** On the reference system they switch real loads:
NC1 is the air conditioner (confirmed 28 September 2026 by opening it from HA), and NC2 is believed to be
the dryer. `POST /ivp/ensemble/dry_contacts` works with an owner token on D8.3.6086, and the new
state shows on the next read, within about 3 s. `POST /ivp/ss/dry_contact_settings` with the
contact's full object also works, and so do POSTs 0.3 s apart. The levels read back within 4 s.
After any settings POST, `ensemble/dry_contacts` reports every NC contact as `open` for 5–90 s,
not just the one written. The relays don't actually open: the AC kept drawing power throughout.

**Grid relay opened and closed live (2026-10-01).** Through the integration's switch, with the
user present. Opening: the command went at 10:51:17 and `mains_oper_state` stopped reading
`closed` at 10:51:45 (about 28 s), but admin and oper still didn't match 30 s after the command,
so the old 30 s confirmation failed falsely. What oper reports in between wasn't captured; the
switch now logs every relay state change at debug. Closing confirmed in about 15 s. No relay
timeouts during the test.

- **PV while islanded:** the microinverters dropped out at the switchover and stayed at about
  −20 W (standby draw) for about 45 s. They then ramped back up with the battery forming the grid
  (14 W at 10:52:34, 248 W at 10:52:43), dropped out again at the reconnect, and came back about
  10 s after it.
- **Dry contacts didn't shed:** NC1 and NC2 have micro-grid action `shed`, but the contacts
  still read `closed` on the slow poll at 10:52:01, mid-island. SPAN shows the AC circuit (NC1)
  drawing up to 1.86 kW while off grid. All four contacts are in `manual` mode with
  `manual_override` `"true"`, as the installer left them: the 2026-09-24 capture, made before
  any write, already had this, NO1 and NO2 included. The likely reason is that manual mode follows the last manual command
  and ignores the grid actions, but that hasn't been tested.

## Local writes do not work (D8.3.6086)

`PUT /admin/lib/tariff` returns 200 and saves the file, but the controller ignores it:

- `storage_settings.charge_from_grid` true or false: no change to `sc/sched`, the setpoint or
  battery power. Tested while the battery was charging at full rate.
- A must-charge window (`must_charge_start`, `must_charge_duration`, mode CG): mode and setpoint
  did not change.
- The `schedule` section is rebuilt on every read (its date is always the current time and its
  values are defaults). Writes to it are dropped without an error.

`storage_settings` in the tariff file is a stale copy (dated weeks before the test). Don't
treat it as the current state.

State left on the reference Envoy (checked 2026-09-25): the tariff file matches the original
except `storage_settings.charge_from_grid`, which the last test set to `false` and which was
kept, so a firmware that honours the file wouldn't charge from grid. The must-charge window is
off (duration 0, mode CP). Cloud changes to charge from grid did not rewrite the file
(`storage_settings.date` unchanged), and `/ivp/sc/sched` `acb_current_mode` kept reading CG
through them while the battery charged and stopped as told.

Checked again on 2026-09-28, after the Envoy restarted by itself at about 03:06: the file matches
the 2026-09-24 original exactly, `storage_settings.charge_from_grid` true included.

**Charge from grid allowed but not charging (2026-09-28).** After that restart, charge from grid
turned on (from the integration and from the Enphase app) was accepted by the cloud and shown
locally as `"Charge From Grid Allowed": true`, but `acb_current_mode` stayed at CP and storage
at 0 W, with the site in Full Backup, `configured_backup_soc` 100 and the battery at 69 %,
`ENCHG_STATE_READY`. The tariff file wasn't the cause (above). The Envoy had accepted the
setting, but its scheduler hadn't acted on it. Changing the profile to Self-Consumption,
turning charge from grid on there ("anytime below reserve") and switching back to Full Backup
made the scheduler re-evaluate: `acb_current_mode` went to CG and the battery charged at about
3.8 kW, and kept charging in Full Backup. Full Backup isn't the problem: the first charge from
grid on this system, before the repo existed, was done entirely in Full Backup. Why the
scheduler stuck is unknown; the Envoy had rebooted itself at about 03:06 that morning.
`Charge From Grid Allowed` therefore confirms the setting, not that the battery is charging.
Charge from grid allowed, the battery below the reserve and the mode not CG is the sign of
this state.

**Non-numeric inventory values after a reboot (2026-09-28).** At 03:14 and 03:15, after the
Envoy's 03:06 reboot, `/ivp/ensemble/inventory` reported the battery's `temperature` and
`maxCellTemp` as the string `"unknown"`. The integration now treats any non-numeric inventory
number as missing.

**Self-Consumption discharge burst (2026-09-28).** On switching from Full Backup to
Self-Consumption (reserve 30 %, battery 69 %, load about 1.1 kW, no PV), the battery
discharged for about 10 s, rising to 3.86 kW (one IQ Battery 5P's maximum) and exporting up
to 2.7 kW, then went to 0 W and stayed there. This was before charge from grid was turned on
in Self-Consumption (above). Not seen again yet.

## Enlighten cloud battery API (write)

Base `https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1`.

| Call | Purpose |
|---|---|
| `GET /batterySettings/{site}?userId={uid}&source=enho` | Current settings plus `requestedConfig` |
| `PUT /batterySettings/{site}?userId={uid}` | Partial update (JSON body with only the changed keys) |
| `POST /batterySettings/acceptDisclaimer/{site}` `{"disclaimer-type":"itc"}` | Required before enabling charge-from-grid |
| `GET /siteSettings/{site}` | Site metadata |
| `GET /mqttSignedUrl/{site}` | AWS IoT live-stream URL (short-lived signed credentials) |

### Auth

- **GET:** needs the `_enlighten_4_session` cookie plus the header `Username: {uid}`. Manager-token
  cookies on their own get a 302 redirect to HTML.
- **Writes:** also need `X-XSRF-Token`, set to the value of the `BP-XSRF-Token` cookie. The
  server sets that cookie on any GET, so do a GET right before each write.
- **Login:** `POST /login/login.json` (`user[email]`, `user[password]`) sets
  `_enlighten_4_session` (HttpOnly, Secure), whose value equals the body's `session_id`, so the
  integration can sign in again when the session expires (spike S1, settled September 2026).
  The captured session expired about a week after capture.

### Verified write bodies

| Action | Body | Seen on the Envoy |
|---|---|---|
| Charge-from-grid on | `acceptDisclaimer` first, then `{"chargeFromGrid":true,"acceptedItcDisclaimer":true,"chargeBeginTime":120,"chargeEndTime":300,"chargeFromGridScheduleEnabled":false}` | Within 10 s: `cfg` true and charging at -2.7 to -3.9 kW |
| Charge-from-grid off | `{"chargeFromGrid":false}` | Within 20 s: `cfg` false and storage about 0 W |
| Reserve | `{"veryLowSoc":N}` (range 5–25 on this system) | Within about 20 s: `VLS_Limit` = N |
| Storage mode | `{"profile":"self-consumption"}`, `"backup_only"` or `"cost_savings"` | Within about 10 s: `configured_backup_soc` and the scheduler mode change (verified 2026-10-01) |

**Each profile keeps its own settings (2026-10-01).** No Envoy field reports the profile, so
the integration confirms a profile change when the cloud reports it with nothing pending (within
about 15–20 s). The cloud keeps a reserve and a charge-from-grid setting for each profile, and a
profile write applies that profile's stored values along with it. On the reference site,
switching from Full Backup (100%, charge from grid off) to Self-Consumption brought back 30% and
charge from grid **on**. Turning charge from grid off while in Self-Consumption stored "off" for
that profile. Full Backup kept its own settings throughout.

### Pending state

After a write, the GET response puts the new values under `data.requestedConfig`, along with
`pendingGateways: [<envoy serial>]`. The top-level fields keep the **old** value until the
Envoy next reports in (5–15 min), even though the Envoy applied the change within seconds. The
Enphase app shows this as "pending".

**Integration rule:** send writes to the cloud and read state from the Envoy. Treat a
`pendingGateways` entry as normal, not as a failure.

## Out of scope for now

- Grid-side voltage. No owner-accessible endpoint exposes it.
