# Spec: Enphase Realtime core integration

Status: draft, 2026-09-24 (revised the same day after a review of the captured samples)
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

**Audience.** The reference system is a single North American split-phase site, but the
integration is published through HACS. It has to work on every grid layout Enphase sells the
System Controller for: single-phase (typically 230 V, 50 Hz), split-phase (120/240 V, 60 Hz) and
three-phase (230/400 V, 50 Hz). It also has to cope with sites that lack some of the hardware.
Section 3.3 covers how it adapts. Anything verified only on the reference system is marked as
such.

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
| `SlowCoordinator` | `/ivp/meters`, `/ivp/meters/readings`, `/ivp/meters/reports`, `/ivp/ensemble/inventory`, `/ivp/ss/dry_contact_settings`, `/ivp/ensemble/dry_contacts`, `/api/v1/production/inverters` | 60 s | Meter layout (phase check, 3.3), lifetime energy counters, battery and System Controller health, dry-contact state, per-micro watts |
| `CloudCoordinator` | `GET batterySettings/{site}` | 300 s, plus an immediate refresh after each write | Profile (storage mode), backup %, charge-from-grid, `veryLowSoc` and their limits, `pendingGateways` |

`/info` and the cloud `siteSettings` are read at setup and on reload only. They aren't polled.

**Units on the wire.** The stream reports W, var, VA, V, A and Hz. `livedata` reports every
`*_p_mw` field in **milliwatts** and every `*_s_mva` field in milli-VA, so all of them are
divided by 1000 (for example `grid.agg_p_mw` 1213800 = 1213.8 W, which matches the stream's
1173 W from the same second). Meter readings and reports are in Wh.

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

### 3.3 Site configuration: phases, region and hardware

The integration learns each site's layout at setup and stores it in the config entry. There
are three parts, each taken from the source that knows it best.

#### Phase layout: from the Envoy

Location can't tell you the phase layout. Many homes in three-phase countries are single-phase,
and North American sites can be split-phase or (commercially) three-phase. The Envoy's CT
configuration is what counts:

| Source | Field | Reference site |
|---|---|---|
| `/ivp/meters` (per meter) | `phaseMode`, `phaseCount` | `"split"`, `2` |
| `/ivp/livedata/status` | `meters.is_split_phase`, `meters.phase_count` | `1`, `2` |

The resulting `phase_layout` is `single`, `split` or `three`. Rules:

- Use the `phaseMode` of the **enabled** production meter. If it's missing or unrecognised, fall
  back to `phase_count` (1 = single, 3 = three, 2 with `is_split_phase` = split). Only
  `"split"` has been seen; the spellings for single-phase and three-phase are unverified
  (spike S9), so the parser has to accept unknown strings without failing.
- **Frames always carry `ph-a`, `ph-b` and `ph-c`, whatever the layout.** On the split-phase
  reference site `ph-c` is present and all zeros. Per-phase entities are created from
  `phase_layout`, never from which keys appear in a frame.
- Entity names use the installer's terms: **L1, L2, L3** (`ph-a`, `ph-b`, `ph-c`). Unique IDs use
  the Envoy's keys (`<serial>_net_power_ph_a`), so they stay the same if names change.
- Single-phase: totals only, with no per-phase entities, because the total is the one phase.
- Split-phase only: an L1–L2 voltage sensor (`v_a + v_b`, since the legs are 180° apart),
  disabled by default. It's the 240 V figure North American users expect.
- The slow tick compares `/ivp/meters` against the stored layout. If an installer has changed
  the CT setup, the integration raises a repair issue asking the user to reload. It doesn't
  rebuild entities in place.

#### Region: from Enlighten, confirmed by the user

The cloud `GET /service/batteryConfig/api/v1/siteSettings/{site}` returns the site's registered
`countryCode`, `region`, `timezone`, `locale` and `isEmea` (reference site: `US`, `US`,
`US/Eastern`, `en`). That is the installer's registration, so it is the primary source. The
config flow shows the country and time zone prefilled from it; if the cloud call fails, they're
prefilled from `hass.config.country` and `hass.config.time_zone`. The user confirms or corrects
them, and they can change them later in the options.

Region is used for:

