# Spec: Enphase Realtime core integration

Status: draft, 2026-09-24
Protocol evidence: [../FINDINGS.md](../FINDINGS.md)

## 1. Goal

Replace the Home Assistant core `enphase_envoy` integration on an IQ System Controller and IQ
Battery site with one integration that:

1. **Streams real-time telemetry.** Readings arrive at about 1 Hz, or every few seconds,
   instead of the core integration's 60 s poll through slow legacy endpoints.
2. **Controls locally what the Envoy accepts locally.** That covers the grid relay, and later
   the dry contacts.
3. **Controls through the Enlighten cloud what the Envoy ignores locally.** That covers the
   battery settings. Every cloud write is then **confirmed from local values**.

When this is done, the core integration can be disabled with nothing lost. The parity table
is in section 5.

### Non-goals (for now)

- **Changing the storage mode or profile** (Self-Consumption, Full Backup, and so on). The
  write endpoint hasn't been captured. The integration reads the mode but can't set it.
- **Writing to dry contacts.** On the reference site they switch HVAC and dryer loads, and
  nobody knows which contact is which. They are read-only until the user maps them.
  Sections 5 and 10 cover this.
- Configuring generators.
- Supporting the legacy Envoy-S (firmware below 7) or envoys without an owner-token flow.
- Getting the brand into home-assistant/brands.

## 2. Why the core integration isn't enough

| Problem | Cause |
|---|---|
| Slow, stale data | Polls `production.json` (30–55 s), `inventory.json` (~20 s) and `home.json` (over 60 s) every 60 s |
| Charge-from-grid switch has no effect | pyenphase writes `/admin/lib/tariff`. On D8.3.6086 the file is saved but the controller ignores it (FINDINGS: "Local writes do not work") |
| Reserve and storage-mode controls are unreliable | Same tariff path. They show values from a stale file that doesn't reflect the live settings |
| No power-flow detail | Doesn't expose per-phase grid, load, PV and storage power from `livedata`, controller mode or setpoint |

## 3. Architecture

```
custom_components/enphase_realtime/
  __init__.py          setup/unload, builds clients + coordinators, stores in entry.runtime_data
  config_flow.py       user step, reauth, options
  const.py
  coordinator.py       StreamCoordinator, FastCoordinator, SlowCoordinator, CloudCoordinator
  entity.py            base entity + DeviceInfo builders
  sensor.py  binary_sensor.py  switch.py  number.py  select.py
  diagnostics.py       redacted dump of every coordinator's last payload
  strings.json  translations/en.json  manifest.json
  envoy_client/        HA-independent async client (aiohttp)
    __init__.py  auth.py  local.py  stream.py  models.py
  enlighten_client/    HA-independent async client (aiohttp)
    __init__.py  session.py  battery.py  models.py
```

- **Transport.** Both clients use `aiohttp` and take a session injected by the caller. HA
  passes `async_get_clientsession(hass)` to the cloud client and
  `async_create_clientsession(hass, verify_ssl=False)` to the Envoy client, because the Envoy
  certificate is self-signed. This keeps aiohttp as the only runtime dependency, since it ships
  with HA; `manifest.json` `requirements` stays empty. This is a deliberate break from
  ha-span-ebus's `requests`: the stream endpoint needs a real async reader.
- **Clients.** Neither client imports `homeassistant`. They parse responses into dataclasses in
  `models.py`, so tests can use the saved fixtures without HA installed.
- **Pytest.** `pythonpath` points at `custom_components/enphase_realtime`, the same as in
  ha-span-ebus.

### 3.1 Coordinators

| Coordinator | Source | Default cadence | Feeds |
|---|---|---|---|
| `StreamCoordinator` | `GET /stream/meter` (server push, `data: {json}` lines, about 1 Hz) | push; entity writes throttled, see 3.2 | Production, net-consumption and total-consumption power per phase; voltage, current, PF, frequency |
| `FastCoordinator` | `/ivp/livedata/status`, `/ivp/ensemble/secctrl`, `/ivp/sc/sched`, `/ivp/ensemble/relay` | 5 s (options: 2–60) | Battery, grid, load and PV power; SoC; controller mode; charge-from-grid in effect; reserve (`VLS_Limit`); relay states; grid presence |
| `SlowCoordinator` | `/ivp/meters/readings`, `/ivp/ensemble/inventory`, `/ivp/ss/dry_contact_settings`, `/ivp/ensemble/dry_contacts`, `/api/v1/production/inverters` | 60 s | Lifetime energy counters, battery and System Controller health, dry-contact state, per-micro watts |
| `CloudCoordinator` | `GET batterySettings/{site}` | 300 s, plus an immediate refresh after each write | Profile (storage mode), backup %, charge-from-grid, `veryLowSoc` and their limits, `pendingGateways` |

