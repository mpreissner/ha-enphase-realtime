"""Dry-contact mode and action selects (docs/specs/dry-contacts.md 4)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EnphaseConfigEntry
from .dry_contact import DryContactControl, contact_device, dry_contact_controls

PARALLEL_UPDATES = 1

# The core integration's option names, to the Envoy's values.
MODES = {"standard": "manual", "battery": "soc"}
ACTIONS = {"powered": "apply", "not_powered": "shed", "schedule": "schedule", "none": "none"}


@dataclass(frozen=True, kw_only=True)
class DryContactSelectDescription(SelectEntityDescription):
    value_fn: Callable[[Any], str | None]
    field: str
    to_envoy: dict[str, str]


# (Envoy field, name suffix, options, translation key)
_SETTINGS = (
    ("mode", "Mode", MODES, "dry_contact_mode"),
    ("grid_action", "Grid action", ACTIONS, "dry_contact_action"),
    ("micro_grid_action", "Microgrid action", ACTIONS, "dry_contact_action"),
    ("gen_action", "Generator action", ACTIONS, "dry_contact_action"),
)


def _descriptions(contact_id: str) -> list[DryContactSelectDescription]:
    out = []
    for field, name, to_envoy, translation_key in _SETTINGS:
        from_envoy = {v: k for k, v in to_envoy.items()}
        out.append(
            DryContactSelectDescription(
                key=f"dry_contact_{contact_id}_{field}",
                name=name,
                translation_key=translation_key,
                options=list(to_envoy),
                field=field,
                to_envoy=to_envoy,
                # An Envoy value outside the map shows as unknown.
                value_fn=lambda d, f=field, m=from_envoy: m.get(
                    getattr(d.dry_contact_settings[contact_id], f)
                ),
            )
        )
    return out


class DryContactSelect(DryContactControl[str], SelectEntity):
    entity_description: DryContactSelectDescription  # type: ignore[assignment]

    @property
    def current_option(self) -> str | None:
        return self._shown()

    async def async_select_option(self, option: str) -> None:
        d = self.entity_description

        async def write() -> None:
            await self.coordinator.write_dry_contact_settings(
                self._contact_id, {d.field: d.to_envoy[option]}
            )

        await self._async_write(option, write)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    contacts = dry_contact_controls(entry)
    if not contacts:
        return
    rt = entry.runtime_data
    async_add_entities(
        DryContactSelect(rt.slow, d, contact_device(hass, entry, contact_id), rt.serial, contact_id)
        for contact_id in contacts
        for d in _descriptions(contact_id)
    )