- **Cloud behaviour that varies by market.** The charge-from-grid disclaimer
  (`"disclaimer-type":"itc"`) is the US Investment Tax Credit, and other markets presumably use
  another type or none. `siteSettings` also has `showChargeFromGrid` and `restrictCfg`. Until
  spike S8 is settled, the charge-from-grid switch is created only when `showChargeFromGrid` is
  true and `restrictCfg` is false, and the ITC disclaimer is sent only when `countryCode` is
  `US`.
- **Times of day.** `chargeBeginTime` and `chargeEndTime` are minutes after local midnight in the
  site's time zone, not HA's. They're shown using the site time zone.
- **Hosts.** `enlighten.enphaseenergy.com` and `entrez.enphaseenergy.com` are assumed to serve
  every region. That hasn't been checked outside the US (S8). The hosts live in `const.py` so a
  regional override is a one-line change.

Region is **not** used for units. Home Assistant converts to the user's unit system, so every
entity reports its native unit (section 5).

#### Hardware: from inventory and `siteSettings`

Only what the site has is created:

| Condition | Effect |
|---|---|
| No `ENCHARGE` in `/ivp/ensemble/inventory` (and `hasEncharge` false) | No battery devices, battery sensors or battery controls. The CloudCoordinator isn't created, and the cloud login is used only for the owner token |
| No `ENPOWER` (`hasEnpower` false) | No System Controller device, grid relay entities or dry contacts |
| Storage meter disabled in `/ivp/meters` | No battery lifetime energy counters |
| Several `ENCHARGE` devices | One device each. Aggregate SoC and energy come from `secctrl` |
| Stream returns 401 or 404 | See 3.1: no stream entities, `livedata` fallback |

The README lists the verified hardware (at first only the reference system) and asks users with
other layouts to attach a diagnostics dump to an issue. Those dumps are how S9 gets settled.

## 4. Authentication and config flow

### 4.1 Credentials

The user step asks for:

- Envoy host, default `envoy.local`
- Enlighten email and password

From these, setup:

1. Logs in with `POST /login/login.json` (form fields `user[email]`, `user[password]`), getting
   `session_id`. In the app capture the `_enlighten_4_session` cookie holds that same value, so
   the client sets the cookie itself if the login response doesn't (spike S1).
2. Gets the Envoy serial from `GET https://<host>/info` (no auth needed). The response is
   **XML**, not JSON: the serial is `envoy_info/device/sn` and the firmware is
   `envoy_info/device/software` (for example `D8.3.6086`). Firmware below 7 is rejected with a
   clear error (see non-goals).
3. Gets an owner token with `POST https://entrez.enphaseenergy.com/tokens`
   `{session_id, serial_num, username}`; the response body is the token itself. This is the
   same flow the vk2him add-on uses.
4. Resolves `site_id` and `user_id` from the Enlighten session (spike S2). The app capture shows
   `GET /app-api/search_sites.json?searchText=&favourite=true` returning
   `sites[{id, path, title, favourite}]`. That gives the site ID, but two of the four captured
   calls returned an empty list, and it doesn't give `user_id`. `user_id` is in the `data`
   claim of the `enlighten_manager_token_production` cookie JWT (and may be in the login body).
   The non-empty responses list the
   same site twice, so dedupe on `id` before counting. If more than one site remains, the flow
   asks the user to pick; if there's none, it asks for the site ID.
5. Reads `siteSettings` for the region and hardware flags, and `/ivp/meters` for the phase
   layout (3.3).
6. Checks everything works:
   - local: `GET /ivp/livedata/status` returns 200
   - cloud: `GET batterySettings` returns 200 (only on sites with a battery)
7. **Confirm step.** Shows the detected phase layout, country, time zone and hardware (for
   example "Split-phase · US · US/Eastern · 1 IQ Battery, System Controller"). The user can
   correct the country and time zone here. The phase layout is shown but can't be edited,
   because it has to match the CTs.

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
- `country` and `time_zone` (prefilled as in 3.3)

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
| Battery power (positive = discharge) | F `livedata.meters.storage.agg_p_mw` / 1000, raw sign (livedata balances as load = grid + pv + storage, so positive already means discharging) | `current_battery_discharge` |
| Grid power | F `meters.grid.agg_p_mw` / 1000 | – (new) |
| Load power | F `meters.load.agg_p_mw` / 1000 | – (new) |
| PV power (livedata) | F `meters.pv.agg_p_mw` / 1000 | – (new; this is the fallback when the stream is off) |
| Voltage, current, PF, frequency per phase | S | – (new, disabled by default) |
| L1–L2 voltage (split-phase only) | S `v_a + v_b` | – (new, disabled by default) |

