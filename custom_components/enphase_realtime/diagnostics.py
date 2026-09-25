"""Diagnostics: the raw payloads behind every entity, with anything identifying removed.

These dumps are how other hardware layouts get verified (spec 3.3), so they keep the full
structure and only mask values.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import EnphaseConfigEntry
from .const import CONF_SERIAL, CONF_SITE_ID, CONF_TOKEN

REDACTED = "**REDACTED**"

TO_REDACT = {
    CONF_EMAIL,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SERIAL,
    CONF_SITE_ID,
    CONF_TOKEN,
    "ownerOrHostMaskedEmail",
    "serial",
    "serial_num",
    "serialNumber",
    "sn",
    "euaid",
    "site_id",
    "siteId",
    "system_id",
    "user_id",
    "userId",
    "zip",
    "title",
    "load_name",
}

_XML_SERIAL = re.compile(r"<sn>[^<]*</sn>")


def _plain(value: Any) -> Any:
    """Dataclasses into dicts, so `async_redact_data` can walk them."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _scrub(value: Any, serials: set[str]) -> Any:
    """Serials also turn up as values and dict keys (pending gateways, the `/info` XML)."""
    if isinstance(value, str):
        value = _XML_SERIAL.sub(f"<sn>{REDACTED}</sn>", value)
        for serial in serials:
            value = value.replace(serial, REDACTED)
        return value
    if isinstance(value, dict):
        return {_scrub(k, serials): _scrub(v, serials) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, serials) for v in value]
    return value


def _serials(rt: Any) -> set[str]:
    serials = {rt.serial}
    slow = rt.slow.data
    if slow is not None:
        serials.update(i.serial for i in slow.inverters)
        if slow.inventory is not None:
            serials.update(b.serial for b in slow.inventory.batteries)
            serials.update(c.serial for c in slow.inventory.system_controllers)
    return {s for s in serials if s}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EnphaseConfigEntry
) -> dict[str, Any]:
    rt = entry.runtime_data
    coordinators = {
        "live": rt.live,
        "fast": rt.fast,
        "slow": rt.slow,
        "cloud": rt.cloud,
        "stream": rt.stream,
    }
    result = {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "phase_layout": rt.phase_layout.value,
        "hardware": _plain(rt.hardware),
        "firmware": rt.firmware,
        "coordinators": {
            name: None
            if c is None
            else {
                "last_update_success": c.last_update_success,
                "data": async_redact_data(_plain(c.data), TO_REDACT),
            }
            for name, c in coordinators.items()
        },
        "payloads": async_redact_data(_plain(rt.client.last_payloads), TO_REDACT),
    }
    return _scrub(result, _serials(rt))
