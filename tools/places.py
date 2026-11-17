"""Place search tools: attractions, restaurants, neighborhoods and price lookup."""

from __future__ import annotations

from data.providers import AttractionQuery, DataProviders, RestaurantQuery, get_providers
from schemas import (
    Attraction,
    CrowdLevel,
    Interest,
    MealType,
    Neighborhood,
    PriceBand,
    PriceRange,
    Restaurant,
    SourcedValue,
)
from tools.errors import ToolError


def search_attractions(
    interests: list[Interest],
    *,
    neighborhood_ids: list[str] | None = None,
    avoid_crowds: bool = False,
    free_only: bool = False,
    indoor_only: bool = False,
    exclude_ids: list[str] | None = None,
    limit: int = 20,
    providers: DataProviders | None = None,
) -> list[Attraction]:
    """Attractions matching any of the interests, best matches and quieter places first.

    ``avoid_crowds`` drops high-crowd places (the "avoid crowds" constraint).
    """
    p = providers or get_providers()
    return p.places.search_attractions(
        AttractionQuery(
            interests=interests,
            neighborhood_ids=neighborhood_ids or [],
            max_crowd=CrowdLevel.MEDIUM if avoid_crowds else None,
            free_only=free_only,
            indoor_only=indoor_only,
            exclude_ids=exclude_ids or [],
            limit=limit,
        )
    )


def search_restaurants(
    *,
    cuisines: list[str] | None = None,
    price_bands: list[PriceBand] | None = None,
    meal_type: MealType | None = None,
    neighborhood_ids: list[str] | None = None,
    near_place_id: str | None = None,
    radius_km: float | None = None,
    avoid_crowds: bool = False,
    exclude_ids: list[str] | None = None,
    limit: int = 20,
    providers: DataProviders | None = None,
) -> list[Restaurant]:
    """Restaurants by cuisine / band / meal; ``near_place_id`` sorts by distance from a place."""
    p = providers or get_providers()
    near = None
    if near_place_id is not None:
        place = p.places.get_place(near_place_id)
        if place is None:
            raise ToolError(f"unknown place id: {near_place_id}")
        near = place.location
    return p.places.search_restaurants(
        RestaurantQuery(
            cuisines=cuisines or [],
            price_bands=price_bands or [],
            meal_type=meal_type,
            neighborhood_ids=neighborhood_ids or [],
            near=near,
            radius_km=radius_km,
            max_crowd=CrowdLevel.MEDIUM if avoid_crowds else None,
            exclude_ids=exclude_ids or [],
            limit=limit,
        )
    )


def list_neighborhoods(*, providers: DataProviders | None = None) -> list[Neighborhood]:
    return (providers or get_providers()).places.neighborhoods()


def get_place(place_id: str, *, providers: DataProviders | None = None) -> Attraction | Restaurant:
    place = (providers or get_providers()).places.get_place(place_id)
    if place is None:
        raise ToolError(f"unknown place id: {place_id}")
    return place


def price_lookup(
    place_id: str, *, providers: DataProviders | None = None
) -> SourcedValue[PriceRange]:
    """Per-person price range of an attraction or restaurant, with its source."""
    return get_place(place_id, providers=providers).price
