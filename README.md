# Enphase Realtime for Home Assistant

A replacement for the Home Assistant core Enphase Envoy integration, built for IQ System
Controller and IQ Battery sites.

- **Real-time telemetry.** Meter data is streamed at about 1 Hz from `/stream/meter`, and grid,
  load, PV and battery power and the grid relay are polled every second from the Envoy's fast
  `/ivp/*` endpoints. Battery and controller state are polled every few seconds. Nothing is
  read from the slow legacy pages.
- **Local control** for the settings the Envoy accepts locally: the IQ System Controller's
  grid relay, off by default (see [Grid relay](#grid-relay)).
- **Cloud control** for the settings it doesn't, such as the battery's charge-from-grid and
  reserve. Every cloud write is confirmed from the local Envoy values, because the cloud only
  updates its own view when the Envoy next reports in.

Coming from the core integration? See [docs/MIGRATION.md](docs/MIGRATION.md).

See [docs/specs/core-integration.md](docs/specs/core-integration.md) for the design and
[docs/FINDINGS.md](docs/FINDINGS.md) for the protocol notes.

## Status

**Beta.** Everything in the design is built and covered by tests, but it has only run on one
site: a split-phase US system with an IQ System Controller, one IQ Battery 5P and Envoy
firmware D8.3.6086.

| Feature | State |
|---|---|
| Real-time power, energy, battery and System Controller sensors | Working on the reference site. The lifetime energy counters match the core integration's |
| Charge from grid switch | Working. The cloud write and its local confirmation have been checked on the live system |
| Battery shutdown level number | Working. The cloud write and its local confirmation have been checked on the live system |
| Reserve battery level number | The cloud write is proven. Whether the Envoy reports the new value where the integration looks for it hasn't been checked yet, so confirmation may time out even when the change took effect |
| Grid enabled switch | **Experimental.** Built and tested against captured data, but it has never switched a real relay. Off by default |
| Microinverter sensors | Built. Not checked against a producing array |
| 1 s polling over a full day | Not yet measured. If your Envoy starts timing out, raise the live poll interval |
| Single-phase and three-phase sites, non-US sites | Supported by design, untested. Diagnostics from these sites are very welcome |

## Requirements

- Home Assistant 2026.8 or newer.
- An Enphase IQ Gateway (Envoy) on firmware 7.0 or newer. Older Envoys should stay on the core
  integration.
- An Enphase Enlighten account for the site (the owner's login). It is used to fetch the
  Envoy's owner token and to read and change battery settings.
- Built for sites with an IQ System Controller and IQ Batteries. A site without them gets the
  power and energy sensors only.

## Installation

**HACS:** HACS → ⋮ → **Custom repositories**, add `https://github.com/mpreissner/ha-enphase-realtime`
as an **Integration**, then install **Enphase Realtime** and restart Home Assistant.

**Manual:** copy `custom_components/enphase_realtime` into your Home Assistant
`config/custom_components/` directory and restart.

## Setup

**Settings → Devices & services → Add integration → Enphase Realtime.**

1. Enter the Envoy's host name or IP address and your Enlighten email and password.
2. If the account has more than one site, choose the one this Envoy belongs to.
3. Check the detected phase layout and hardware, and confirm the country and time zone.
   Battery schedules use the site's time zone.

If Enphase later refuses the saved login, Home Assistant asks you to log in again.

**What you get**, on devices named as in the core integration (`Envoy <serial>`,
`Enpower <serial>`, `Encharge <serial>`, `Inverter <serial>`):

- power: production, consumption, net consumption and battery flow, plus grid, load and PV
  power; per-phase voltage, current and power factor (disabled by default)
- lifetime energy counters for the Energy dashboard
- battery: charge, available energy, capacity, reserve, state of health, and per-battery
  status and temperatures
- IQ System Controller: grid status, temperature and communication status
- battery settings from the cloud: storage mode (read-only), charge from grid switch, battery
  shutdown level and reserve battery level numbers, and a "Pending cloud change" sensor
- dry contacts: state and settings (no controls yet)
- microinverters: last reported power and time (disabled by default)

The full list, with the core integration's equivalent for each entity, is in section 5 of the
[spec](docs/specs/core-integration.md#5-entities-and-parity-with-the-core-integration).

## Update rates and the recorder

By default the power sensors update about once a second, so that automations such as load
shedding can react quickly. Two options set the rates (**Settings → Devices & services →
Enphase Realtime → Configure**):

- **Live poll interval** (1–60 s, default 1): grid, load, PV and battery power, and grid status.
- **Stream write interval** (0–60 s, default 0): how often the streamed meter readings are
  written. 0 writes every frame (about 1 Hz).

If you only want these values for dashboards and the energy panel, set both to 5.

At 1 s the recorder stores tens of thousands of rows per entity per day. If you keep the fast
rates but don't need their history at full resolution, leave these entities out of the
recorder. Replace `<serial>` with your Envoy's serial number:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.envoy_<serial>_current_*
      - sensor.envoy_<serial>_*_power
      - sensor.envoy_<serial>_*_ct*
      - sensor.envoy_<serial>_voltage_l1_l2
```

The energy panel reads the lifetime energy sensors, which update slowly and aren't excluded.

**Which sensors to trigger on.** Use the live-poll sensors (grid, load, PV and battery power,
grid status) for time-critical automations. The streamed meter sensors (current power
production, consumption and net consumption, and the per-phase readings) average one reading a second, but the
Envoy sometimes holds the stream for several seconds and then catches up in a burst.

**Load shedding and the battery's own protection.** A 1 s update still has to pass through
Home Assistant, your automation and the switch or relay it drives. An IQ Battery can hit its
overload limit faster than that. Treat automations as a way to avoid reaching the limit, not as
overload protection: for loads that must never trip the battery, use hardware load control
(the IQ System Controller's load-control relays, or a smart panel).

## Grid relay

On a site with an IQ System Controller, the option **Allow switching the grid relay** creates a
**Grid enabled** switch on the System Controller. Off opens the main relay and the house runs
from the battery; on closes it again. The option is off by default: if the house is taken off
the grid by mistake and Home Assistant or the network goes down with it, it can't be put back
remotely, and the battery keeps draining until someone switches it back at the System
Controller or in the Enphase app.

Before every write the integration asks Enphase the same question the app asks
(`grid_control_check`). If Enphase flags anything, or can't be reached, the relay isn't touched
and the service call fails with the reason. The switch then shows the requested state until the
Envoy reports that the relay has actually moved, and marks it `confirmation: confirmed`, or
`failed` if it hasn't moved after 30 s.

The switch shows what the relay has been told. For whether the house is actually on the grid, use
the **Grid status** binary sensor on the IQ System Controller device. Grid status off with Grid
enabled on means the grid has gone.

The relay write hasn't yet been tried on a live system (see [Status](#status)). Test it once
while you're at the System Controller, with the battery well charged.

## Known limitations

- **Storage mode** (Self-Consumption, Full Backup and so on) is read-only. Change it in the
  Enphase app.
- **Dry contacts** have no controls yet. The Envoy may well accept dry-contact writes, but
  they haven't been tested, so the integration only shows their state and settings.
- **Energy today and last 7 days** aren't provided. Use the Energy dashboard, or a
  `utility_meter` on the lifetime sensors.
- **Battery settings need the cloud.** The Envoy ignores local battery writes on current
  firmware, so these controls stop working when Enphase's servers or your internet
  connection are down. The sensors keep working, because they're all read locally.
- **Charging the battery from the grid counts as consumption.** It raises lifetime energy
  consumption, as the Envoy calculates it. Charging from PV probably doesn't, but that hasn't
  been checked yet.
- **Charge from grid** is only offered where the site's Enphase settings allow it. The
  disclaimer it needs has only been checked for US sites.

## Reporting problems

Open an [issue](https://github.com/mpreissner/ha-enphase-realtime/issues) and attach the
integration's diagnostics: **Settings → Devices & services → Enphase Realtime → ⋮ → Download
diagnostics**. Serial numbers, tokens and login details are removed from the download. Reports
from single-phase, three-phase and non-US sites help most.
