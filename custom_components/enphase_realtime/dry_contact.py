"""Dry-contact controls: the switch, selects and numbers share this base, which writes to the
Envoy and confirms on the SlowCoordinator's data (docs/specs/dry-contacts.md)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later

from .confirm import DRY_CONTACT_CONFIRM_TIMEOUT
from .const import CONF_ALLOW_DRY_CONTACTS, DEFAULT_ALLOW_DRY_CONTACTS, DOMAIN
from .control import ConfirmingControl
from .coordinator import SlowCoordinator, SlowData
from .entity import IQ_SYSTEM_CONTROLLER, MANUFACTURER, child_device
from .envoy_client.errors import EnvoyError

if TYPE_CHECKING:
    from . import EnphaseConfigEntry

_LOGGER = logging.getLogger(__name__)

DRY_CONTACT_RELAY = "Dry contact relay"

# How often a control re-reads the contacts while a write is unconfirmed (spec 5).
CONFIRM_POLL = 3


def contact_device(hass: HomeAssistant, entry: EnphaseConfigEntry, contact_id: str) -> DeviceInfo:
    """The contact's own device, as in the core integration: named after its load and hanging
    off the System Controller, or the Envoy without one."""
    rt = entry.runtime_data
    inventory = rt.slow.data.inventory
    controllers = inventory.system_controllers if inventory is not None else []
    parent_id = rt.envoy_device_id
    if controllers:
        # Registered here so the contact can point at it by ID, whichever platform runs first.
        parent_id = (
            dr.async_get(hass)
            .async_get_or_create(
                config_entry_id=entry.entry_id,
                **child_device(IQ_SYSTEM_CONTROLLER, controllers[-1].serial, rt.envoy_device_id),
            )
            .id
        )
    return DeviceInfo(
        identifiers={(DOMAIN, f"{rt.serial}_{contact_id}")},
        manufacturer=MANUFACTURER,
        model=DRY_CONTACT_RELAY,
        name=contact_label(rt.slow.data, contact_id),
        via_device_id=parent_id,
    )


def contact_label(data: SlowData, contact_id: str) -> str:
    """The Envoy's `load_name`, or the contact ID when it has none."""
    settings = data.dry_contact_settings.get(contact_id)
    return settings.load_name if settings is not None and settings.load_name else contact_id


def dry_contact_controls(entry: EnphaseConfigEntry) -> list[str]:
    """The contact IDs to make controls for: none unless the owner allowed it (spec 3)."""
    rt = entry.runtime_data
    if not rt.hardware.has_enpower or not entry.options.get(
        CONF_ALLOW_DRY_CONTACTS, DEFAULT_ALLOW_DRY_CONTACTS
    ):
        return []
    data = rt.slow.data
    # A settings write needs the contact's full object, so both endpoints must list it.
    return [c for c in data.dry_contact_states if c in data.dry_contact_settings]


class DryContactControl[T](ConfirmingControl[SlowData, T]):
    """Writes to the Envoy, then re-reads the contacts every few seconds until the write is
    confirmed or times out; the slow poll alone would take a minute."""

    coordinator: SlowCoordinator

    def __init__(
        self,
        slow: SlowCoordinator,
        description: Any,
        device: DeviceInfo,
        serial: str,
        contact_id: str,
    ) -> None:
        super().__init__(slow, description, device, serial, DRY_CONTACT_CONFIRM_TIMEOUT)
        self._contact_id = contact_id
        self._cancel_poll: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._stop_polling)

    async def _async_write(self, requested: T, write: Callable[[], Awaitable[None]]) -> None:
        if not self._confirm.pending and self._value()[1] == requested:
            return
        _LOGGER.info("%s: asking the Envoy for %s", self.entity_id, requested)
        try:
            await write()
        except EnvoyError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="dry_contact_write_failed",
                translation_placeholders={"contact": self._contact_id, "error": str(err)},
            ) from err
        self._start_confirm(requested)
        self._poll_later()

    @callback
    def _poll_later(self) -> None:
        self._stop_polling()
        if self._confirm.pending:
            self._cancel_poll = async_call_later(self.hass, CONFIRM_POLL, self._poll)

    async def _poll(self, _now: datetime) -> None:
        self._cancel_poll = None
        try:
            await self.coordinator.refresh_dry_contacts()
        except EnvoyError as err:
            _LOGGER.debug("%s: couldn't re-read the dry contacts: %s", self.entity_id, err)
        self._poll_later()

    @callback
    def _stop_polling(self) -> None:
        if self._cancel_poll is not None:
            self._cancel_poll()
            self._cancel_poll = None
