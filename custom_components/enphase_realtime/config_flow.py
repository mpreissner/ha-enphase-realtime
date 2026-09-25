"""Config, reauth and options flows (spec 4.1, 4.3)."""

from __future__ import annotations

import logging
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    CountrySelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CLOUD_INTERVAL,
    CONF_COUNTRY,
    CONF_ENABLE_STREAM,
    CONF_FAST_INTERVAL,
    CONF_FIRMWARE,
    CONF_HAS_BATTERY,
    CONF_HAS_ENPOWER,
    CONF_LIVE_INTERVAL,
    CONF_PHASE_LAYOUT,
    CONF_SERIAL,
    CONF_SITE_ID,
    CONF_STREAM_INTERVAL,
    CONF_TIME_ZONE,
    CONF_TOKEN,
    DEFAULT_CLOUD_INTERVAL,
    DEFAULT_ENABLE_STREAM,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_HOST,
    DEFAULT_LIVE_INTERVAL,
    DEFAULT_STREAM_INTERVAL,
    DOMAIN,
    MIN_FIRMWARE_MAJOR,
)
from .credentials import request_owner_token
from .enlighten_client.battery import BatteryConfigClient
from .enlighten_client.errors import EnlightenAuthError, EnlightenError
from .enlighten_client.models import SiteSettings
from .enlighten_client.session import EnlightenSession
from .envoy_client.errors import EnvoyAuthError, EnvoyError
from .envoy_client.local import FAST_TIMEOUT, EnvoyClient
from .envoy_client.models import Inventory, PhaseLayout, detect_phase_layout

_LOGGER = logging.getLogger(__name__)

# Shown in the confirm step's summary.
_LAYOUT_LABELS = {
    PhaseLayout.SINGLE: "Single-phase",
    PhaseLayout.SPLIT: "Split-phase",
    PhaseLayout.THREE: "Three-phase",
}


class FlowError(Exception):
    """Carries a `strings.json` error key back to the form."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass
class Probe:
    """Everything setup learns about the site before creating the entry."""

    serial: str
    firmware: str
    token: str
    site_ids: list[int]
    layout: PhaseLayout | None = None
    site_id: int | None = None
    site_settings: SiteSettings | None = None
    inventory: Inventory = field(default_factory=lambda: Inventory([], []))


class _Sessions:
    """The two throwaway sessions a validation run needs, closed afterwards."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.cloud = async_create_clientsession(hass, auto_cleanup=False)
        self.envoy = async_create_clientsession(hass, verify_ssl=False, auto_cleanup=False)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        # HA's sessions share its connector: detach, never close.
        self.cloud.detach()
        self.envoy.detach()


async def _login_and_token(
    sessions: _Sessions, host: str, email: str, password: str
) -> tuple[EnlightenSession, EnvoyClient, Probe]:
    """Steps 1 to 4 of spec 4.1: login, `/info`, owner token, site ID."""
    cloud = EnlightenSession(sessions.cloud, email, password)
    envoy = EnvoyClient(sessions.envoy, host)
    try:
        info = await envoy.info()
    except EnvoyError as err:
        _LOGGER.debug("Envoy /info failed: %s", err)
        raise FlowError("cannot_connect") from err
    if info.firmware_major is None or info.firmware_major < MIN_FIRMWARE_MAJOR:
        raise FlowError("firmware_unsupported")
    try:
        owner = await request_owner_token(cloud, sessions.cloud, info.serial)
    except (EnlightenAuthError, EnvoyAuthError) as err:
        _LOGGER.debug("Owner token request refused: %s", err)
        raise FlowError("invalid_auth") from err
    except (EnlightenError, EnvoyError) as err:
        _LOGGER.debug("Owner token request failed: %s", err)
        raise FlowError("cannot_connect") from err
    envoy.set_token(owner.token)

    site_ids = [cloud.system_id] if cloud.system_id is not None else []
    return cloud, envoy, Probe(info.serial, info.firmware, owner.token, site_ids)


async def _find_sites(cloud: EnlightenSession, probe: Probe) -> None:
    """Accounts with several sites: `search_sites` is flaky, so give it a few tries."""
    for _ in range(3):
        try:
            sites = await cloud.search_sites()
        except EnlightenError as err:
            _LOGGER.debug("search_sites failed: %s", err)
            continue
        ids = list(dict.fromkeys(s.id for s in sites))
        if ids:
            probe.site_ids = ids
            return


