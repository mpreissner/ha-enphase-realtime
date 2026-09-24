"""Copy reference-site captures into tests/fixtures/reference/ with identifiers replaced.

The captures live outside this repo (they carry live session cookies and real serials):

    python tools/sanitize_fixtures.py [--data ../enphase-envoy-mqtt-json/data]

Serials, the Envoy euaid, the site ID, the user ID and free-text account fields are swapped
for stable placeholders. The script refuses to finish if any original identifier survives in
the output, so a new capture with an unexpected field fails loudly instead of leaking.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "tests" / "fixtures" / "reference"

# Envoy samples copied as-is apart from identifier replacement: (source, fixture name).
SAMPLES = [
    ("info.xml", "info.xml"),
    ("stream_meter.txt", "stream_meter.txt"),
    ("ivp_livedata_status.json", "ivp_livedata_status.json"),
    ("ivp_ensemble_secctrl.json", "ivp_ensemble_secctrl.json"),
    ("ivp_sc_sched.json", "ivp_sc_sched.json"),
    ("ivp_sc_status.json", "ivp_sc_status.json"),
    # The same endpoint while the controller was in mode 2 (charging from grid).
    ("ivp_sc_status_mode2.json", "ivp_sc_status_cg.json"),
    ("ivp_ensemble_relay.json", "ivp_ensemble_relay.json"),
    ("ivp_meters.json", "ivp_meters.json"),
    ("ivp_meters_readings.json", "ivp_meters_readings.json"),
    ("ivp_meters_reports.json", "ivp_meters_reports.json"),
    ("ivp_ensemble_inventory.json", "ivp_ensemble_inventory.json"),
    ("ivp_ss_dry_contact_settings.json", "ivp_ss_dry_contact_settings.json"),
    ("ivp_ensemble_dry_contacts.json", "ivp_ensemble_dry_contacts.json"),
    ("api_v1_production_inverters.json", "api_v1_production_inverters.json"),
    ("cloud_batterySettings.json", "cloud_battery_settings.json"),
]

# Cloud responses pulled out of the app HAR: (method, path substring, selector, fixture name).
# The selector picks which matching entry to keep when there are several.
HAR_RESPONSES = [
    ("GET", "/batteryConfig/api/v1/siteSettings/", "first", "cloud_site_settings.json"),
    ("POST", "/batterySettings/acceptDisclaimer/", "first", "cloud_accept_disclaimer.json"),
    ("PUT", "/batteryConfig/api/v1/batterySettings/", "first", "cloud_put_response.json"),
    ("GET", "/app-api/search_sites.json", "nonempty", "cloud_search_sites.json"),
    ("GET", "/app-api/search_sites.json", "empty", "cloud_search_sites_empty.json"),
    ("GET", "/grid_control_check.json", "first", "cloud_grid_control_check.json"),
]

PLACEHOLDER_SITE_ID = "1234567"
PLACEHOLDER_USER_ID = "7654321"
PLACEHOLDER_EUAID = "000000"
# Free-text account fields, replaced wherever they appear in a JSON body.
SCRUB_FIELDS = {
    "email": "owner@example.com",
    "ownerOrHostMaskedEmail": "o***r@example.com",
    "title": "Reference Site",
    "zipcode": "00000",
}

SERIAL = re.compile(r"(?<![\d.])\d{12}(?![\d.])")


def serial_placeholder(index: int) -> str:
    """Twelve digits, like a real serial, but obviously fake."""
    return f"9000000{index:05d}"


def scrub_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            k: SCRUB_FIELDS[k] if k in SCRUB_FIELDS and v else scrub_fields(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub_fields(v) for v in value]
    return value


def har_bodies(har_path: Path) -> tuple[dict[str, str], set[str], set[str]]:
    """Return the selected response bodies and the site and user IDs seen in the HAR."""
    entries = json.loads(har_path.read_text())["log"]["entries"]
    site_ids: set[str] = set()
    user_ids: set[str] = set()
    for entry in entries:
        url = urlsplit(entry["request"]["url"])
        site_ids.update(re.findall(r"/(?:batterySettings|siteSettings|app-api)/(\d+)", url.path))
        user_ids.update(parse_qs(url.query).get("userId", []))

    bodies: dict[str, str] = {}
    for method, needle, selector, name in HAR_RESPONSES:
        for entry in entries:
            request = entry["request"]
            if request["method"] != method or needle not in request["url"]:
                continue
            text = entry["response"]["content"].get("text") or ""
            if selector != "first":
                empty = not json.loads(text).get("sites")
                if empty != (selector == "empty"):
                    continue
            bodies[name] = json.dumps(scrub_fields(json.loads(text)), indent=2) + "\n"
            break
        else:
            sys.exit(f"no {method} {needle} ({selector}) in {har_path}")
    return bodies, site_ids, user_ids


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data",
        type=Path,
        default=REPO.parent / "enphase-envoy-mqtt-json" / "data",
        help="capture directory holding samples/ and enphase_app_capture.har",
    )
    args = parser.parse_args()

    files = {name: (args.data / "samples" / src).read_text() for src, name in SAMPLES}
    bodies, site_ids, user_ids = har_bodies(args.data / "enphase_app_capture.har")
    files.update(bodies)

    replacements: dict[str, str] = {}
    for text in files.values():
        for serial in SERIAL.findall(text):
            replacements.setdefault(serial, serial_placeholder(len(replacements) + 1))
    euaid = re.search(r"<euaid>(\w+)</euaid>", files["info.xml"])
    if euaid:
        replacements[euaid.group(1)] = PLACEHOLDER_EUAID
    replacements.update(dict.fromkeys(site_ids, PLACEHOLDER_SITE_ID))
    replacements.update(dict.fromkeys(user_ids, PLACEHOLDER_USER_ID))

    pattern = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, replacements)) + r")(?![\w.])")
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        clean = pattern.sub(lambda m: replacements[m.group(1)], text)
        leaked = [orig for orig in replacements if orig in clean]
        if leaked:
            sys.exit(f"{name}: identifiers survived sanitizing: {leaked}")
        (OUT / name).write_text(clean)

    print(f"wrote {len(files)} fixtures to {OUT.relative_to(REPO)}")
    print(f"replaced {len(replacements)} identifiers")


if __name__ == "__main__":
    main()
