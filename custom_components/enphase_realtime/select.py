"""Dry-contact mode and action selects (docs/specs/dry-contacts.md 4), and the storage mode."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from . import EnphaseConfigEntry
from .const import FULL_BACKUP
from .control import CloudControl
from .dry_contact import DryContactControl, contact_device, dry_contact_controls
from .enlighten_client.battery import BatteryConfigClient
from .entity import controller_or_envoy, envoy_device

_LOGGER = logging.getLogger(__name__)

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


# The core integration's storage modes, to the cloud's battery profiles. Other profiles (the
# app's "expert" and AI optimisation) show as unknown.
STORAGE_MODES = {
    "backup": FULL_BACKUP,
    "self_consumption": "self-consumption",
    "savings": "cost_savings",
}
_PROFILE_MODES = {v: k for k, v in STORAGE_MODES.items()}

STORAGE_MODE = SelectEntityDescription(
    key="storage_mode",
    name="Storage mode",
    translation_key="storage_mode",
    options=list(STORAGE_MODES),
)

# The gateways pick up a profile change within a minute or two; the cloud is re-read this often
# until they have.
STORAGE_MODE_REPOLL = 15
STORAGE_MODE_CONFIRM_TIMEOUT = timedelta(minutes=5)


class StorageModeSelect(CloudControl[str], SelectEntity):
    """The battery profile, written through the cloud's `batterySettings` PUT. No Envoy field
    reports the profile, so a write is confirmed once the cloud reports it with no gateway
    still to apply it."""

    _cancel_repoll: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._cloud.async_add_listener(self._handle_cloud_update))
        self.async_on_remove(self._stop_repoll)

    def _value(self) -> tuple[bool, Any]:
        settings = self._cloud.data
        if settings is None:
            return False, None
        return True, _PROFILE_MODES.get(settings.profile)

    def _local(self) -> str | None:
        settings = self._cloud.data
        if settings is None or settings.has_pending_change:
            return None
        return _PROFILE_MODES.get(settings.profile)

    @property
    def current_option(self) -> str | None:
        return self._shown()

    async def async_select_option(self, option: str) -> None:
        profile = STORAGE_MODES[option]
        _LOGGER.info("%s: asking Enphase for profile %s", self.entity_id, profile)

        async def write(battery: BatteryConfigClient) -> None:
            response = await battery.update_battery_settings({"profile": profile})
            _LOGGER.debug("%s: profile PUT answered %s", self.entity_id, response)

        await self._async_write(option, write)
        self._repoll_later()

    @callback
    def _handle_cloud_update(self) -> None:
        if (settings := self._cloud.data) is not None:
            # A profile change can bring its own reserve and charge-from-grid settings with it.
            _LOGGER.debug(
                "%s: cloud has profile %s, reserve %s%%, charge from grid %s, pending %s",
                self.entity_id,
                settings.profile,
                settings.backup_percentage,
                settings.charge_from_grid,
                settings.pending_gateways,
            )
        self._check(dt_util.utcnow())
        if self._confirm.pending:
            self._repoll_later()
        self.async_write_ha_state()

    @callback
    def _repoll_later(self) -> None:
        self._stop_repoll()
        self._cancel_repoll = async_call_later(self.hass, STORAGE_MODE_REPOLL, self._repoll)

    async def _repoll(self, _now: datetime) -> None:
        self._cancel_repoll = None
        if self._confirm.pending:
            await self._cloud.async_request_refresh()

    @callback
    def _stop_repoll(self) -> None:
        if self._cancel_repoll is not None:
            self._cancel_repoll()
            self._cancel_repoll = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    rt = entry.runtime_data
    if contacts := dry_contact_controls(entry):
        async_add_entities(
            DryContactSelect(
                rt.slow, d, contact_device(hass, entry, contact_id), rt.serial, contact_id
            )
            for contact_id in contacts
            for d in _descriptions(contact_id)
        )
    if rt.cloud is None or rt.fast is None:
        return
    inventory = rt.slow.data.inventory
    controllers = inventory.system_controllers if inventory is not None else []
    # Where the core integration's storage mode select is, so the entity ID matches.
    device = controller_or_envoy(
        [c.serial for c in controllers], rt.envoy_device_id, envoy_device(rt.serial, rt.firmware)
    )
    async_add_entities(
        [
            StorageModeSelect(
                rt.fast, rt.cloud, STORAGE_MODE, device, rt.serial, STORAGE_MODE_CONFIRM_TIMEOUT
            )
        ]
    )
