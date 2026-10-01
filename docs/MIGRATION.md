# Moving from the core Enphase Envoy integration

This guide moves a site from Home Assistant's core Enphase Envoy integration to Enphase
Realtime and keeps your automations and history. Enphase Realtime uses the core integration's
device and entity names, so most entities take over the old entity IDs with no editing.

## 1. Run both side by side first

Set up Enphase Realtime while the core integration is still running, and compare the two for a
day or two. Power readings should agree to within a second's worth of change, and the lifetime
energy counters should rise at the same rate.

While both are loaded, most of the new entities get a `_2` suffix, because the core
integration already holds that entity ID: for example `sensor.envoy_<serial>_battery_2` and
`sensor.envoy_<serial>_lifetime_energy_production_2`. That goes away in step 3.

## 2. Remove the core integration

**Settings → Devices & services → Enphase Envoy → ⋮ → Delete.**

Disabling isn't enough: a disabled integration keeps its entities in the registry, and with
them their entity IDs. Deleting frees the IDs. The recorded history stays in the database,
filed under each entity ID, so a new entity that takes over an ID picks up the old history.

## 3. Take over the old entity IDs

Once the core integration is deleted, open each Enphase Realtime entity that got a `_2` suffix
(**Settings → Entities**, search for `_2`, ⚙) and remove the suffix from its **Entity ID**. It
then has the core entity's ID and history.

The entities below have the same ID in both integrations. `<serial>` is the Envoy's serial,
`<sc>` the IQ System Controller's, `<collar>` the IQ Meter Collar's, `<c6>` the C6 Combiner
Controller's, `<battery>` each IQ Battery's and `<inverter>` each microinverter's. The IDs assume you kept the device names both integrations give; if you
renamed a device, check the old IDs in your own entity list.

| Entity ID (core and Enphase Realtime) |
|---|
| `sensor.envoy_<serial>_current_power_production` (and `_l1`, `_l2`, `_l3`) |
| `sensor.envoy_<serial>_current_power_consumption` (and per phase) |
| `sensor.envoy_<serial>_current_net_power_consumption` (and per phase) |
| `sensor.envoy_<serial>_current_battery_discharge` |
| `sensor.envoy_<serial>_lifetime_energy_production` |
| `sensor.envoy_<serial>_lifetime_energy_consumption` |
| `sensor.envoy_<serial>_lifetime_net_energy_consumption` (grid import) |
| `sensor.envoy_<serial>_lifetime_net_energy_production` (grid export) |
| `sensor.envoy_<serial>_lifetime_battery_energy_charged` |
| `sensor.envoy_<serial>_lifetime_battery_energy_discharged` |
| `sensor.envoy_<serial>_battery` |
| `sensor.envoy_<serial>_available_battery_energy` |
| `sensor.envoy_<serial>_battery_capacity` |
| `sensor.envoy_<serial>_reserve_battery_energy` |
| `sensor.envoy_<serial>_reserve_battery_level` |
| `sensor.envoy_<serial>_voltage_net_consumption_ct` and the other CT readings |
| `sensor.encharge_<battery>_battery` |
| `sensor.encharge_<battery>_temperature` |
| `sensor.enpower_<sc>_temperature` |
| `binary_sensor.enpower_<sc>_grid_status` |
| `number.enpower_<sc>_reserve_battery_level` |
| `switch.enpower_<sc>_charge_from_grid` (`switch.envoy_<serial>_charge_from_grid` on a site without a System Controller) |
| `switch.enpower_<sc>_grid_enabled` (only with the grid relay option on; see the README) |
| `sensor.collar_<collar>_temperature`, `_last_reported`, `_admin_state`, `_grid_status`, `_mid_state` |
| `binary_sensor.collar_<collar>_communicating` |
| `sensor.c6_combiner_<c6>_last_reported`, `binary_sensor.c6_combiner_<c6>_communicating` |
| `sensor.inverter_<inverter>` |
| `switch.<contact>`, `select.<contact>_mode`, `_grid_action`, `_microgrid_action`, `_generator_action`, `number.<contact>_cutoff_battery_level`, `_restore_battery_level` (changes need the dry-contact option), where `<contact>` is the contact's load name, or its ID (such as `nc1`) |

Enphase Realtime's **Battery shutdown level** (the level at which the battery stops discharging)
is new: core has no equivalent.

If Home Assistant raises a repair about a changed unit for a sensor's statistics, choose to
update the unit. The old statistics are kept.

Then check the **Energy dashboard** (**Settings → Dashboards → Energy**): its sources should
point to the lifetime sensors above.

## What doesn't carry over

- **Energy today and last 7 days.** This integration doesn't create these. Use the Energy
  dashboard, or a `utility_meter` on the lifetime sensors.
- **Grid enabled** is only created when you turn on the grid relay option. See the README's
  [Grid relay](../README.md#grid-relay) section before you do.
