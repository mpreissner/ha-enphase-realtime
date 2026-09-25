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
      - sensor.envoy_<serial>_*_power
      - sensor.envoy_<serial>_*_power_*
      - sensor.envoy_<serial>_*_current_*
      - sensor.envoy_<serial>_*_power_factor_*
      - sensor.envoy_<serial>_voltage*
      - sensor.envoy_<serial>_frequency
```

The energy panel reads the lifetime energy sensors, which update slowly and aren't excluded.

**Which sensors to trigger on.** Use the live-poll sensors (grid, load, PV and battery power,
grid status) for time-critical automations. The streamed meter sensors (production,
consumption and net power, and the per-phase readings) average one reading a second, but the
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
the **Grid status** binary sensor; **Grid outage** turns on when the relay is told to stay on the
grid but is open, which is what an outage looks like.

**Status:** pre-alpha, not yet functional.
