"""Diagnostics keep the payloads and drop everything identifying (spec 3.3)."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_realtime.diagnostics import async_get_config_entry_diagnostics

from .conftest import EMAIL, PASSWORD, SERIAL, SITE, FakeEnphase


async def test_diagnostics_redact(
    hass: HomeAssistant, fake: FakeEnphase, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await async_get_config_entry_diagnostics(hass, config_entry)
    dump = json.dumps(result)

    rt = config_entry.runtime_data
    serials = {SERIAL} | {b.serial for b in rt.slow.data.inventory.batteries}
    serials |= {i.serial for i in rt.slow.data.inverters}
    for secret in [EMAIL, PASSWORD, fake.token, str(SITE), *serials]:
        assert secret not in dump, secret

    assert result["phase_layout"] == "split"
    assert result["coordinators"]["fast"]["last_update_success"] is True
    assert "/ivp/livedata/status" in result["payloads"]
    assert "/info" not in result["payloads"] or "<sn>**REDACTED**</sn>" in dump