**Stream lifecycle.** A long-lived background task reads `/stream/meter`. If the stream drops,
the task reconnects with backoff (1 s up to 60 s). While it is down, stream entities become
unavailable after 30 s, and the fast poll keeps working. If the stream endpoint returns 401 or
404, stream entities are not created, and power values fall back to `livedata`.

**Never polled:** `production.json`, `inventory.json` and `home.json`.

### 3.2 Recorder load

At 1 Hz, about 20 stream entities write roughly 1.7M rows a day. Defaults:

- Stream entities write state at most once every `stream_interval` seconds. The default is 5;
  the options allow 1–60. The coordinator keeps the latest frame, and the entity writes it when
  the throttle window closes.
- Per-phase voltage, current, PF, Q and S, and all `sc/status` diagnostics, are **disabled by
  default** (`entity_registry_enabled_default=False`).
- The README recommends a `recorder: exclude` block for anyone who turns on a 1 s interval.

## 4. Authentication and config flow

### 4.1 Credentials

The user step asks for:

- Envoy host, default `envoy.local`
- Enlighten email and password

From these, setup:

1. Logs in with `POST /login/login.json`, getting `session_id` and hopefully the
   `_enlighten_4_session` cookie (spike S1).
2. Gets the Envoy serial from `GET https://<host>/info` (no auth needed).
3. Gets an owner token with `POST https://entrez.enphaseenergy.com/tokens`
   `{session_id, serial, username}`. This is the same flow the vk2him add-on uses.
4. Resolves `site_id` and `user_id` from the Enlighten session (spike S2). If that fails, the
   flow asks the user for the site ID.
5. Checks everything works:
   - local: `GET /ivp/livedata/status` returns 200
   - cloud: `GET batterySettings` returns 200

**The password is stored in the config entry.** The core integration does the same. The
Enlighten session expires within days (the captured session expired a week after capture), and
the owner token expires within about a year. Asking the user to re-authenticate every week
isn't acceptable. This differs from ha-span-ebus, where the password is used once and
discarded.

### 4.2 Renewal

| Event | Action |
|---|---|
| Envoy 401 | Get a fresh owner token (log in again if needed), retry once, then start reauth |
| Owner token within 30 days of `exp` | Renew in the background, from the slow tick |
| Cloud 302, 401 or 403, or an HTML body | Log in again, retry once, then start reauth |
| Login rejected | `ConfigEntryAuthFailed` starts reauth. **No retry loop**, to avoid an account lockout |

### 4.3 Options

- `fast_interval`
- `stream_interval`
- `cloud_interval`
- `enable_stream` (default on)
- `allow_grid_relay_control` (default **off**, see 6.3)

## 5. Entities and parity with the core integration

**Devices:**

- Envoy (serial)
- System Controller (Enpower serial)
- one device per IQ Battery (serial)
- one device per microinverter, all disabled by default

**Unique IDs:** `<serial>_<key>`.

Source key: **S** = stream, **F** = fast, **L** = slow, **C** = cloud.

### 5.1 Power (real time)

| Entity | Source | Core equivalent |
|---|---|---|
| Production power, total and per phase | S `production` | `current_power_production`, `production_ct_power` |
| Consumption power, total and per phase | S `total-consumption` | `current_power_consumption` |
| Net power, total and per phase (positive = import) | S `net-consumption` | `current_net_power_consumption` |
| Battery power (positive = discharge) | F `livedata.meters.storage.agg_p_mw` / 1000, sign flipped | `current_battery_discharge` |
| Grid power | F `meters.grid.agg_p_mw` | – (new) |
| Load power | F `meters.load.agg_p_mw` | – (new) |
| PV power (livedata) | F `meters.pv.agg_p_mw` | – (new; this is the fallback when the stream is off) |
| Voltage, current, PF, frequency per phase | S | – (new, disabled by default) |

### 5.2 Energy

| Entity | Source | Core equivalent |
|---|---|---|
| Lifetime production, consumption and net import/export | L `/ivp/meters/readings` `actEnergyDlvd` / `actEnergyRcvd` per meter eid (`/ivp/meters` maps eid to type) | `lifetime_energy_*`, `lifetime_net_energy_*`, `production_ct_energy_*` |
| Lifetime battery charged and discharged | L readings for the `storage` meter | `lifetime_battery_energy_*` |
| Energy today, energy last 7 days | **Dropped** | The Envoy only has these in the slow `production.json`. Use HA's `utility_meter` or the Energy dashboard on the lifetime counters (spike S5 checks this) |

