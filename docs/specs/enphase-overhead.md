# Enphase overhead from a backup-load sensor

Status: implemented, 2026-09-26; outlier rejection and the 300 s window added the same day after
four hours of data from the reference site (section 6).

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

**What the overhead includes.** Only what draws from the System Controller's side of the Envoy's
CTs. On the reference site the IQ Gateway is powered from a breaker in the System Controller, and
the Neutral Forming Transformer's relay is open (it is idle), so the figure is the Gateway, the
System Controller and the batteries' own draw. On a grid-tied install where the Gateway draws
from the combiner bus ahead of the production CTs, its draw isn't in the overhead at all.

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
| Enphase overhead power | power, measurement, W | Mean of the filtered overhead over the last 300 s |
| Enphase overhead energy | energy, total, Wh | Integral of the filtered overhead since the entity was created |

**When the value is computed.** On every live poll (default 1 s), from that poll's `load` and the
backup-load sensor's current state. The backup-load sensor isn't polled: its latest state is
used as it stands, because sensors such as SPAN's report only when the value changes.

**Averaging.** The two meters are read at different moments and by different devices, so single
samples wobble by tens of watts, the same size as the overhead itself. Each sample first passes
the outlier filter (section 6); the power entity shows the mean of the filtered samples over
300 s. Samples older than that drop out of the window. 60 s, the first choice, still let a
single mistimed step move the mean by tens of watts.

**Units.** The backup-load sensor's value is converted from its `unit_of_measurement` (W, kW,
MW, …) to W. A state that isn't a number, or a unit that isn't a power unit, counts as missing.

**Missing data.** If the backup-load sensor is missing, unavailable or unreadable, or the live
poll has no `load`, no sample is taken. The power entity is unavailable while its window is
empty; the energy entity stays available and stops counting.

The live coordinator notifies its listeners only when a poll's data differs from the last
(`always_update=False`), so a poll identical to the previous one takes no sample. A poll whose
power values are stale is skipped too (section 6).

**Energy.** Each sample adds `overhead × Δt` (left Riemann sum), where Δt is the time since the
previous sample. If more than 30 s has passed, or the previous poll had no sample, nothing is
added for that interval: an outage is a gap, not a guess. The filtered value is integrated, so
a rejected sample counts at the baseline. Negative samples are added as they are, because clipping them would bias the total upwards; the state class is therefore `total`
rather than `total_increasing`, which the Energy dashboard accepts for a device. The total is
restored across restarts.

## 6. Outlier rejection

**Why.** On the reference site (09:39–13:59, 2026-09-26, 1–2.5 kW of load):

- In steady load the overhead's median was 8–9 W (hourly medians 6.7–10.7 W). A fit against
  load gives about 5.5 W + 0.22 % of load, so it is mostly a fixed draw. Load above 2.5 kW
  hasn't been seen yet.
- SPAN's main feed reports a step about 1.4 s after the Envoy (median of the matched steps;
  −0.2 to 3.3 s), sometimes as a ramp over two or three updates, and sometimes shows
  sub-second blips the Envoy never sees.
- The Envoy's livedata stalls: 38 gaps of over 8 s in four hours, clustered around every
  ten minutes, with polls taking 1–2 s and returning stale load (once 1164 W for about 7 s while
  the panel drew 2400 W), and 15 relay timeouts.

Each step in the load therefore shows up as a spike of up to the step's size in the difference,
lasting as long as the two meters disagree. With no filter the published figure ranged from −50
to 312 W over a 300 s window, and the energy averaged 10.4 W against a true 8–9 W.

**How.** The baseline is the median of the last 30 accepted differences. A difference within
150 W of it is accepted; one further away is replaced by the baseline, for both the mean and the
energy. The first 5 samples are accepted as they are, to seed the baseline. The baseline is kept
across gaps, so polling that resumes mid-step is judged against the old level.

A real change in the overhead (the Neutral Forming Transformer closing, say) would be rejected
indefinitely, so after 20 s of continuous rejection a difference is accepted once the raw
differences over the last 5 s lie within 150 W of each other; the baseline then restarts from
it. A timing artefact doesn't hold that long.

Replayed over the same four hours, this gives −19 to 19 W over the 300 s window and an energy
average of 7.8 W. An earlier design that matched each Envoy step with the SPAN's (holding the
last good value until the SPAN caught up) reached −8 to 21 W but failed when the first part of a
ramp was below its step threshold: that part became the "good" value and the hold never ended.
Comparing against a baseline needs no step detection.

Rejections starting and ending are logged at debug level by the sensor platform.

**Stale snapshots.** During a stall the Envoy keeps answering, but with old power values: on
2026-09-26 grid and load held at exactly 1273.456 W from 14:17:36 to 14:18:33 while the SPAN
moved between 1254 and 1368 W, and again for 19 s at 14:27:50. `meters.last_update` kept
advancing through both (the live coordinator logs at debug level if it holds for 3 s, and it
never did), so it can't be used to detect them. The values themselves can: a measured load
doesn't repeat to the milliwatt. A poll whose Envoy load equals the previous poll's is skipped:
it adds nothing to the window, the baseline or the rejection timer, and the previous sample
carries the energy across the stall if it lasts no longer than 30 s. Without this, a step over
150 W during a long stall would be rejected for 20 s and then, the held value being perfectly
steady, accepted as a new baseline. Runs of skipped polls are logged at debug level.

## 7. Tests

- Unit (`tests/test_overhead.py`): the window mean and expiry, unit conversion, the energy sum,
  gaps over 30 s, missing samples, and the outlier filter: seeding, rejection, a step reported
  as a ramp, accepting a lasting change only once steady, and the baseline surviving a gap; a
  repeated Envoy load is skipped, carries energy across a short stall but not a long one, and
  isn't taken as a new level.
- Home Assistant (`tests_ha/test_overhead.py`): the option adds the entities; the values follow
  the fixture's load and a stub backup-load state; the entity goes unavailable when the stub
  does; clearing the option removes the entities; the options form offers the selector; a held
  livedata timestamp is logged.

## 8. Later

- Load above 2.5 kW, to check whether the overhead's dependence on load stays small.
- Diagnostics and a distinct mode for partial-backup sites whose CTs are at the service
  entrance, if one turns up.

The backup-load sensor is taken to report power drawn by the panel as positive. SPAN's main feed
sensor does (confirmed on the reference site, 2026-09-26), as any correctly installed main
monitor should, so there is no invert option.
