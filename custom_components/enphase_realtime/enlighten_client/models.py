"""Typed views of the Enlighten cloud payloads the integration reads.

The battery-config endpoints wrap their body as `{"type": ..., "timestamp": ..., "data": {...}}`;
the parsers take the whole response and unwrap it. A payload missing a field the model needs
raises `EnlightenParseError`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .errors import EnlightenParseError

__all__ = ["EnlightenParseError"]  # re-exported: parsers raise it, callers catch it from here


@contextmanager
def _parsing(what: str) -> Iterator[None]:
    """Turn the lookup errors a malformed payload causes into one exception type."""
    try:
        yield
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as err:
        if isinstance(err, EnlightenParseError):
            raise
        raise EnlightenParseError(f"unexpected {what} payload: {err!r}") from err


@dataclass(frozen=True, slots=True)
class Site:
    id: int
    title: str


def parse_search_sites(data: Any) -> list[Site]:
    """`search_sites.json` → sites, in order, without duplicates.

    The endpoint has been seen listing the same site twice, and returning an empty list for an
    account that has a site (spike S2), so an empty result isn't proof there are none.
    """
    with _parsing("search_sites"):
        sites: dict[int, Site] = {}
        for s in data["sites"]:
            site_id = int(s["id"])
            sites.setdefault(site_id, Site(id=site_id, title=s.get("title") or ""))
        return list(sites.values())


@dataclass(frozen=True, slots=True)
class SiteSettings:
    """Region and hardware flags from `siteSettings` (spec 3.3)."""

    country_code: str | None
    region: str | None
    timezone: str | None
    locale: str | None
    is_emea: bool
    has_encharge: bool
    has_enpower: bool
    show_charge_from_grid: bool
    restrict_cfg: bool

    @property
    def needs_itc_disclaimer(self) -> bool:
        """The ITC disclaimer is the US Investment Tax Credit; other markets are unknown (S8)."""
        return self.country_code == "US"

    @classmethod
    def from_payload(cls, payload: Any) -> SiteSettings:
        with _parsing("siteSettings"):
            data = payload["data"]
            return cls(
                country_code=data.get("countryCode"),
                region=data.get("region"),
                timezone=data.get("timezone"),
                locale=data.get("locale"),
                is_emea=bool(data.get("isEmea")),
                has_encharge=bool(data.get("hasEncharge")),
                has_enpower=bool(data.get("hasEnpower")),
                show_charge_from_grid=bool(data.get("showChargeFromGrid")),
                restrict_cfg=bool(data.get("restrictCfg")),
            )


@dataclass(frozen=True, slots=True)
class BatterySettings:
    """`batterySettings`. Times are minutes after midnight in the site's time zone."""

    profile: str
    backup_percentage: int
    backup_percentage_min: int | None
    backup_percentage_max: int | None
    very_low_soc: int | None
    very_low_soc_min: int | None
    very_low_soc_max: int | None
    charge_from_grid: bool
    charge_from_grid_schedule_enabled: bool
    charge_begin_time: int | None
    charge_end_time: int | None
    accepted_itc_disclaimer: str | None
    requested_config: dict[str, Any] = field(default_factory=dict)
    # `cfgControl.show`: whether the app offers charge from grid. Unlike `hideChargeFromGrid`, it
    # stays true in `backup_only` (spec 3.3).
    charge_from_grid_offered: bool = False

    @property
    def pending_gateways(self) -> list[Any]:
        """Gateways yet to apply a change. `requestedConfig` is `{}` when nothing is pending."""
        return list(self.requested_config.get("pendingGateways") or [])

    @property
    def has_pending_change(self) -> bool:
        return bool(self.pending_gateways)

    @classmethod
    def from_payload(cls, payload: Any) -> BatterySettings:
        with _parsing("batterySettings"):
            data = payload["data"]
            return cls(
                profile=data["profile"],
                backup_percentage=data["batteryBackupPercentage"],
                backup_percentage_min=data.get("batteryBackupPercentageMin"),
                backup_percentage_max=data.get("batteryBackupPercentageMax"),
                very_low_soc=data.get("veryLowSoc"),
                very_low_soc_min=data.get("veryLowSocMin"),
                very_low_soc_max=data.get("veryLowSocMax"),
                charge_from_grid=bool(data.get("chargeFromGrid")),
                charge_from_grid_schedule_enabled=bool(data.get("chargeFromGridScheduleEnabled")),
                charge_begin_time=data.get("chargeBeginTime"),
                charge_end_time=data.get("chargeEndTime"),
                accepted_itc_disclaimer=data.get("acceptedItcDisclaimer"),
                requested_config=dict(data.get("requestedConfig") or {}),
                charge_from_grid_offered=bool((data.get("cfgControl") or {}).get("show")),
            )


def charge_from_grid_available(site: SiteSettings, battery: BatterySettings | None) -> bool:
    """Whether to create the charge-from-grid switch (spec 3.3). `restrictCfg` rules it out.
    Otherwise either flag will do: the reference site, where charging from the grid works, has
    `showChargeFromGrid` false but `cfgControl.show` true."""
    if site.restrict_cfg:
        return False
    return site.show_charge_from_grid or (battery is not None and battery.charge_from_grid_offered)


@dataclass(frozen=True, slots=True)
class GridControlCheck:
    """`grid_control_check.json`, asked before a grid relay write (spec 6.3).

    What each flag means is part of spike S4. Until then every flag that is set counts as a
    reason to refuse.
    """

    flags: dict[str, bool]

    @property
    def blockers(self) -> list[str]:
        return [name for name, is_set in self.flags.items() if is_set]

    @classmethod
    def from_payload(cls, data: Any) -> GridControlCheck:
        with _parsing("grid_control_check"):
            if not isinstance(data, dict):
                raise EnlightenParseError("grid_control_check isn't an object")
            return cls(flags={k: bool(v) for k, v in data.items()})
