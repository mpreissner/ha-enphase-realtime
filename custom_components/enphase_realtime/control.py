"""Bases for the controls: write, then confirm locally (spec 6.1-6.3).

A cloud-backed control writes through the CloudCoordinator's client and reads its state from the
FastCoordinator. The cloud supplies only limits and metadata: its own fields lag the Envoy by
minutes. The grid relay writes to the Envoy and reads from the LiveCoordinator.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .confirm import CONFIRM_TIMEOUT, Confirmation, LocalConfirm
from .const import CONF_COUNTRY, DOMAIN
from .coordinator import CloudCoordinator, FastCoordinator, FastData
from .enlighten_client.battery import BatteryConfigClient
from .enlighten_client.errors import EnlightenAuthError, EnlightenError
from .enlighten_client.models import BatterySettings, charge_from_grid_available
from .entity import EnphaseEntity

if TYPE_CHECKING:
    from . import EnphaseConfigEntry

_LOGGER = logging.getLogger(__name__)


def charge_from_grid_itc(entry: EnphaseConfigEntry) -> bool | None:
    """None where there is no charge-from-grid control; otherwise whether "on" needs the ITC
    disclaimer (the US Investment Tax Credit). Battery maintenance exists where the switch
    does."""
    rt = entry.runtime_data
    if rt.cloud is None or rt.fast is None or rt.cloud.site is None:
        return None
    if not charge_from_grid_available(rt.cloud.site, rt.cloud.data):
        return None
    # The user's confirmed country wins over the registered one (spec 3.3).
    return (entry.options.get(CONF_COUNTRY) or rt.cloud.site.country_code) == "US"


async def write_charge_from_grid(
    battery: BatteryConfigClient,
    on: bool,
    settings: BatterySettings | None,
    *,
    itc_disclaimer: bool,
) -> None:
    """The Enphase app's write, shared by the switch and battery maintenance. "On" keeps the
    begin and end times from the last GET, as the app sends them with every "on"."""
    if not on:
        await battery.update_battery_settings({"chargeFromGrid": False})
        return
    body: dict[str, Any] = {"chargeFromGrid": True, "chargeFromGridScheduleEnabled": False}
    if settings is not None:
        for key, value in (
            ("chargeBeginTime", settings.charge_begin_time),
            ("chargeEndTime", settings.charge_end_time),
        ):
            if value is not None:
                body[key] = value
    if itc_disclaimer:
        body["acceptedItcDisclaimer"] = True
        await battery.accept_disclaimer("itc")
    await battery.update_battery_settings(body)


class ConfirmingControl[DataT, T](EnphaseEntity[DataT]):
    """Shows the requested value while the write is pending, then whatever the Envoy reports."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[DataT],
        description: Any,
        device: DeviceInfo,
        unique_prefix: str,
        timeout: timedelta = CONFIRM_TIMEOUT,
    ) -> None:
        super().__init__(coordinator, description, device, unique_prefix)
        self._confirm: LocalConfirm[T] = LocalConfirm(timeout=timeout)
        self._cancel_timeout: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_timer)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        status = self._confirm.status
        return None if status is None else {"confirmation": status.value}

    def _local(self) -> T | None:
        """The value a write is confirmed against."""
        return self._value()[1]

    def _shown(self) -> T | None:
        return self._confirm.shown(self._value()[1])

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
                "%s: asked for %s, but after %d s the Envoy still reports %s",
                self.entity_id,
                requested,
                self._confirm.timeout.total_seconds(),
                local,
            )

    @callback
    def _timed_out(self, _now: datetime) -> None:
        """Fails the confirmation even if the Envoy has stopped answering and the coordinator
        has gone quiet."""
        self._cancel_timeout = None
        started = self._confirm.started
        if started is not None:
            self._check(max(dt_util.utcnow(), started + self._confirm.timeout))
        self.async_write_ha_state()

    @callback
    def _cancel_timer(self) -> None:
        if self._cancel_timeout is not None:
            self._cancel_timeout()
            self._cancel_timeout = None

    @callback
    def _start_confirm(self, requested: T) -> None:
        """Call once the write has been accepted."""
        self._cancel_timer()
        self._confirm.start(requested, dt_util.utcnow())
        self._cancel_timeout = async_call_later(self.hass, self._confirm.timeout, self._timed_out)
        self.async_write_ha_state()


class CloudControl[T](ConfirmingControl[FastData, T]):
    """A battery setting written through Enphase's cloud and confirmed on the fast poll."""

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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._cloud.async_add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        """A write needs the cloud, so a failing CloudCoordinator takes the control down."""
        return (
            super().available and self._cloud.last_update_success and self._cloud.data is not None
        )

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
        self._start_confirm(requested)
        # Picks up requestedConfig, which the "Pending cloud change" sensor shows.
        await self._cloud.async_request_refresh()
