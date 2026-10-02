# Entity parity with the core integration

Status: implemented, 2026-10-02. Checked against the core integration's own entity registry
listing on the reference site (Home Assistant with pyenphase 4.0.3).

## 1. Goal

A site can swap the core Enphase Envoy integration for this one and keep every automation,
dashboard and statistic. That needs two things:

1. **Every entity the core integration creates exists here, under the same entity ID**, with
   the same unit, device class, state class and enabled-by-default setting.
2. **Entities core doesn't have are named the way core names things**, on the same devices.

This replaces the earlier position in [core-integration.md](core-integration.md) section 5,
which matched names only where an entity already existed here and dropped the rest (energy
today and last seven days, the per-CT readings, most microinverter sensors).

## 2. How the comparison was made

The core integration was loaded on the reference site next to this one (disabled), and its
entity IDs were listed: 484 entities across the Envoy, the System Controller, the battery, 25
microinverters and four dry contacts. Core's sensor descriptions were read from its source for
units, classes, precision, categories and enabled-by-default flags.

Entity IDs come from the device name and the entity name, so matching an ID means matching
both. The device names already matched (`Envoy <serial>`, `Enpower <serial>`,
`Encharge <serial>`, `Inverter <serial>`, `Collar <serial>`, `C6 Combiner <serial>`).

## 3. What was added

### 3.1 New reads

All three are in the slow (60 s) poll and optional: an Envoy that doesn't serve one simply
doesn't get its entities, and the rest of the poll is unaffected.

| Endpoint | Used for |
|---|---|
| `/production.json?details=1` | Energy today and last seven days, the balanced net figures, and their per-phase lines |
| `/ivp/ensemble/power` | Each battery's power and apparent power |
| `/ivp/pdm/device_data` | Each microinverter's DC and AC readings, temperature and energy |

`/ivp/meters` and `/ivp/meters/readings`, already polled, now also give each CT's full reading
(energy delivered and received, power, voltage, current, power factor, frequency), its metering
status and status flags, as a total and per phase.

### 3.2 New entities

On the Envoy device, each as a total and per phase (`_l1`, `_l2`, `_l3`) where the site has
more than one phase:

| Entity ID suffix | Source | Enabled by default |
|---|---|---|
| `energy_production_today`, `energy_production_last_seven_days` | `production.json` | Total yes, phases no |
| `energy_consumption_today`, `energy_consumption_last_seven_days` | `production.json` | Total yes, phases no |
| `lifetime_energy_production`, `lifetime_energy_consumption` per phase | `production.json` | No |
| `balanced_net_power_consumption`, `lifetime_balanced_net_energy_consumption` | `production.json` | No |
| `production_ct_energy_delivered`, `production_ct_energy_received`, `production_ct_power` | readings | Total yes, phases no |
| `lifetime_net_energy_consumption`, `lifetime_net_energy_production` per phase | readings | No |
| `lifetime_battery_energy_charged`, `lifetime_battery_energy_discharged`, `current_battery_discharge` per phase | readings | No |
| `frequency_<ct>`, `voltage_<ct>`, `<ct>_current`, `power_factor_<ct>` | readings | No |
| `metering_status_<ct>`, `meter_status_flags_active_<ct>` (diagnostic) | `/ivp/meters` | No |

`<ct>` is `net_consumption_ct`, `production_ct` or `storage_ct`, and likewise
`total_consumption_ct`, `backfeed_ct`, `load_ct`, `evse_ct` and `pv3p_ct` on a site with those
CTs enabled.

On each battery: `power` and `apparent_power`.

On each microinverter: `dc_voltage`, `dc_current`, `ac_voltage`, `ac_current`, `frequency`,
`temperature`, `lifetime_energy_production`, `energy_production_today`, `last_report_duration`,
`energy_production_since_previous_report` and `lifetime_maximum_power`, all disabled by default
as in core.

### 3.3 Existing entities changed to match core

| Entity | Change |
|---|---|
| Power sensors with a core name | Suggested unit kW with three decimals (native unit still W) |
| Lifetime energy sensors | Suggested unit MWh with three decimals (native unit still Wh) |
| `battery_capacity` | No state class |
| `reserve_battery_level` | Device class battery |
| `last_reported` (battery, System Controller, collar, combiner, microinverter) | No longer diagnostic |
| `temperature` (System Controller, collar) | No longer diagnostic |
| `sensor.inverter_<serial>` (power) | Enabled by default |
| CT frequency, voltage, current, power factor | Core's display precision |

