"""Typed tool functions over the data providers (architecture.md §3, Phase 2).

Agents call these (Phase 4 wraps them as LangChain tools). Every result is a Phase 1 model
carrying its provenance, so the UI can show where each number came from.
"""

from tools.errors import ToolError
from tools.logistics import (
    get_hotel_areas,
    hotel_fees,
    transit_fare,
    travel_matrix,
    travel_time,
    weather_forecast,
)
from tools.places import (
    get_place,
    list_neighborhoods,
    price_lookup,
    search_attractions,
    search_restaurants,
)

__all__ = [
    "ToolError",
    "get_hotel_areas",
    "get_place",
    "hotel_fees",
    "list_neighborhoods",
    "price_lookup",
    "search_attractions",
    "search_restaurants",
    "transit_fare",
    "travel_matrix",
    "travel_time",
    "weather_forecast",
]