"Per phase" means one entity per phase in `phase_layout` (3.3): none for single-phase, L1 and
L2 for split-phase, and L1 to L3 for three-phase. The same goes for the per-phase `livedata`
fields (`agg_p_ph_a_mw` and so on), which are exposed as disabled-by-default fallbacks when the
stream is off.

### 5.2 Energy

| Entity | Source | Core equivalent |
|---|---|---|
| Lifetime production | L `/ivp/meters/readings`, production meter `actEnergyDlvd` | `lifetime_energy_*`, `production_ct_energy_*` |
| Lifetime grid import and export | L readings, net-consumption meter: `actEnergyDlvd` = import, `actEnergyRcvd` = export | `lifetime_net_energy_*` |
| Lifetime consumption | L `/ivp/meters/reports`, `total-consumption` `cumulative.whDlvdCum` | `lifetime_energy_*` |
| Lifetime battery charged and discharged | L readings, storage meter (which field is which: spike S5) | `lifetime_battery_energy_*` |
| Energy today, energy last 7 days | **Dropped** | The Envoy only has these in the slow `production.json`. Use HA's `utility_meter` or the Energy dashboard on the lifetime counters (spike S5 checks this) |

**Why two endpoints.** Neither one has everything:

- `readings` is keyed by meter `eid`, and `/ivp/meters` maps each eid to a `measurementType`.
  It keeps import and export apart. It has no total-consumption meter, because a
  total-consumption CT doesn't exist: the Envoy calculates it.
- `reports` is keyed by `reportType` and includes that calculated `total-consumption`. But its
  `net-consumption` report holds only the **net** figure. On the reference site
  `whDlvdCum` = 753,640 Wh = readings import 1,124,885 − export 371,245, and `whRcvdCum` = 0.
  That can't feed the Energy dashboard's separate import and export.
- Reference-site check: reports `total-consumption` 1,496,484 = production 742,844 + net
  753,640. Battery flows are **not** netted out, so charging the battery counts as
  consumption. The README states this.

**Parsing rules.** `readings` can include eids that `/ivp/meters` doesn't list. The reference
site has an extra `1023410688`, probably an internal or IQ Meter Collar channel. Skip any eid
that isn't mapped, and any meter whose `state` isn't `enabled`. Create a counter only when its
meter is enabled.

All lifetime sensors are `total_increasing` in Wh, so they can go straight into the Energy
dashboard. They're totals across all phases; `readings` also has per-phase `channels`, which
aren't exposed in v1.

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

**Temperature units.** Each device reports temperature in its own unit, and the sensor declares
that unit as its native unit so HA converts it for the user. On the reference system, IQ
Battery `temperature` and `maxCellTemp` are **°C** (23) and the System Controller's
`temperature` is **°F** (77, from the same moment). That matches pyenphase. It hasn't been
checked on other System Controller models, so the unit is set per device type in one place in
`const.py`.

**No pending change.** When nothing is pending, the `batterySettings` response has
`requestedConfig: {}` with no `pendingGateways` key. A missing key or an empty object means
nothing is pending, and it isn't a parse error.

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
diagnostic attributes only in v1. Writing them comes later. The times are minutes after local
midnight in the site's time zone (3.3).

**Regional gating.** The charge-from-grid switch exists only where the site's `siteSettings`
allow it and, for the disclaimer, only as described in 3.3. The ITC disclaimer body above is
verified for the US only. The reference site shows the cloud's `hideChargeFromGrid: true` while
in `backup_only` even though `cfgControl.show` is true, so visibility must not rely on
`hideChargeFromGrid` alone.

### 6.3 Local control: grid relay

- **Switch:** "Grid enabled", on the System Controller device.
- **Write:** the pyenphase call, `POST /ivp/ensemble/relay {"mains_admin_state":"open"|"closed"}`.
  This is **unverified on D8.3.6086 (spike S4)**.
- **Pre-check:** before the app toggles the grid, it calls
  `GET /app-api/{site}/grid_control_check.json`, which returns `disableGridControl`,
  `activeDownload`, `sunlightBackupSystemCheck`, `gridOutageCheck` and
  `userInitiatedGridToggle`. The integration makes the same call and refuses the write
  (raising `HomeAssistantError` with the reason) if any of them blocks it. What each flag
  means is part of S4; if the cloud is unreachable, the write is refused.
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
  - `/ivp/meters/readings` and `/ivp/meters/reports`: 20 s
  - cloud calls: 30 s
