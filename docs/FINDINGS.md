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
| Controller mode | `/ivp/sc/sched` | `acb_current_mode`, an index into `sched_mode_key` (2 = CG, 6 = CP). Not a live status: on 2026-09-25 it still read CG a day after charge from grid was turned off, with the battery at 0 W. Not exposed. |
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

**Dry contacts: never write.** On the reference system they switch real loads (HVAC and dryer).

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
- **Still open:** confirm that `POST /login/login.json` (`user[email]`, `user[password]`)
  returns `_enlighten_4_session`. The integration needs this so it can sign in again when the
  session expires. The captured session expired about a week after capture.

### Verified write bodies

| Action | Body | Seen on the Envoy |
|---|---|---|
| Charge-from-grid on | `acceptDisclaimer` first, then `{"chargeFromGrid":true,"acceptedItcDisclaimer":true,"chargeBeginTime":120,"chargeEndTime":300,"chargeFromGridScheduleEnabled":false}` | Within 10 s: `cfg` true and charging at -2.7 to -3.9 kW |
| Charge-from-grid off | `{"chargeFromGrid":false}` | Within 20 s: `cfg` false and storage about 0 W |
| Reserve | `{"veryLowSoc":N}` (range 5–25 on this system) | Within about 20 s: `VLS_Limit` = N |

### Pending state

After a write, the GET response puts the new values under `data.requestedConfig`, along with
`pendingGateways: [<envoy serial>]`. The top-level fields keep the **old** value until the
Envoy next reports in (5–15 min), even though the Envoy applied the change within seconds. The
Enphase app shows this as "pending".

**Integration rule:** send writes to the cloud and read state from the Envoy. Treat a
`pendingGateways` entry as normal, not as a failure.

## Out of scope for now

- Changing the storage mode or profile (Self-Consumption, Full Backup and so on). The endpoint
  wasn't captured.
- Grid-side voltage. No owner-accessible endpoint exposes it.
