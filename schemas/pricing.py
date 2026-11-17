"""Reference price tables: transit fares, taxi tariffs and hotel fees (research.md §2.5-2.6)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, PositiveFloat

from schemas.common import AED, HotelTier
from schemas.logistics import TransportMode
from schemas.provenance import Provenance


class FareProduct(StrEnum):
    RED_TICKET = "red_ticket"  # paper single-trip ticket
    SILVER = "silver"  # standard nol card — the default for visitors
    GOLD = "gold"  # first-class cabin
    BLUE = "blue"  # personalised card
    CASH = "cash"  # abra crossings


class ZoneBand(StrEnum):
    ONE_ZONE = "1_zone"
    TWO_ZONES = "2_zones"
    MULTI_ZONE = "3_plus_zones"
    FLAT = "flat"  # one price regardless of distance (abra)


class TransitFare(Provenance):
    """One-way fare for a mode, ticket product and zone band."""

    mode: TransportMode
    product: FareProduct
    zone_band: ZoneBand
    fare_aed: AED


class TaxiKind(StrEnum):
    STREET_DAY = "street_day"  # hailed 06:00-22:00
    STREET_NIGHT = "street_night"  # hailed 22:00-06:00
    APP_PEAK = "app_peak"  # booked in an app, weekday 08:00-10:00 and 16:00-20:00
    APP_OFF_PEAK = "app_off_peak"
    APP_NIGHT = "app_night"
    AIRPORT = "airport"  # pick-up at DXB


class TaxiTariff(Provenance):
    kind: TaxiKind
    flagfall_aed: AED
    per_km_aed: AED
    minimum_aed: AED
    booking_fee_aed: AED = 0.0
    waiting_per_min_aed: AED = 0.0

    def fare(self, distance_km: float, waiting_min: float = 0.0) -> float:
        """Metered fare (before Salik tolls), never below the minimum."""
        metered = (
            self.flagfall_aed
            + self.booking_fee_aed
            + self.per_km_aed * distance_km
            + self.waiting_per_min_aed * waiting_min
        )
        return round(max(self.minimum_aed, metered), 2)


class HotelFees(Provenance):
    """Charges added on top of a quoted room rate, per hotel tier."""

    tier: HotelTier
    tourism_dirham_per_room_night: AED
    service_charge_pct: float = Field(ge=0, le=30)
    municipality_fee_pct: float = Field(ge=0, le=30)
    vat_pct: float = Field(ge=0, le=30)

    def total_per_night(self, room_rate_aed: float) -> float:
        """Room rate plus percentage fees plus the flat Tourism Dirham."""
        pct = self.service_charge_pct + self.municipality_fee_pct + self.vat_pct
        return round(room_rate_aed * (1 + pct / 100) + self.tourism_dirham_per_room_night, 2)


class HotelSeason(Provenance):
    """Multiplier applied to the base nightly range for a calendar month."""

    month: int = Field(ge=1, le=12)
    multiplier: PositiveFloat
    label: str = ""
