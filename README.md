# Enphase Realtime for Home Assistant

A replacement for the Home Assistant core Enphase Envoy integration, built for IQ System
Controller and IQ Battery sites.

- **Real-time telemetry.** Meter data is streamed at about 1 Hz from `/stream/meter`. Power,
  battery and controller state are polled every few seconds from the Envoy's fast `/ivp/*`
  endpoints. Nothing is read from the slow legacy pages.
- **Local control** for the settings the Envoy accepts locally.
- **Cloud control** for the settings it doesn't, such as the battery's charge-from-grid and
  reserve. Every cloud write is confirmed from the local Envoy values, because the cloud only
  updates its own view when the Envoy next reports in.

See [docs/specs/core-integration.md](docs/specs/core-integration.md) for the design and
[docs/FINDINGS.md](docs/FINDINGS.md) for the protocol notes.

**Status:** pre-alpha, not yet functional.
