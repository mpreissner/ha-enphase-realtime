# Moving from the core Enphase Envoy integration

This guide moves a site from Home Assistant's core Enphase Envoy integration to Enphase
Realtime and keeps your automations and history. Enphase Realtime creates every entity
the core integration does, under the same device and entity names, so the old entity IDs carry
over with no editing. The few exceptions are under
[What doesn't carry over](#what-doesnt-carry-over).

## 1. Run both side by side first

Set up Enphase Realtime while the core integration is still running, and compare the two for a
day or two. Power readings should agree to within a second's worth of change, and the lifetime
energy counters should rise at the same rate.

While both are loaded, the new entities that match a core one get a `_2` suffix, because the
core integration already holds that entity ID: for example `sensor.envoy_<serial>_battery_2` and
`sensor.envoy_<serial>_lifetime_energy_production_2`. That goes away in step 3.

## 2. Remove the core integration

**Settings → Devices & services → Enphase Envoy → ⋮ → Delete.**

Disabling isn't enough: a disabled integration keeps its entities in the registry, and with
them their entity IDs. Deleting frees the IDs. The recorded history stays in the database,
filed under each entity ID, so a new entity that takes over an ID picks up the old history.

## 3. Take over the old entity IDs

Once the core integration is deleted, have Home Assistant work the entity IDs out again. Each
entity then gets the core entity's ID and history.

1. Open **Settings → Entities**.
2. Filter by **Integration: Enphase Realtime**, and clear the status filter so that disabled
   entities are listed too.
3. Turn on selection mode and select all.
4. **⋮ → Recreate entity IDs of selected.**

For one device only, its page has the same action under **⋮ → Recreate entity IDs**. You can
also rename entities one at a time: search for `_2`, open the entity's settings (⚙) and remove
the suffix from its **Entity ID**.

While it renames, Home Assistant may log "Cannot migrate history … already in use" for each
entity. That is expected: the ID already has the core entity's history, and the entity carries
on from it.

If you'd rather not rename anything, skip step 1: delete the core integration first and then
add Enphase Realtime. Its entities are then created with the old IDs directly.

The entities below have the same ID in both integrations. `<serial>` is the Envoy's serial,
`<sc>` the IQ System Controller's, `<collar>` the IQ Meter Collar's, `<c6>` the C6 Combiner
Controller's, `<battery>` each IQ Battery's and `<inverter>` each microinverter's. The IDs assume you kept the device names both integrations give; if you
renamed a device, check the old IDs in your own entity list.

| Entity ID (core and Enphase Realtime) |
|---|
| **Envoy: power and energy.** Each also per phase (`_l1`, `_l2`, `_l3`) on a site with more than one phase |
| `sensor.envoy_<serial>_current_power_production`, `_current_power_consumption`, `_current_net_power_consumption`, `_current_battery_discharge` |
| `sensor.envoy_<serial>_balanced_net_power_consumption` |
| `sensor.envoy_<serial>_energy_production_today`, `_energy_production_last_seven_days`, `_lifetime_energy_production` |
| `sensor.envoy_<serial>_energy_consumption_today`, `_energy_consumption_last_seven_days`, `_lifetime_energy_consumption` |
| `sensor.envoy_<serial>_lifetime_net_energy_consumption` (grid import), `_lifetime_net_energy_production` (grid export), `_lifetime_balanced_net_energy_consumption` |
| `sensor.envoy_<serial>_lifetime_battery_energy_charged`, `_lifetime_battery_energy_discharged` |
| `sensor.envoy_<serial>_production_ct_energy_delivered`, `_production_ct_energy_received`, `_production_ct_power` |
| **Envoy: CT readings**, where `<ct>` is `net_consumption_ct`, `production_ct` or `storage_ct`. Each also per phase |
| `sensor.envoy_<serial>_frequency_<ct>`, `_voltage_<ct>`, `_<ct>_current`, `_power_factor_<ct>` |
| `sensor.envoy_<serial>_metering_status_<ct>`, `_meter_status_flags_active_<ct>` |
| **Envoy: battery totals** |
| `sensor.envoy_<serial>_battery`, `_available_battery_energy`, `_battery_capacity`, `_reserve_battery_energy`, `_reserve_battery_level` |
| **IQ Battery** |
| `sensor.encharge_<battery>_battery`, `_temperature`, `_power`, `_apparent_power`, `_last_reported` |
| `binary_sensor.encharge_<battery>_communicating`, `_dc_switch` |
| **IQ System Controller** |
| `sensor.enpower_<sc>_temperature`, `_last_reported` |
| `binary_sensor.enpower_<sc>_communicating`, `_grid_status` |
| `number.enpower_<sc>_reserve_battery_level`, `select.enpower_<sc>_storage_mode` |
| `switch.enpower_<sc>_charge_from_grid` (`switch.envoy_<serial>_charge_from_grid` on a site without a System Controller) |
| `switch.enpower_<sc>_grid_enabled` (only with the grid relay option on; see the README) |
| **IQ Meter Collar and C6 Combiner Controller** |
| `sensor.collar_<collar>_temperature`, `_last_reported`, `_admin_state`, `_grid_status`, `_mid_state` |
| `binary_sensor.collar_<collar>_communicating` |
| `sensor.c6_combiner_<c6>_last_reported`, `binary_sensor.c6_combiner_<c6>_communicating` |
| **Microinverters** |
| `sensor.inverter_<inverter>` (power), `_last_reported` |
| `sensor.inverter_<inverter>_dc_voltage`, `_dc_current`, `_ac_voltage`, `_ac_current`, `_frequency`, `_temperature` |
| `sensor.inverter_<inverter>_energy_production_today`, `_lifetime_energy_production`, `_energy_production_since_previous_report`, `_last_report_duration`, `_lifetime_maximum_power` |
| **Dry contacts with a load name**, where `<contact>` is that name (such as `load_1`). Changes need the dry-contact option |
| `switch.<contact>`, `select.<contact>_mode`, `_grid_action`, `_microgrid_action`, `_generator_action` |
| `number.<contact>_cutoff_battery_level`, `_restore_battery_level` |
| **Dry contacts without a load name.** The first such contact in the System Controller's order (NC1, NC2, NO1, NO2) has no suffix; the next ones end in `_2`, `_3`, `_4` |
| `switch.enphase_envoy_<sc>_relay_<terminal>_relay_status`, where `<terminal>` is `nc1`, `nc2`, `no1` or `no2` |
| `select.mode`, `select.grid_action`, `select.microgrid_action`, `select.generator_action` |
| `number.envoy_<envoy>_cutoff_battery_level`, `number.restore_battery_level` |

The same entities are enabled by default as in core. The ones core leaves disabled (the
per-phase sensors, most CT readings and all microinverter sensors but power) are disabled here
too. If you had enabled one in core, enable it again here; it keeps its ID and history.

Enphase Realtime's other entities, such as Grid, Load and PV power and **Battery shutdown
level** (the level at which the battery stops discharging), are new: core has no equivalent.

If Home Assistant raises a repair about a changed unit for a sensor's statistics, choose to
update the unit. The old statistics are kept.

Then check the **Energy dashboard** (**Settings → Dashboards → Energy**): its sources should
point to the lifetime sensors above. The README's
[Energy dashboard](../README.md#energy-dashboard) section lists which entity goes in which
field.

## What doesn't carry over

- **Dry contacts without a load name, in three cases.** Their entity IDs carry over unless:
  - you renamed the core integration's entry. Core's cutoff level takes its ID from the entry's
    title, and Enphase Realtime assumes the default, `Envoy <serial>`;
  - a load name was added or removed after core created the entities. Core keeps the IDs from
    when it was set up; Enphase Realtime numbers the contacts that are unnamed now;
  - another integration already uses a bare ID such as `select.mode`. Home Assistant then
    hands out the next free number.

  In those cases, rename the entities in Home Assistant or edit the automations.
- **AC Battery (ACB) entities.** Enphase Realtime supports IQ Batteries only.
- **Grid enabled** is only created when you turn on the grid relay option. See the README's
  [Grid relay](../README.md#grid-relay) section before you do.
