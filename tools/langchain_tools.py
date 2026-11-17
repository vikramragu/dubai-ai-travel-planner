"""LangChain tool wrappers over ``tools/``, returning compact views (``tools/views.py``).

They let an LLM call the data tools directly (``model.bind_tools(dubai_tools(providers))``).
The Phase 4 agents call the same functions in code and put the compact views in their prompts,
which keeps runs predictable; the wrappers are what the LangGraph nodes and any tool-calling
agent use.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

import tools
from data.providers import DataProviders, get_providers
from schemas import HotelTier, Interest, MealType, PriceBand, TransportMode
from tools.views import attraction_view, hotel_view, leg_view, price_summary, restaurant_view


class _Attractions(BaseModel):
    interests: list[Interest] = Field(description="match any of these interests")
    neighborhood_ids: list[str] = Field(default_factory=list)
    avoid_crowds: bool = False
    free_only: bool = False
    limit: int = Field(default=15, ge=1, le=50)


class _Restaurants(BaseModel):
    cuisines: list[str] = Field(default_factory=list)
    price_bands: list[PriceBand] = Field(default_factory=list)
    meal_type: MealType | None = None
    near_place_id: str | None = None
    radius_km: float | None = Field(default=None, gt=0)
    limit: int = Field(default=15, ge=1, le=50)


class _Hotels(BaseModel):
    tier: HotelTier
    month: int | None = Field(default=None, ge=1, le=12)


class _Travel(BaseModel):
    from_id: str
    to_id: str
    mode: TransportMode | None = None
    party_size: int = Field(default=1, ge=1, le=10)


class _Price(BaseModel):
    place_id: str


def dubai_tools(providers: DataProviders | None = None) -> list[BaseTool]:
    p = providers or get_providers()

    def search_attractions(**kw: Any) -> list[dict[str, Any]]:
        return [attraction_view(a) for a in tools.search_attractions(providers=p, **kw)]

    def search_restaurants(**kw: Any) -> list[dict[str, Any]]:
        return [restaurant_view(r) for r in tools.search_restaurants(providers=p, **kw)]

    def get_hotel_areas(tier: HotelTier, month: int | None = None) -> list[dict[str, Any]]:
        return [hotel_view(h) for h in tools.get_hotel_areas(tier, month=month, providers=p)]

    def travel_time(
        from_id: str, to_id: str, mode: TransportMode | None = None, party_size: int = 1
    ) -> list[dict[str, Any]]:
        legs = tools.travel_time(from_id, to_id, mode, party_size=party_size, providers=p)
        return [leg_view(leg) for leg in legs]

    def price_lookup(place_id: str) -> str:
        return price_summary(tools.price_lookup(place_id, providers=p))

    return [
        StructuredTool.from_function(
            search_attractions,
            name="search_attractions",
            description="Find Dubai attractions by interest; quieter and better matches first.",
            args_schema=_Attractions,
        ),
        StructuredTool.from_function(
            search_restaurants,
            name="search_restaurants",
            description="Find restaurants by cuisine, price band, meal or distance from a place.",
            args_schema=_Restaurants,
        ),
        StructuredTool.from_function(
            get_hotel_areas,
            name="get_hotel_areas",
            description="Hotel areas for a tier with nightly price ranges (before fees).",
            args_schema=_Hotels,
        ),
        StructuredTool.from_function(
            travel_time,
            name="travel_time",
            description="Travel options between two places or neighborhoods, fastest first.",
            args_schema=_Travel,
        ),
        StructuredTool.from_function(
            price_lookup,
            name="price_lookup",
            description="Per-person price of an attraction or restaurant, with its source.",
            args_schema=_Price,
        ),
    ]