All lifetime sensors are `total_increasing` in Wh, so they can go straight into the Energy
dashboard.

### 5.3 Battery and System Controller

| Entity | Source | Core equivalent |
|---|---|---|
| Aggregate SoC | F `secctrl.agg_soc` | `envoy_battery` |
| Available energy, capacity | F `secctrl.ENC_agg_avail_energy`, `Max_energy` | `available_battery_energy`, `battery_capacity` |
| Reserve energy | F `sc/sched['Agg VLS Energy']` | `reserve_battery_energy` |
| Reserve level | F `secctrl.VLS_Limit` | `reserve_battery_level` |
| Backup SoC target | F `secctrl.configured_backup_soc` | – |
| State of health | F `secctrl.ENC_agg_soh` | – (new) |
| Controller mode (enum: ID, ZN, CG, DG, ND, DL, CP, …) | F `sc/sched.acb_current_mode`, labelled using `sched_mode_key` | – (new) |
| Charge from grid in effect (binary) | F `sc/sched['Charge From Grid Allowed']` | – (new; this is the true value) |
| Per-battery: SoC, temperature, max cell temperature, communicating, DC switch, last reported, status | L `ensemble/inventory` ENCHARGE | `encharge_*` |
| System Controller: communicating, temperature, last reported | L `ensemble/inventory` ENPOWER | `enpower_*` |
| Storage mode (read-only sensor) | C `profile` | `storage_mode` select (writable in core; read-only here) |
| Pending cloud change (binary, with `requestedConfig` as attributes) | C `requestedConfig.pendingGateways` non-empty | – (new) |

### 5.4 Grid

| Entity | Source | Core equivalent |
|---|---|---|
| Grid status: on when the relay is actually closed | F `relay.mains_oper_state == "closed"` | `enpower_grid_status` |
| Grid enabled switch | F `mains_admin_state`; write in 6.3 | `enpower_grid_enabled` |
| Grid outage (binary, problem class): on when admin is closed but oper is open | F derived | – (new; this is the signature the existing HA automation already relies on) |

### 5.5 Microinverters

For each micro: last-report watts and last-report time, from L `/api/v1/production/inverters`.
Parity with the core `inverter_*` entities. Disabled by default.

### 5.6 Dry contacts (read-only in v1)

For each contact:

- a binary sensor for relay state (from `ensemble/dry_contacts`)
- diagnostic sensors for `mode`, `grid_action`, `micro_grid_action`, `gen_action`, `soc_low`
  and `soc_high` (from `ss/dry_contact_settings`)

These replace the core's `relay_*_status` switches and the `*_action`, `mode`,
`cutoff_battery_level` and `restore_battery_level` controls. **They are deliberately not
writable** (see 1).

## 6. Control

### 6.1 Write, then confirm locally

Every cloud-backed control works the same way:

1. **Write.** Do a GET on `batterySettings` to refresh the `BP-XSRF-Token` cookie, then send
   the PUT with the `X-XSRF-Token` header.
2. **Show the requested value right away.** The entity takes the new value immediately and
   sets the attribute `confirmation: pending`.
3. **Wait for confirmation.** A `LocalConfirm` watcher on the FastCoordinator checks each tick
   whether the local value now matches.
   - If it matches within **90 s**: set `confirmation: confirmed`.
   - If not: go back to the local value, set `confirmation: failed`, and log a warning.
     **No exception is raised after the service call has already returned.**
4. **Keep showing the local value.** The entity's state is always the local value once one
   exists. The cloud's top-level fields are only used for limits and metadata, because they lag
   5–15 min (FINDINGS: "Pending state").

A failed write (anything other than 200, or an XSRF or auth error) raises
`HomeAssistantError` from the service call and doesn't change the state.

### 6.2 Cloud-backed controls

| Entity | Write | Confirm locally with | Verified? |
|---|---|---|---|
| Charge from grid switch | On: `POST acceptDisclaimer {"disclaimer-type":"itc"}`, then `PUT {"chargeFromGrid":true,"acceptedItcDisclaimer":true,"chargeBeginTime":…,"chargeEndTime":…,"chargeFromGridScheduleEnabled":false}` (keeps the current begin and end times from the last GET). Off: `PUT {"chargeFromGrid":false}` | `sc/sched['Charge From Grid Allowed']` | **Yes**, confirmed within 10–20 s |
| Very-low SoC number | `PUT {"veryLowSoc":N}`; min and max from cloud `veryLowSocMin` / `veryLowSocMax` (5–25) | `secctrl.VLS_Limit == N` | **Yes**, confirmed within about 20 s |
| Backup reserve number | `PUT {"batteryBackupPercentage":N}`; min and max from the cloud | `secctrl.configured_backup_soc == N` | **No (spike S3).** Also only makes sense outside Full Backup, where the cloud pins it at 100. The entity is unavailable when `profile == backup_only` |

