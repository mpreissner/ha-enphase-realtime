"""Base for the cloud-backed controls (spec 6.1, 6.2).

A control writes through the CloudCoordinator's client and reads its state from the
FastCoordinator. The cloud supplies only limits and metadata: its own fields lag the Envoy by
minutes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .confirm import CONFIRM_TIMEOUT, Confirmation, LocalConfirm
from .const import DOMAIN
from .coordinator import CloudCoordinator, FastCoordinator, FastData
from .enlighten_client.battery import BatteryConfigClient
from .enlighten_client.errors import EnlightenAuthError, EnlightenError
from .entity import EnphaseEntity

_LOGGER = logging.getLogger(__name__)


def schedule(d: FastData):
    if d.schedule is None:
        raise KeyError("schedule")
    return d.schedule


def secctrl(d: FastData):
    if d.secctrl is None:
        raise KeyError("secctrl")
    return d.secctrl


class CloudControl[T](EnphaseEntity[FastData]):
    """Shows the requested value while the write is pending, then whatever the Envoy reports."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        fast: FastCoordinator,
        cloud: CloudCoordinator,
        description: Any,
        device: DeviceInfo,
        unique_prefix: str,
    ) -> None:
        super().__init__(fast, description, device, unique_prefix)
        self._cloud = cloud
        self._confirm: LocalConfirm[T] = LocalConfirm()
        self._cancel_timeout: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._cloud.async_add_listener(self.async_write_ha_state))
        self.async_on_remove(self._cancel_timer)

    @property
    def available(self) -> bool:
        """A write needs the cloud, so a failing CloudCoordinator takes the control down."""
        return (
            super().available and self._cloud.last_update_success and self._cloud.data is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        status = self._confirm.status
        return None if status is None else {"confirmation": status.value}

    def _local(self) -> T | None:
        return self._value()[1]

    def _shown(self) -> T | None:
        return self._confirm.shown(self._local())

    @callback
    def _handle_coordinator_update(self) -> None:
        self._check(dt_util.utcnow())
        super()._handle_coordinator_update()

    @callback
    def _check(self, now: datetime) -> None:
        requested, local = self._confirm.requested, self._local()
        status = self._confirm.check(local, now)
        if status is None:
            return
        self._cancel_timer()
        if status is Confirmation.CONFIRMED:
            _LOGGER.debug("%s: the Envoy confirms %s", self.entity_id, requested)
        if status is Confirmation.FAILED:
            # The service call returned long ago, so this is a warning, not an exception.
            _LOGGER.warning(
                "%s: asked Enphase for %s, but after %d s the Envoy still reports %s",
                self.entity_id,
                requested,
                CONFIRM_TIMEOUT.total_seconds(),
                local,
            )

    @callback
    def _timed_out(self, _now: datetime) -> None:
        """Fails the confirmation even if the Envoy has stopped answering and the fast
        coordinator has gone quiet."""
        self._cancel_timeout = None
        started = self._confirm.started
        if started is not None:
            self._check(max(dt_util.utcnow(), started + CONFIRM_TIMEOUT))
        self.async_write_ha_state()

    @callback
    def _cancel_timer(self) -> None:
        if self._cancel_timeout is not None:
            self._cancel_timeout()
            self._cancel_timeout = None

    async def _async_write(
        self, requested: T, write: Callable[[BatteryConfigClient], Awaitable[Any]]
    ) -> None:
        """Send the write; only once Enphase accepts it does the state change."""
        try:
            await write(self._cloud.battery)
        except EnlightenAuthError as err:
            self._cloud.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="write_auth_failed"
            ) from err
        except EnlightenError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        _LOGGER.debug("%s: Enphase accepted %s; waiting for the Envoy", self.entity_id, requested)
        self._cancel_timer()
        self._confirm.start(requested, dt_util.utcnow())
        self._cancel_timeout = async_call_later(self.hass, CONFIRM_TIMEOUT, self._timed_out)
        self.async_write_ha_state()
        # Picks up requestedConfig, which the "Pending cloud change" sensor shows.
        await self._cloud.async_request_refresh()
