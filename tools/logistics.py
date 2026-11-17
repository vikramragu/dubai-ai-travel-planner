"""Logistics tools: hotel areas, travel time, fares and weather."""

from __future__ import annotations

from datetime import date

from data import heuristics
from data.providers import DataProviders, get_providers
from schemas import (
    DayWeather,
    HotelArea,
    HotelFees,
    HotelTier,
    Leg,
    PriceRange,
    Source,
    SourcedValue,
    TaxiKind,
    TransportMode,
    TravelMatrix,
    estimated,
)
from schemas.pricing import FareProduct, ZoneBand
from tools.errors import ToolError

OFFLINE_MODES = (TransportMode.WALK, TransportMode.METRO, TransportMode.ABRA, TransportMode.TAXI)


def get_hotel_areas(
    tier: HotelTier, *, month: int | None = None, providers: DataProviders | None = None
) -> list[HotelArea]:
    """Hotel areas for a tier, cheapest first. With ``month``, nightly ranges are scaled by
    that month's season multiplier (still before fees; see :func:`hotel_fees`)."""
    p = providers or get_providers()
    areas = p.hotels.areas(tier)
    if month is None:
        return areas
    season = p.hotels.season(month)
    out = []
    for a in areas:
        r = a.nightly_rate.value
        scaled = PriceRange(
            min_aed=round(r.min_aed * season.multiplier),
            max_aed=round(r.max_aed * season.multiplier),
        )
        rate = estimated(
            scaled,
            f"{a.nightly_rate.reference}; x{season.multiplier} for month {month} ({season.label})",
        )
        out.append(a.model_copy(update={"nightly_rate": rate}))
    return out


def hotel_fees(tier: HotelTier, *, providers: DataProviders | None = None) -> HotelFees:
    """Tourism Dirham, service charge, municipality fee and VAT for a tier."""
    return (providers or get_providers()).hotels.fees(tier)


def travel_time(
    from_id: str,
    to_id: str,
    mode: TransportMode | None = None,
    *,
    party_size: int = 1,
    providers: DataProviders | None = None,
) -> list[Leg]:
    """Legs between two places or neighborhoods, fastest first.

    With ``mode`` the list has at most one leg; without it, every sensible offline mode
    (walk, metro, abra, taxi) is returned. Costs are per person.
    """
    if party_size < 1:
        raise ToolError("party_size must be at least 1")
    if from_id == to_id:
        return []
    p = providers or get_providers()
    modes = (mode,) if mode is not None else OFFLINE_MODES
    legs = [
        leg
        for m in modes
        if (leg := p.routes.travel(from_id, to_id, m, party_size=party_size)) is not None
    ]
    return sorted(legs, key=lambda leg: (leg.duration_min.value, leg.cost_aed.value))


def travel_matrix(
    neighborhood_ids: list[str],
    modes: list[TransportMode] | None = None,
    *,
    providers: DataProviders | None = None,
) -> TravelMatrix:
    """Stored neighborhood-to-neighborhood times (symmetric lookups)."""
    return (providers or get_providers()).routes.matrix(
        neighborhood_ids, modes or list(OFFLINE_MODES)
    )


def transit_fare(
    mode: TransportMode,
    distance_km: float,
    *,
    product: FareProduct = FareProduct.SILVER,
    night: bool = False,
    providers: DataProviders | None = None,
) -> SourcedValue[float]:
    """Fare for one trip (per vehicle for taxis, per person otherwise).

    Metro/bus/tram use the nol zone band estimated from distance; taxis use the RTA tariff.
    """
    if distance_km < 0:
        raise ToolError("distance_km must be non-negative")
    p = providers or get_providers()
    if mode is TransportMode.WALK:
        return estimated(0.0, "walking is free")
    if mode in (TransportMode.TAXI, TransportMode.RIDE_HAIL):
        if mode is TransportMode.TAXI:
            kind = TaxiKind.STREET_NIGHT if night else TaxiKind.STREET_DAY
        else:
            kind = TaxiKind.APP_NIGHT if night else TaxiKind.APP_OFF_PEAK
        tariff = p.pricing.taxi_tariff(kind)
        if tariff is None:
            raise ToolError(f"no taxi tariff for {kind.value}")
        return SourcedValue[float](
            value=tariff.fare(distance_km),
            source=tariff.source,
            reference=f"{tariff.reference} ({kind.value}, {distance_km} km)",
            as_of=tariff.as_of,
        )
    if mode is TransportMode.ABRA:
        product, band = FareProduct.CASH, ZoneBand.FLAT
    else:
        band = ZoneBand(heuristics.zone_band_for_km(distance_km))
    fare = p.pricing.transit_fare(mode, product, band)
    if fare is None:
        raise ToolError(f"no {product.value} fare for {mode.value} ({band.value})")
    # The fare itself may be verified, but the zone band is inferred from distance.
    source_note = "" if band is ZoneBand.FLAT else "; zone band estimated from distance"
    return SourcedValue[float](
        value=fare.fare_aed,
        source=fare.source if band is ZoneBand.FLAT else Source.ESTIMATED,
        reference=f"{fare.reference} ({product.value}, {band.value}{source_note})",
        as_of=fare.as_of,
    )


def weather_forecast(
    start: date, days: int, *, providers: DataProviders | None = None
) -> list[DayWeather]:
    """Weather per trip day. Offline this is the climate normal for each date."""
    if not 1 <= days <= 14:
        raise ToolError("days must be between 1 and 14")
    return (providers or get_providers()).weather.forecast(start, days)
