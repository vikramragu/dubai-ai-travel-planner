"""Fallback chains (architecture.md §9.2).

A chain tries each provider in order. When one raises, times out or returns nothing, the chain
emits a ``fallback`` trace event and tries the next. The local provider is always last, so a
chain only fails when the curated data itself has no answer. In Phase 2 the chains contain only
the local provider; Phase 7 puts Overpass / OSRM / Open-Meteo in front of it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from typing import Any, Generic, TypeVar

from core.tracing import TraceEvent, TraceKind
from data.providers.base import (
    AttractionQuery,
    NotApplicable,
    PlacesProvider,
    ProviderError,
    RestaurantQuery,
    RoutesProvider,
    WeatherProvider,
)
from data.providers.runtime import data_event
from schemas import (
    Attraction,
    DayWeather,
    Leg,
    Neighborhood,
    Restaurant,
    TransportMode,
    TravelMatrix,
)

R = TypeVar("R")
P = TypeVar("P", PlacesProvider, RoutesProvider, WeatherProvider)
Emit = Callable[[TraceEvent], None]


class _Chain(Generic[P]):
    def __init__(
        self, providers: Sequence[P], *, emit: Emit | None = None, run_id: str = "offline"
    ) -> None:
        if not providers:
            raise ValueError("a chain needs at least one provider")
        self.providers: list[P] = list(providers)
        self.emit = emit
        self.run_id = run_id
        self.name = "chain(" + ",".join(p.name for p in self.providers) + ")"

    def _first(
        self,
        op: str,
        call: Callable[[P], R],
        empty: Callable[[R], bool],
        unhandled: Callable[[], R] | None = None,
    ) -> R:
        """The first non-empty answer. ``unhandled`` gives the answer when no provider handles
        the request at all (every one is :class:`NotApplicable`)."""
        answers: list[R] = []  # empty answers, kept so "no result" is not an error
        errors: list[str] = []
        for i, provider in enumerate(self.providers):
            pname = provider.name
            try:
                result = call(provider)
            except NotApplicable:
                continue
            except (ProviderError, TimeoutError, OSError) as exc:
                errors.append(f"{pname}: {exc}")
                self._fallback(op, pname, f"failed: {exc}", i)
                continue
            except Exception as exc:
                # A live source answering in an unexpected way must not break a run; a bug in
                # the last provider (the curated data) still surfaces.
                if i == len(self.providers) - 1:
                    raise
                errors.append(f"{pname}: {type(exc).__name__}: {exc}")
                self._fallback(op, pname, f"failed unexpectedly: {type(exc).__name__}", i)
                continue
            if not empty(result):
                return result
            answers.append(result)
            self._fallback(op, pname, "returned no results", i)
        if answers:
            return answers[-1]
        if not errors and unhandled is not None:
            return unhandled()
        raise ProviderError(f"{op}: every provider failed ({'; '.join(errors)})")

    def _fallback(self, op: str, provider: str, why: str, index: int) -> None:
        if index == len(self.providers) - 1:
            return  # nothing left to fall back to; the caller sees the result/error
        nxt = self.providers[index + 1].name
        summary = f"{provider}.{op} {why}; using {nxt}"
        payload = {"op": op, "from": provider, "to": nxt}
        if self.emit is not None:
            self.emit(
                TraceEvent(
                    run_id=self.run_id,
                    agent="data",
                    kind=TraceKind.FALLBACK,
                    summary=summary,
                    payload=payload,
                )
            )
        else:  # the current run's trace (see data.providers.runtime)
            data_event(TraceKind.FALLBACK, summary, payload)


def _no_items(result: Any) -> bool:
    return isinstance(result, list) and not result


class ChainedPlacesProvider(_Chain[PlacesProvider]):
    def neighborhoods(self) -> list[Neighborhood]:
        return self._first("neighborhoods", lambda p: p.neighborhoods(), _no_items)

    def search_attractions(self, query: AttractionQuery) -> list[Attraction]:
        return self._first("search_attractions", lambda p: p.search_attractions(query), _no_items)

    def search_restaurants(self, query: RestaurantQuery) -> list[Restaurant]:
        return self._first("search_restaurants", lambda p: p.search_restaurants(query), _no_items)

    def get_place(self, place_id: str) -> Attraction | Restaurant | None:
        return self._first(
            "get_place", lambda p: p.get_place(place_id), lambda r: r is None, lambda: None
        )


class ChainedRoutesProvider(_Chain[RoutesProvider]):
    def travel(
        self, from_id: str, to_id: str, mode: TransportMode, *, party_size: int = 1
    ) -> Leg | None:
        # ``None`` is a real answer here ("this mode isn't an option", e.g. too far to walk),
        # not a miss: only an error or an unknown id moves on to the next provider.
        return self._first(
            "travel",
            lambda p: p.travel(from_id, to_id, mode, party_size=party_size),
            lambda r: False,
            unhandled=lambda: None,  # nobody offers this mode between these places
        )

    def matrix(
        self, neighborhood_ids: Sequence[str], modes: Sequence[TransportMode]
    ) -> TravelMatrix:
        return self._first(
            "matrix", lambda p: p.matrix(neighborhood_ids, modes), lambda m: not m.entries
        )


class ChainedWeatherProvider(_Chain[WeatherProvider]):
    def forecast(self, start: date, days: int) -> list[DayWeather]:
        return self._first("forecast", lambda p: p.forecast(start, days), _no_items)
