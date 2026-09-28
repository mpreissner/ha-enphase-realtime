# Dry-contact controls

Status: implemented. The switch and the level writes were tested live on 28 September 2026 (see 7).

## 1. Goal

Let the owner switch and configure the System Controller's dry contacts (NC1, NC2, NO1, NO2)
from Home Assistant, matching the core `enphase_envoy` integration's controls. Until now
the dry contacts were read-only (core-integration.md 5.6).

## 2. Endpoints

Both are local and were taken from pyenphase (`open_dry_contact`, `close_dry_contact`,
`update_dry_contact`). Neither has been captured from the Enphase app.

| What | Request | Body |
|---|---|---|
| Open or close one contact | `POST /ivp/ensemble/dry_contacts` | `{"dry_contacts": {"id": "NC1", "status": "open" \| "closed"}}` |
| Change one contact's settings | `POST /ivp/ss/dry_contact_settings` | `{"dry_contacts": {<the contact's full object>}}` |

pyenphase warns that a partial settings object "may crash the Envoy". So a settings write
sends the contact's object exactly as the last `GET /ivp/ss/dry_contact_settings` returned it,
with every field kept, strings like `"override": "false"` untouched, and only the changed fields
replaced. This differs from pyenphase, which rebuilds the object from its model. That drops
`safe_state_action`, `black_start_retries`, `action_retries` and `pv_phase_select`, and sends
`manual_override` as a bool. The reply isn't used.

## 3. Gate

The controls exist only when both of these hold:

- the site has a System Controller (`has_enpower`);
- the new option **Allow dry-contact control** (`allow_dry_contact_control`) is on. It is off
  by default, like `allow_grid_relay_control`, because the contacts switch real loads.

Without the option, the read-only binary sensor and diagnostic sensors stay as they are. The
controls are added alongside them, not in their place.

## 4. Entities

Each contact gets the following entities. They sit on the same device as the contact's
existing entities (the System Controller, or the Envoy on a site without one). Names start
with the contact's label: the Envoy's `load_name`, or the contact ID when that's empty.

| Platform | Key (unique ID `{envoy serial}_{key}`) | Name | Envoy field | Values |
|---|---|---|---|---|
| switch | `dry_contact_{id}` | `{label}` | `ensemble/dry_contacts` `status` | on = `closed` |
| select | `dry_contact_{id}_mode` | `{label} mode` | `mode` | `standard` = `manual`, `battery` = `soc` |
| select | `dry_contact_{id}_grid_action` | `{label} grid action` | `grid_action` | see below |
| select | `dry_contact_{id}_micro_grid_action` | `{label} microgrid action` | `micro_grid_action` | see below |
| select | `dry_contact_{id}_gen_action` | `{label} generator action` | `gen_action` | see below |
| number | `dry_contact_{id}_soc_low` | `{label} cutoff battery level` | `soc_low` | 0–100 % |
| number | `dry_contact_{id}_soc_high` | `{label} restore battery level` | `soc_high` | 0–100 % |

- **Actions:** `powered` = `apply`, `not_powered` = `shed`, `schedule` = `schedule`,
  `none` = `none`. These are the core integration's option names.
- **Unrecognised values:** an Envoy value outside these maps shows as unknown.
- **Unique IDs:** the keys are the same as the read-only entities', on other platforms. Home
  Assistant keys unique IDs per platform, so the two don't clash.
- **Device:** the core integration makes a device per contact. Ours stay on the System
  Controller with the existing contact entities, so the entity IDs that 0.2.0 created don't
  change.
- **Levels:** the cutoff level must stay below the restore level. A write that breaks this is
  refused with a validation error, and nothing is sent.

## 5. Write, then confirm

The same pattern as the grid relay (core-integration.md 6.3), confirmed against the
SlowCoordinator's data.

1. **Skip:** a write asking for what the Envoy already reports, with nothing pending, sends
   nothing.
2. **Send:** the POST goes out. If the Envoy refuses it, the service call raises
   `dry_contact_write_failed` and the entity's state doesn't change.
3. **Pending:** the entity shows the requested value, with `confirmation: pending`.
4. **Poll:** the SlowCoordinator polls every 60 s, too slow for this. So while a confirmation
   is pending, the entity re-reads just the two dry-contact endpoints every 3 s and pushes the
   result into the coordinator's data (`SlowCoordinator.refresh_dry_contacts`).
5. **Settle:** the confirmation succeeds when the Envoy reports the requested value, or fails
   after 30 s (`DRY_CONTACT_CONFIRM_TIMEOUT`, which is `RELAY_CONFIRM_TIMEOUT`) and logs a
   warning.

**Back-to-back settings writes.** Say the cutoff level changes and then the restore level
changes a second later. The second write would be built from a GET that doesn't show the
first change yet, and would undo it. So the coordinator remembers, per contact, the fields it
has written. It lays them over the Envoy's object for every later write, until:

- the Envoy reports them, or
- 30 s pass.

## 6. What this doesn't do

- **Other fields.** It doesn't write `load_name`, `type`, `override`, the essential times or
  the priority. These are installer fields, and the core integration doesn't write them either.
- **Mode conflicts.** It doesn't keep the switch from fighting the mode. In `battery` mode, the
  System Controller opens and closes the contact itself at the cutoff and restore levels, so a
  manual switch may be undone. The entity shows what the Envoy reports.

## 7. Testing

- **Unit:** the model keeps the raw object, and the client sends the bodies in 2.
- **HA** (`tests_ha/test_dry_contacts.py`):
  - no controls unless allowed, or without a System Controller;
  - the switch posts and confirms;
  - the full-object settings body;
  - back-to-back writes keep the earlier change;
  - failing after 30 s;
  - the level validation;
  - the Envoy refusing the write.
- **Live:** only with the owner present and approving. The contacts switch real loads on the
  reference system: NC1 the air conditioner, NC2 (believed) the dryer.
  - **28 September 2026, switch:** NC1 opened at 14:33:14 and was confirmed at 14:33:17, and
    the AC's ~50 W draw dropped to 0. It was closed at 14:33:28 and confirmed at 14:33:33.
  - **28 September 2026, levels:** NC1's cutoff went 30 → 25 → 30 and its restore level
    40 → 38 → 40, by clicking the number arrows. That sent 15+ settings POSTs, some 0.3 s
    apart. The Envoy took every one, and the final values read back were confirmed within
    4 s. The rapid clicks showed that back-to-back writes don't undo each other.
  - **Not yet tested:** the mode and action selects. They use the same POST.
