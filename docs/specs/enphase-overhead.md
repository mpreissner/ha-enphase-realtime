# Enphase overhead from a backup-load sensor

Status: implemented, 2026-09-26.

## 1. Goal

Measure what the Enphase equipment itself draws (the IQ Gateway, the IQ System Controller and
the batteries' own consumption) so it can be tracked like any other circuit, for example as a
device on the Energy dashboard.

## 2. Why the Envoy alone can't do it

On the reference site (checked 2026-09-26 in a test Home Assistant instance), `livedata` `load`
is calculated, not measured: over 71 one-second samples, grid + PV + battery − load averaged
0.0 W, and the sample-to-sample spread (−19 to +16 W) came only from grid and load being read at
slightly different moments. `/ivp/meters` lists a `load` meter, but it is disabled and reads
zero, and the `total-consumption` and `net-consumption` reports are identical. The System
Controller has CTs on its output for its own backup switching, but it doesn't report them to an
owner token.

The equipment's draw therefore sits inside both grid and load, and it can only be separated
by measuring the power into the backed-up panel independently.

## 3. Approach

The user picks an existing Home Assistant power sensor that measures the feed into the
**backup load**: the panel (or subpanel) the System Controller feeds. On the reference site this
is the main feed of a SPAN panel. Then

```
Enphase overhead = Envoy load − backup load
                 = grid + PV + battery − backup load
```

The two lines are the same number, because the Envoy calculates load as grid + PV + battery. So
the overhead is also the comparison between the Envoy's calculated load and the measured one.

This works for full-home and partial backup alike, provided the Envoy's consumption CTs measure
the System Controller's grid input. If they sit at the utility service instead, on a
partial-backup site whose non-backed-up loads are upstream of the System Controller, those loads
land in the overhead too. The README says so.

## 4. Options

One new option, **Backup load sensor** (`backup_load_entity`): an entity selector limited to
`sensor` entities with device class `power`. It is optional; leaving it empty turns the feature
off. Offered on every site: the calculation doesn't need a battery or a System Controller.
Changing it reloads the entry, as every option does.

When the option is cleared, the overhead entities are removed from the entity registry on the
next setup, so they don't linger as "restored" orphans.

## 5. Entities

Both on the Envoy device, next to grid and load power. Unique IDs `<serial>_enphase_overhead_power`
and `<serial>_enphase_overhead_energy`.

| Entity | Class | Value |
|---|---|---|
| Enphase overhead power | power, measurement, W | Mean of the instantaneous overhead over the last 60 s |
| Enphase overhead energy | energy, total, Wh | Integral of the instantaneous overhead since the entity was created |

**When the value is computed.** On every live poll (default 1 s), from that poll's `load` and the
backup-load sensor's current state. The backup-load sensor isn't polled: its latest state is
used as it stands, because sensors such as SPAN's report only when the value changes.

**Averaging.** The two meters are read at different moments and by different devices, so single
samples wobble by tens of watts, the same size as the overhead itself. The power entity shows a
60 s mean. Samples older than 60 s drop out of the window.

**Units.** The backup-load sensor's value is converted from its `unit_of_measurement` (W, kW,
MW, …) to W. A state that isn't a number, or a unit that isn't a power unit, counts as missing.

**Missing data.** If the backup-load sensor is missing, unavailable or unreadable, or the live
poll has no `load`, no sample is taken. The power entity is unavailable while its window is
empty; the energy entity stays available and stops counting.

**Energy.** Each sample adds `overhead × Δt` (left Riemann sum), where Δt is the time since the
previous sample. If more than 30 s has passed, or the previous poll had no sample, nothing is
added for that interval: an outage is a gap, not a guess. Negative samples are added as they
are, because clipping them would bias the total upwards; the state class is therefore `total`
rather than `total_increasing`, which the Energy dashboard accepts for a device. The total is
restored across restarts.

## 6. Tests

- Unit (`tests/test_overhead.py`): the window mean and expiry, unit conversion, the energy sum,
  gaps over 30 s, and missing samples.
- Home Assistant (`tests_ha/test_overhead.py`): the option adds the entities; the values follow
  the fixture's load and a stub backup-load state; the entity goes unavailable when the stub
  does; clearing the option removes the entities; the options form offers the selector.

## 7. Later

- Diagnostics and a distinct mode for partial-backup sites whose CTs are at the service
  entrance, if one turns up.

The backup-load sensor is taken to report power drawn by the panel as positive. SPAN's main feed
sensor does (confirmed on the reference site, 2026-09-26), as any correctly installed main
monitor should, so there is no invert option.