Home Assistant applies a suggested unit and an enabled-by-default flag only when an entity is
first registered. An existing installation keeps what it has; a new one, or one taking over
core's entities, gets core's settings.

## 4. Where a value has two sources

Several core entities are backed by faster data here. One entity ID has one source, and the
first that exists wins:

1. the stream (`/stream/meter`), about once a second;
2. the live and fast polls;
3. the slow poll (`readings`, then `production.json`).

So `current_power_production` comes from the stream where it's available and from
`production.json` otherwise. It used not to exist without the stream; now it always does.

## 5. Following pyenphase

The values must agree with core's, so the parsers follow pyenphase's rules where it has any:

- **Readings are matched to CTs by `eid`**, and only for CTs `/ivp/meters` lists as enabled.
- **Per-phase entities exist only with more than one phase.** A single-phase site gets totals.
- **One-channel storage CT** (firmware 8.3.6000 and later, split phase): when one leg of the
  storage CT reads zero energy and the other carries the whole total, the total and the dead
  leg are reported as unknown.
- **Total consumption repeating net consumption** (firmware 8.3.5433 and later): when
  `production.json`'s total-consumption section has the same power and lifetime energy as
  net-consumption, production is added back. With no usable production figure, total
  consumption is unknown.
- **Production** comes from the production CT's section of `production.json`. The inverters'
  own count is used only on a site with no production CT.
- **Microinverter details** are ignored when the Envoy reports as many devices as its
  `deviceDataLimit`, because it truncates the list there. An inverter's readings are unknown
  while its `lastReading` is empty, which is every night.

## 6. Entities only this integration has

These keep their names, which already follow core's style (sentence case, on the device core
would put them on): Grid, Load and PV power; `voltage_l1_l2`; battery shutdown level, state of
health, scheduler mode and configured reserve level; per-battery status and maximum cell
temperature; the PCS and installer-setting sensors; pending cloud change; the battery
maintenance controls; and each dry contact's manual override.

## 7. Known differences

- **Unnamed dry contacts.** Core names a contact's device after its load name. With an empty
  load name, core's entities have no device name and get IDs that depend on creation order
  (`select.mode`, `select.mode_2`, `number.restore_battery_level_2`,
  `switch.enphase_envoy_<serial>_relay_no1_relay_status`). This integration names such a
  contact after its ID instead (`switch.no1`, `select.no1_mode`). Contacts with a load name
  match core exactly (`switch.load_1`, `select.load_1_mode`). Reproducing core's IDs for
  unnamed contacts would mean depending on creation order, so it isn't done.
- **Grid enabled** (`switch.enpower_<serial>_grid_enabled`) is only created with the grid relay
  option on ([core-integration.md](core-integration.md) section 6.3).
- **Per-phase power from the stream** (`current_power_production_l1` and so on) is enabled by
  default here and disabled in core. The IDs are the same.
- **AC Battery (ACB)** entities aren't created. The integration targets IQ Batteries.
- **Envoys without CTs.** Core supports an unmetered Envoy from the inverters' totals. This
  integration needs the meters (`/ivp/meters`) and doesn't set up without them.

## 8. Testing

- `tests/test_envoy_models.py`: the CT, production report, microinverter detail and battery
  power parsers against the reference captures, and synthetic payloads for each pyenphase rule
  in section 5.
- `tests_ha/test_init.py`: `test_entity_ids_match_the_core_integration` builds core's full
  entity ID list for the reference site (455 entities, without the dry contacts and Grid
  enabled) and checks every one exists; other tests check enabled-by-default flags, categories,
  values, and setup with each new endpoint missing.
- Three new fixtures: `production_details.json`, `ivp_ensemble_power.json`,
  `ivp_pdm_device_data.json` ([fixtures README](../../tests/fixtures/README.md)).

## 9. Process note

The global workflow asks for architect, coder and reviewer agents from `.claude/agents/`. That
directory doesn't exist in this repo, so the spec, the implementation and the review were done
in one session, with the review made against core's descriptions and pyenphase's source.
