# Test fixtures

## `reference/`

Real payloads from the reference site. It is a North American split-phase system on Envoy
firmware D8.3.6086, with one IQ System Controller, one IQ Battery 5P, 25 IQ8 microinverters,
and production, net-consumption and storage CTs. Country `US`, time zone `US/Eastern`.

Don't edit these by hand. `tools/sanitize_fixtures.py` regenerates them from the raw captures,
which are kept outside the repo, and swaps out identifiers along the way:

| Original | Placeholder |
|---|---|
| Device serials (Envoy, System Controller, battery, microinverters) | `900000000001` onwards, 12 digits, same value in every file |
| Envoy `euaid` | `000000` |
| Enlighten site ID | `1234567` |
| Enlighten user ID | `7654321` |
| Account email, masked email, zip code, site title | `owner@example.com`, `o***r@example.com`, `00000`, `Reference Site` |

Meter eids (`704643328` and so on) are channel IDs, the same on every Envoy, so they are left
as they are.

The script exits with an error if any original identifier is still in its output. After adding a
capture, grep the output for anything else personal, such as email addresses, IPs or names,
before committing.

### Envoy (local)

| File | Endpoint | Notes |
|---|---|---|
| `info.xml` | `GET /info` | XML, no auth |
| `stream_meter.txt` | `GET /stream/meter` | SSE frames, power in W |
| `ivp_livedata_status.json` | `GET /ivp/livedata/status` | power in mW/mVA |
| `ivp_meters.json` | `GET /ivp/meters` | eid → measurement type, `phaseMode`/`phaseCount` |
| `ivp_meters_readings.json` | `GET /ivp/meters/readings` | includes the unmapped eid `1023410688`, which parsers must skip |
| `ivp_meters_reports.json` | `GET /ivp/meters/reports` | `total-consumption` source |
| `ivp_ensemble_inventory.json` | `GET /ivp/ensemble/inventory` | battery temperature in °C, System Controller in °F |
| `ivp_ensemble_secctrl.json` | `GET /ivp/ensemble/secctrl` | |
| `ivp_ensemble_relay.json` | `GET /ivp/ensemble/relay` | |
| `ivp_ensemble_dry_contacts.json` | `GET /ivp/ensemble/dry_contacts` | |
| `ivp_ss_dry_contact_settings.json` | `GET /ivp/ss/dry_contact_settings` | |
| `ivp_ss_pel_settings.json` | `GET /ivp/ss/pel_settings` | copied from a probe's output: it holds no identifiers, and there's no raw capture for the sanitizer |
| `ivp_ss_pcs_settings.json` | `GET /ivp/ss/pcs_settings` | as above. `/ivp/ss/pcs_config` answers 404 |
| `ivp_sc_sched.json` | `GET /ivp/sc/sched` | |
| `ivp_sc_status.json` | `GET /ivp/sc/status` | normal operation |
| `ivp_sc_status_cg.json` | `GET /ivp/sc/status` | captured in mode 2, charging from grid |
| `api_v1_production_inverters.json` | `GET /api/v1/production/inverters` | |

### Enlighten (cloud)

| File | Endpoint | Notes |
|---|---|---|
| `cloud_site_settings.json` | `GET /service/batteryConfig/api/v1/siteSettings/{site}` | region, time zone, hardware flags. `siteStatus.text` came back in German, whatever the account locale, so don't parse it |
| `cloud_battery_settings.json` | `GET /service/batteryConfig/api/v1/batterySettings/{site}` | `requestedConfig` is `{}`; no pending-change capture yet |
| `cloud_put_response.json` | `PUT /service/batteryConfig/api/v1/batterySettings/{site}` | the same `{message}` body for every PUT captured |
| `cloud_accept_disclaimer.json` | `POST /service/batteryConfig/api/v1/batterySettings/acceptDisclaimer/{site}` | request body was `{"disclaimer-type":"itc"}` |
| `cloud_search_sites.json` | `GET /app-api/search_sites.json` | lists the same site twice; dedupe on `id` |
| `cloud_search_sites_empty.json` | `GET /app-api/search_sites.json` | the empty response seen in 2 of 4 calls |
| `cloud_grid_control_check.json` | `GET /app-api/{site}/grid_control_check.json` | pre-check before a grid relay write |

## Other layouts

Single-phase and three-phase captures go in `<layout>/` directories next to `reference/` (for
example `three_phase/`), with the same file names. Until real dumps exist, tests for those
layouts build synthetic payloads in the test module and label them as synthetic (spec §8, S9).