async def _inspect_site(
    cloud: EnlightenSession, envoy: EnvoyClient, probe: Probe, site_id: int
) -> None:
    """Steps 5 and 6: layout, hardware and a read from each side."""
    probe.site_id = site_id
    try:
        livedata = await envoy.livedata(FAST_TIMEOUT)
        meters = await envoy.meters()
    except EnvoyAuthError as err:
        raise FlowError("invalid_auth") from err
    except EnvoyError as err:
        _LOGGER.debug("Envoy read failed: %s", err)
        raise FlowError("cannot_connect") from err
    probe.layout = detect_phase_layout(meters, livedata)
    if probe.layout is None:
        raise FlowError("unknown_phase_layout")

    try:
        probe.inventory = await envoy.inventory()
    except EnvoyError as err:
        # Envoys without Ensemble hardware may not serve it at all.
        _LOGGER.debug("No ensemble inventory: %s", err)

    battery = BatteryConfigClient(cloud, site_id)
    try:
        probe.site_settings = await battery.site_settings()
    except EnlightenAuthError as err:
        raise FlowError("invalid_auth") from err
    except EnlightenError as err:
        # Region falls back to HA's settings (spec 3.3).
        _LOGGER.debug("siteSettings failed: %s", err)

    if _has_battery(probe):
        try:
            await battery.battery_settings()
        except EnlightenAuthError as err:
            raise FlowError("invalid_auth") from err
        except EnlightenError as err:
            _LOGGER.debug("batterySettings failed: %s", err)
            raise FlowError("cloud_unavailable") from err


def _has_battery(probe: Probe) -> bool:
    settings = probe.site_settings
    return bool(probe.inventory.batteries) or bool(settings and settings.has_encharge)


def _has_enpower(probe: Probe) -> bool:
    settings = probe.site_settings
    return bool(probe.inventory.system_controllers) or bool(settings and settings.has_enpower)


def _interval(minimum: int, maximum: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )


_TIME_ZONES: list[str] = []


async def _time_zone_selector(hass: HomeAssistant) -> SelectSelector:
    """Listing the zones reads the tz database from disk, so it runs once, off the loop."""
    if not _TIME_ZONES:
        _TIME_ZONES.extend(sorted(await hass.async_add_executor_job(zoneinfo.available_timezones)))
    return SelectSelector(SelectSelectorConfig(options=_TIME_ZONES, sort=False))


class EnphaseRealtimeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._user_input: dict[str, Any] = {}
        self._probe: Probe | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EnphaseRealtimeOptionsFlow()

    # --- user ---------------------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._user_input = user_input
            try:
                async with _Sessions(self.hass) as sessions:
                    cloud, envoy, probe = await _login_and_token(
                        sessions,
                        user_input[CONF_HOST],
                        user_input[CONF_EMAIL],
                        user_input[CONF_PASSWORD],
                    )
                    await self.async_set_unique_id(probe.serial)
                    self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST]})
                    if not probe.site_ids:
                        await _find_sites(cloud, probe)
                    self._probe = probe
                    if len(probe.site_ids) == 1:
                        await _inspect_site(cloud, envoy, probe, probe.site_ids[0])
            except FlowError as err:
                errors["base"] = err.key
            except AbortFlow:
                raise
            except Exception:
                _LOGGER.exception("Unexpected error setting up Enphase Realtime")
                errors["base"] = "unknown"
            else:
                if not probe.site_ids:
                    errors["base"] = "no_site"
                elif len(probe.site_ids) > 1:
                    return await self.async_step_site()
                else:
                    return await self.async_step_confirm()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
                        vol.Required(CONF_EMAIL): TextSelector(
                            TextSelectorConfig(type=TextSelectorType.EMAIL)
                        ),
                        vol.Required(CONF_PASSWORD): TextSelector(
                            TextSelectorConfig(type=TextSelectorType.PASSWORD)
                        ),
                    }
                ),
                {k: v for k, v in self._user_input.items() if k != CONF_PASSWORD},
            ),
            errors=errors,
        )

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Only for accounts with more than one site."""
        assert self._probe is not None  # noqa: S101
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                async with _Sessions(self.hass) as sessions:
                    cloud, envoy, probe = await _login_and_token(
                        sessions,
                        self._user_input[CONF_HOST],
                        self._user_input[CONF_EMAIL],
                        self._user_input[CONF_PASSWORD],
                    )
                    probe.site_ids = self._probe.site_ids
                    await _inspect_site(cloud, envoy, probe, int(user_input[CONF_SITE_ID]))
            except FlowError as err:
                errors["base"] = err.key
            except Exception:
                _LOGGER.exception("Unexpected error setting up Enphase Realtime")
                errors["base"] = "unknown"
            else:
                self._probe = probe
                return await self.async_step_confirm()

        return self.async_show_form(
            step_id="site",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SITE_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=str(i), label=str(i))
                                for i in self._probe.site_ids
                            ]
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what was found; the user can correct the region (spec 4.1 step 7)."""
        probe = self._probe
        assert probe is not None and probe.layout is not None  # noqa: S101
        settings = probe.site_settings

        if user_input is not None:
            data = {
                CONF_HOST: self._user_input[CONF_HOST],
                CONF_EMAIL: self._user_input[CONF_EMAIL],
                CONF_PASSWORD: self._user_input[CONF_PASSWORD],
                CONF_SERIAL: probe.serial,
                CONF_FIRMWARE: probe.firmware,
                CONF_SITE_ID: probe.site_id,
                CONF_TOKEN: probe.token,
                CONF_PHASE_LAYOUT: probe.layout.value,
                CONF_HAS_BATTERY: _has_battery(probe),
                CONF_HAS_ENPOWER: _has_enpower(probe),
            }
            options = {
                CONF_COUNTRY: user_input[CONF_COUNTRY],
                CONF_TIME_ZONE: user_input[CONF_TIME_ZONE],
            }
            return self.async_create_entry(
                title=f"Envoy {probe.serial}", data=data, options=options
            )

        country = (settings and settings.country_code) or self.hass.config.country
        time_zone = (settings and settings.timezone) or self.hass.config.time_zone
        return self.async_show_form(
            step_id="confirm",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_COUNTRY): CountrySelector(),
                        vol.Required(CONF_TIME_ZONE): await _time_zone_selector(self.hass),
                    }
                ),
                {CONF_COUNTRY: country, CONF_TIME_ZONE: time_zone},
            ),
            description_placeholders={
                "serial": probe.serial,
                "layout": _LAYOUT_LABELS[probe.layout],
                "hardware": _hardware_summary(probe),
            },
        )

    # --- reauth -------------------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                async with _Sessions(self.hass) as sessions:
                    _, _, probe = await _login_and_token(
                        sessions,
                        entry.data[CONF_HOST],
                        user_input[CONF_EMAIL],
                        user_input[CONF_PASSWORD],
                    )
            except FlowError as err:
                errors["base"] = err.key
            except Exception:
                _LOGGER.exception("Unexpected error re-authenticating Enphase Realtime")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(probe.serial)
                self._abort_if_unique_id_mismatch(reason="wrong_envoy")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_TOKEN: probe.token,
                        CONF_FIRMWARE: probe.firmware,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_EMAIL): TextSelector(
                            TextSelectorConfig(type=TextSelectorType.EMAIL)
                        ),
                        vol.Required(CONF_PASSWORD): TextSelector(
                            TextSelectorConfig(type=TextSelectorType.PASSWORD)
                        ),
                    }
                ),
                {CONF_EMAIL: entry.data[CONF_EMAIL]},
            ),
            errors=errors,
        )


