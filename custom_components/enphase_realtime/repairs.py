"""Fix flows for the integration's repair issues."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from . import statistics_repair
from .statistics_repair import CHARGED, DISCHARGED, Repair


class BatteryStatisticsRepairFlow(RepairsFlow):
    """Removes the phantom resets after the user has seen what will go (spec 2)."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        repairs: dict[str, Repair] = {}
        if entry is not None:
            repairs = await statistics_repair.async_find_repairs(self.hass, entry)
        if user_input is not None:
            await statistics_repair.async_apply(self.hass, repairs.values())
            return self.async_create_entry(data={})

        def energy(key: str) -> str:
            return f"{repairs[key].phantom_energy:,.1f}" if key in repairs else "0.0"

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"charged": energy(CHARGED), "discharged": energy(DISCHARGED)},
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    return BatteryStatisticsRepairFlow(str((data or {}).get("entry_id")))
