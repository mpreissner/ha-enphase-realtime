# Moving from the core Enphase Envoy integration

This guide moves a site from Home Assistant's core Enphase Envoy integration to Enphase
Realtime and keeps your automations and history. It takes about half an hour, most of it
renaming entities.

## 1. Run both side by side first

Set up Enphase Realtime while the core integration is still running, and compare the two for a
day or two. Power readings should agree to within a second's worth of change, and the lifetime
energy counters should rise at the same rate.

While both are loaded, a few of the new entities get a `_2` suffix, because the core
integration already holds that entity ID: for example `sensor.envoy_<serial>_battery` and
`sensor.envoy_<serial>_available_battery_energy`. That goes away in step 3.

## 2. Remove the core integration

**Settings → Devices & services → Enphase Envoy → ⋮ → Delete.**

Disabling isn't enough: a disabled integration keeps its entities in the registry, and with
them their entity IDs. Deleting frees the IDs. The recorded history stays in the database,
filed under each entity ID, so a new entity that takes over an ID picks up the old history.

## 3. Take over the old entity IDs

For each entity your automations, dashboards or the Energy dashboard use, open the new entity
(**Settings → Entities**, search for it, ⚙) and set its **Entity ID** to the old one. Remove
the `_2` suffix from any entity that got one in step 1.

Typical mappings are below. `<serial>` is the Envoy's serial, `<sc>` the IQ System Controller's
and `<battery>` each IQ Battery's. Check the old IDs in your own entity list: they depend on the
names your devices had.

| Core entity | Enphase Realtime entity |
|---|---|
| `sensor.envoy_<serial>_current_power_production` | `sensor.envoy_<serial>_production_power` |
| `sensor.envoy_<serial>_current_power_consumption` | `sensor.envoy_<serial>_consumption_power` |
| `sensor.envoy_<serial>_current_net_power_consumption` | `sensor.envoy_<serial>_net_power` |
| `sensor.envoy_<serial>_current_battery_discharge` | `sensor.envoy_<serial>_battery_power` |
| `sensor.envoy_<serial>_lifetime_energy_production` | `sensor.envoy_<serial>_lifetime_production` |
| `sensor.envoy_<serial>_lifetime_energy_consumption` | `sensor.envoy_<serial>_lifetime_consumption` |
| `sensor.envoy_<serial>_lifetime_net_energy_consumption` | `sensor.envoy_<serial>_lifetime_grid_import` |
| `sensor.envoy_<serial>_lifetime_net_energy_production` | `sensor.envoy_<serial>_lifetime_grid_export` |
| `sensor.envoy_<serial>_lifetime_battery_energy_charged` | `sensor.envoy_<serial>_lifetime_battery_charged` |
| `sensor.envoy_<serial>_lifetime_battery_energy_discharged` | `sensor.envoy_<serial>_lifetime_battery_discharged` |
| `sensor.envoy_<serial>_battery` | `sensor.envoy_<serial>_battery` (same ID) |
| `sensor.envoy_<serial>_available_battery_energy` | same ID |
| `sensor.envoy_<serial>_battery_capacity` | same ID |
| `sensor.envoy_<serial>_reserve_battery_energy` | same ID |
| `sensor.envoy_<serial>_reserve_battery_level` | `sensor.envoy_<serial>_battery_shutdown_level` |
| `sensor.encharge_<battery>_battery` | `sensor.iq_battery_<battery>` |
| `sensor.encharge_<battery>_temperature` | `sensor.iq_battery_<battery>_temperature` |
| `sensor.enpower_<sc>_temperature` | `sensor.iq_system_controller_<sc>_temperature` |
| `binary_sensor.enpower_<sc>_grid_status` | `binary_sensor.envoy_<serial>_grid_status` |
| `switch.enpower_<sc>_grid_enabled` | `switch.iq_system_controller_<sc>_grid_enabled` (only with the grid relay option on; see the README) |

If Home Assistant raises a repair about a changed unit for a sensor's statistics, choose to
update the unit. The old statistics are kept.

Then check the **Energy dashboard** (**Settings → Dashboards → Energy**): its sources should
point to the lifetime sensors above.

## 4. Repoint grid-outage automations

The core integration has no outage sensor, so outage automations usually compare two entities:
the grid is enabled (the relay has been told to stay closed) but the grid status is off (the
relay is open). Enphase Realtime has that as one binary sensor,
`binary_sensor.envoy_<serial>_grid_outage`, updated on the live poll (every second by default).

Replace a trigger like this:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.enpower_<sc>_grid_status
    to: "off"
conditions:
  - condition: state
    entity_id: switch.enpower_<sc>_grid_enabled
    state: "on"
```

with:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.envoy_<serial>_grid_outage
    to: "on"
```

and the "grid is back" side with `to: "off"`. A grid outage that you started yourself, by
turning **Grid enabled** off, doesn't count as an outage.

## What doesn't carry over

- **Energy today and last 7 days.** This integration doesn't create these. Use the Energy
  dashboard, or a `utility_meter` on the lifetime sensors.
- **Storage mode** is read-only here; change it in the Enphase app.
- **Grid enabled** is only created when you turn on the grid relay option. See the README's
  [Grid relay](../README.md#grid-relay) section before you do.