- The fast and slow endpoints are fetched in parallel within their tick (`asyncio.gather`).
- `diagnostics.py` redacts the token, cookies, email (including `siteSettings`
  `ownerOrHostMaskedEmail`), `site_id`, `user_id`, every serial, `euaid`, the zip code and
  site titles. It includes the detected phase layout, region and hardware (3.3), plus the raw
  payloads, because those dumps are how other layouts get verified (S9).

## 8. Testing

- **Fixtures.** `tests/fixtures/reference/` holds sanitized copies of the reference-site
  captures (from the sibling `enphase-envoy-mqtt-json/data/samples`, which is never committed).
  Serials, `euaid`, site IDs and user IDs are replaced with placeholders; see
  `tests/fixtures/README.md`. Payloads for other layouts go in `tests/fixtures/<layout>/`
  (`single-phase/`, `three-phase/`) once users contribute diagnostics dumps. Until then, tests
  for those layouts use payloads built from the reference ones, clearly marked as synthetic.
- **Client tests** (no HA installed):
  - parsing each endpoint into models, including `/info` XML
  - stream line parser: partial lines, keepalive, reconnect
  - sign conventions: storage `agg_p_mw` negative means charging
  - unit conversion: `livedata` mW to W
  - phase layout detection (3.3) for all three layouts, including unknown `phaseMode` strings
    and an all-zero `ph-c`
  - energy mapping: readings eid mapping via `/ivp/meters`, unmapped and disabled eids skipped,
    total consumption taken from reports
  - the XSRF refresh-then-write sequence, using a fake aiohttp server
  - the `pendingGateways` parse, including a missing key
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
   - repoint automations that use the core grid-status entities (on the reference site,
     `automation.grid_loss_circuit_shedding`) to the new grid outage binary sensor

## 10. Spikes and open questions

| ID | Question | How to settle it |
|---|---|---|
| S1 | Does `POST /login/login.json` set `_enlighten_4_session` (and allow `batterySettings` GETs)? | **Mostly settled:** the cookie's value equals the login `session_id`, so the client sets it when the login doesn't. Confirm with one real login (`tools/spike_login.py`) |
| S2 | How to get `site_id` and `user_id` from a session | **Partly settled:** `search_sites.json?favourite=true` returns `sites[].id`, but was empty in 2 of 4 captured calls (4.1). `user_id` is `data.user_id` in the `enlighten_manager_token_production` cookie JWT; the client also checks the login body and a `manager_token` field. Still needed: which of those a fresh login provides (`tools/spike_login.py`), and why `search_sites` is sometimes empty |
| S3 | Does `PUT {"batteryBackupPercentage":N}` take effect, and where does it show locally? | Test in Self-Consumption with the user watching, then restore |
| S4 | Does `POST /ivp/ensemble/relay` work with an owner token on D8.3.6086? | Only with the user present, battery SoC above 50%, and an immediate restore |
| S5 | Do `/ivp/meters/readings` and `reports` lifetime counters match the core's lifetime values, and are they monotonic? Which storage field is charged and which discharged? (Reference site: `actEnergyDlvd` 626 Wh, `actEnergyRcvd` 13,560 Wh, on a new battery that spent the test day charging from grid, which suggests `Rcvd` = charged) | Compare during the phase 2 side-by-side run; check which storage counter rises while `agg_p_mw` is negative |
| S6 | Dry-contact mapping (which contact switches the AC, which the dryer) | User task. Writes stay out of scope until it's done |
| S7 | Is it worth adding the `mqttSignedUrl` AWS IoT stream as a push source for cloud state (to replace the 300 s poll)? | Revisit after phase 3 |
| S8 | Outside the US: do the same Enlighten and Entrez hosts work, which charge-from-grid disclaimer type (if any) is needed, and what do `showChargeFromGrid` and `restrictCfg` look like? | Needs a non-US tester. Until then, the conservative gating in 3.3 applies |
| S9 | What do single-phase and three-phase sites send for `phaseMode`, `phase_count`, `is_split_phase`, stream frames and `livedata` per-phase fields? What temperature unit do other System Controller models report? | Diagnostics dumps from users (3.3). Add each as a fixture under `tests/fixtures/<layout>/` |
