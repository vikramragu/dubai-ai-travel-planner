"""Provenance: every fact says where it came from (problem statement §5, research.md §3.3)."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Generic, Self, TypeVar

from pydantic import model_validator

from schemas.base import StrictModel

T = TypeVar("T")


class Source(StrEnum):
    VERIFIED = "verified"  # copied from an official source and dated (Visit Dubai, RTA, venue)
    API = "api"  # returned by a live API call in this run (Overpass, OSRM, Open-Meteo)
    ESTIMATED = "estimated"  # derived or assumed (range midpoint, heuristic, stale value)


class Provenance(StrictModel):
    """Where something came from. Used directly by records sourced as a whole (a fare table row,
    a taxi tariff) and via :class:`SourcedValue` for single values.

    - ``verified`` must name a ``reference`` (URL or dataset id) and an ``as_of`` date.
    - ``api`` must name the API in ``reference``.
    - ``estimated`` may optionally explain the basis in ``reference``.
    """

    source: Source
    reference: str | None = None
    as_of: date | None = None

    @model_validator(mode="after")
    def _check_provenance(self) -> Self:
        if self.source is Source.VERIFIED and (not self.reference or self.as_of is None):
            raise ValueError("verified values need a reference (URL or dataset id) and as_of date")
        if self.source is Source.API and not self.reference:
            raise ValueError("api values need a reference naming the API")
        return self


class SourcedValue(Provenance, Generic[T]):
    """A value plus where it came from (see :class:`Provenance` for the rules)."""

    value: T


def verified(value: T, reference: str, as_of: date) -> SourcedValue[T]:
    return SourcedValue(value=value, source=Source.VERIFIED, reference=reference, as_of=as_of)


def from_api(value: T, api_name: str, as_of: date | None = None) -> SourcedValue[T]:
    return SourcedValue(value=value, source=Source.API, reference=api_name, as_of=as_of)


def estimated(value: T, basis: str | None = None) -> SourcedValue[T]:
    return SourcedValue(value=value, source=Source.ESTIMATED, reference=basis)