def _hardware_summary(probe: Probe) -> str:
    parts = []
    batteries = len(probe.inventory.batteries)
    if batteries:
        parts.append(f"{batteries} IQ Batter{'y' if batteries == 1 else 'ies'}")
    elif _has_battery(probe):
        parts.append("IQ Battery")
    if _has_enpower(probe):
        parts.append("System Controller")
    return ", ".join(parts) or "No battery or System Controller"


class EnphaseRealtimeOptionsFlow(OptionsFlow):
    """Changing any option reloads the entry (see `_async_options_updated`)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **user_input,
                    CONF_LIVE_INTERVAL: int(user_input[CONF_LIVE_INTERVAL]),
                    CONF_FAST_INTERVAL: int(user_input[CONF_FAST_INTERVAL]),
                    CONF_STREAM_INTERVAL: int(user_input[CONF_STREAM_INTERVAL]),
                    CONF_CLOUD_INTERVAL: int(user_input[CONF_CLOUD_INTERVAL]),
                }
            )

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_LIVE_INTERVAL): _interval(1, 60),
                        vol.Required(CONF_FAST_INTERVAL): _interval(2, 60),
                        vol.Required(CONF_STREAM_INTERVAL): _interval(0, 60),
                        vol.Required(CONF_CLOUD_INTERVAL): _interval(60, 3600),
                        vol.Required(CONF_ENABLE_STREAM): bool,
                        vol.Required(CONF_COUNTRY): CountrySelector(),
                        vol.Required(CONF_TIME_ZONE): await _time_zone_selector(self.hass),
                    }
                ),
                {
                    CONF_LIVE_INTERVAL: options.get(CONF_LIVE_INTERVAL, DEFAULT_LIVE_INTERVAL),
                    CONF_FAST_INTERVAL: options.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL),
                    CONF_STREAM_INTERVAL: options.get(
                        CONF_STREAM_INTERVAL, DEFAULT_STREAM_INTERVAL
                    ),
                    CONF_CLOUD_INTERVAL: options.get(CONF_CLOUD_INTERVAL, DEFAULT_CLOUD_INTERVAL),
                    CONF_ENABLE_STREAM: options.get(CONF_ENABLE_STREAM, DEFAULT_ENABLE_STREAM),
                    CONF_COUNTRY: options.get(CONF_COUNTRY, self.hass.config.country),
                    CONF_TIME_ZONE: options.get(CONF_TIME_ZONE, self.hass.config.time_zone),
                },
            ),
        )