Charge-from-grid schedule (`chargeFromGridScheduleEnabled`, begin and end times): exposed as
diagnostic attributes only in v1. Writing them comes later.

### 6.3 Local control: grid relay

- **Switch:** "Grid enabled", on the System Controller device.
- **Write:** the pyenphase call, `POST /ivp/ensemble/relay {"mains_admin_state":"open"|"closed"}`.
  This is **unverified on D8.3.6086 (spike S4)**.
- **Confirm:** `mains_admin_state` and then `mains_oper_state`, within 30 s.
- **Gate:** the switch is only created when the `allow_grid_relay_control` option is on (off by
  default). Taking the house off-grid by accident can't be undone remotely if the battery is low.

### 6.4 Local writes deliberately not used

`/admin/lib/tariff` is not used for anything. It has no effect on the controller (FINDINGS), and
writing to it only makes the stale file drift further from the cloud.

## 7. Errors and availability

- A coordinator failure makes only that coordinator's entities unavailable. A cloud outage never
  touches local telemetry.
- Cloud-backed controls go unavailable when the CloudCoordinator fails. Their confirmation binary
  sensors still come from the local values.
- Timeouts:
  - `/ivp/*`: 10 s each
  - `/ivp/meters/readings`: 20 s
  - cloud calls: 30 s
- The fast and slow endpoints are fetched in parallel within their tick (`asyncio.gather`).
- `diagnostics.py` redacts the token, cookies, email, `site_id`, `user_id` and every serial.

## 8. Testing

- **Fixtures.** `tests/fixtures/` holds sanitized copies of the `data/samples` payloads, with
  serials replaced and `stream_meter.txt` included.
- **Client tests** (no HA installed):
  - parsing each endpoint into models
  - stream line parser: partial lines, keepalive, reconnect
  - sign conventions: storage `agg_p_mw` negative means charging
  - the XSRF refresh-then-write sequence, using a fake aiohttp server
  - the `pendingGateways` parse
- **Confirmation tests:** the `LocalConfirm` state machine, run as a pure function of
  (requested, local value, elapsed time).
- **CI:** the existing workflow (ruff, pytest, hassfest, HACS). HA-level tests with
  `pytest-homeassistant-custom-component` are deferred until phase 2 is done.

## 9. Delivery phases

Each phase is its own `feature/*` branch off `dev`, with a PR into `dev`.

1. **Clients.** `envoy_client` (auth, local, stream) and `enlighten_client` (session, battery),
   plus fixtures and tests. Spikes S1 and S2 resolved.
2. **Read-only integration.** Config flow, the four coordinators, and all read entities in
   section 5, plus diagnostics. **Done when** it runs side by side with the core integration for
   a few days and the values agree.
3. **Battery control.** Charge-from-grid switch and very-low SoC number, with local
   confirmation. The backup-reserve number once S3 passes.
4. **Grid relay control** (once S4 passes) and a migration guide:
   - disable the core integration
   - rename entity IDs to keep automations and history
   - repoint `automation.grid_loss_circuit_shedding`

## 10. Spikes and open questions

| ID | Question | How to settle it |
|---|---|---|
| S1 | Does `POST /login/login.json` set `_enlighten_4_session` (and allow `batterySettings` GETs)? | One login with real credentials; no retry on failure |
| S2 | How to get `site_id` and `user_id` from a session | Check the login.json body; otherwise the Enlighten `search_sites` / `app-api` endpoints; otherwise ask in the config flow |
| S3 | Does `PUT {"batteryBackupPercentage":N}` take effect, and where does it show locally? | Test in Self-Consumption with the user watching, then restore |
| S4 | Does `POST /ivp/ensemble/relay` work with an owner token on D8.3.6086? | Only with the user present, battery SoC above 50%, and an immediate restore |
| S5 | Do `/ivp/meters/readings` lifetime counters match the core's lifetime values, and are they monotonic? | Compare during the phase 2 side-by-side run |
| S6 | Dry-contact mapping (which contact switches the AC, which the dryer) | User task. Writes stay out of scope until it's done |
| S7 | Is it worth adding the `mqttSignedUrl` AWS IoT stream as a push source for cloud state (to replace the 300 s poll)? | Revisit after phase 3 |
