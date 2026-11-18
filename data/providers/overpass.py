"""Live places from OpenStreetMap via Overpass (research.md §2.3), merged with the curated data.

What it does:

- ``search_attractions`` asks Overpass for places matching the interests (museums, beaches,
  parks, malls, ...) in the Dubai area, drops any that a curated place already covers (within
  200 m or the same name: curated prices and crowd levels win), and adds a few of the rest to
  the curated results, tagged ``api``. They have no known price (an ``estimated`` range by
  category), an assumed medium crowd level and a typical visit length by category.
- ``get_place`` answers for the live places it has returned (kept in memory and in the cache).

What it leaves to the local provider (:class:`NotApplicable`, no fallback event):

- restaurants: OpenStreetMap has no prices, price bands or meal types;
- searches near a point (the scheduler's per-stop lookups), which would mean one call per meal;
- neighborhoods and curated place ids.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from core.config import Settings
from data.providers.base import (
    AttractionQuery,
    NotApplicable,
    ProviderError,
    RestaurantQuery,
)
from data.providers.http import HttpClient
from schemas import (
    Attraction,
    CrowdLevel,
    Interest,
    LatLon,
    Neighborhood,
    PriceRange,
    Restaurant,
    Source,
    estimated,
)

BBOX = (24.95, 55.0, 25.36, 55.58)  # south, west, north, east: urban Dubai
MERGE_RADIUS_KM = 0.2
MAX_HOOD_KM = 6.0  # live places further than this from every curated area are skipped
PREFIX = "osm-"


@dataclass(frozen=True)
class _Kind:
    category: str
    filters: tuple[str, ...]  # Overpass tag filters
    interests: tuple[Interest, ...]
    indoor: bool
    duration_min: int
    price: tuple[float, float]  # typical AED range per person (estimated)


KINDS = (
    _Kind("museum", ('["tourism"="museum"]',), (Interest.CULTURE,), True, 75, (0, 50)),
    _Kind(
        "heritage",
        ('["historic"~"^(fort|castle|monument|archaeological_site|building)$"]',),
        (Interest.CULTURE,),
        False,
        45,
        (0, 0),
    ),
    _Kind(
        "gallery",
        ('["tourism"="gallery"]', '["amenity"="arts_centre"]'),
        (Interest.ART, Interest.CULTURE),
        True,
        60,
        (0, 20),
    ),
    _Kind(
        "beach",
        ('["natural"="beach"]', '["leisure"="beach_resort"]'),
        (Interest.BEACH, Interest.RELAXATION),
        False,
        120,
        (0, 10),
    ),
    _Kind(
        "park",
        ('["leisure"="park"]["name"]', '["leisure"="nature_reserve"]', '["leisure"="garden"]'),
        (Interest.NATURE, Interest.RELAXATION, Interest.FAMILY),
        False,
        60,
        (0, 10),
    ),
    _Kind("mall", ('["shop"="mall"]',), (Interest.SHOPPING,), True, 90, (0, 0)),
    _Kind(
        "market",
        ('["amenity"="marketplace"]',),
        (Interest.SHOPPING, Interest.CULTURE),
        False,
        45,
        (0, 0),
    ),
    _Kind(
        "theme park",
        ('["tourism"~"^(theme_park|zoo|aquarium)$"]', '["leisure"="water_park"]'),
        (Interest.FAMILY, Interest.ADVENTURE),
        False,
        180,
        (80, 300),
    ),
)


NEAR_HOOD_KM = 2.5  # a search for some neighborhoods keeps live places this close to them
RETRY_AFTER_S = 600  # after a failure, skip Overpass for 10 minutes


def _where(el: dict[str, Any]) -> LatLon | None:
    """An element's coordinate (nodes have lat/lon, ways/relations a centre), if in Dubai."""
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    if lat is None or lon is None:
        return None
    try:
        return LatLon(lat=float(lat), lon=float(lon))
    except ValidationError:
        return None


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


class OverpassPlacesProvider:
    name = "overpass"

    def __init__(
        self,
        http: HttpClient,
        settings: Settings,
        *,
        curated: list[Attraction],
        neighborhoods: list[Neighborhood],
        extra_share: float = 0.25,
        background: bool = True,
    ) -> None:
        self.http = http
        self.settings = settings
        self.curated = curated
        self.hoods = neighborhoods
        self.extra_share = extra_share
        self._registry: dict[str, Attraction] = {}
        self._down_until = 0.0
        self.background = background
        self._loader: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ PlacesProvider
    def neighborhoods(self) -> list[Neighborhood]:
        raise NotApplicable("neighborhoods are curated")

    def search_restaurants(self, query: RestaurantQuery) -> list[Restaurant]:
        raise NotApplicable("restaurants are curated (OSM has no prices or meal types)")

    def get_place(self, place_id: str) -> Attraction | Restaurant | None:
        if not place_id.startswith(PREFIX):
            raise NotApplicable("curated id")
        if place_id in self._registry:
            return self._registry[place_id]
        stored = self.http.recall(f"place:{place_id}")
        if stored is not None:
            place = Attraction.model_validate(stored)
            self._registry[place_id] = place
            return place
        return None

    def search_attractions(self, query: AttractionQuery) -> list[Attraction]:
        """Live extras only; the chain's local provider supplies the curated results, and
        :class:`MergedPlaces` combines both."""
        if query.near is not None:
            raise NotApplicable("searches near a point use the curated data")
        wanted = set(query.interests) | {
            i for i in Interest if i.value in {t.lower() for t in query.tags}
        }
        kinds = [k for k in KINDS if not wanted or wanted & set(k.interests)]
        if not kinds or query.free_only or query.max_crowd is CrowdLevel.LOW:
            return []  # nothing to look for, or filters live data can't satisfy
        if query.indoor_only:
            kinds = [k for k in kinds if k.indoor]
        elements = self._elements()
        if query.neighborhood_ids:
            near = [h for h in self.hoods if h.id in set(query.neighborhood_ids)]
            elements = [
                el
                for el in elements
                if (loc := _where(el)) is not None
                and any(h.location.distance_km(loc) <= NEAR_HOOD_KM for h in near)
            ]
        found: list[tuple[Attraction, bool]] = []
        excluded = set(query.exclude_ids)
        for el in elements:
            made = self._to_attraction(el, kinds, wanted)
            if made is None or made[0].id in excluded:
                continue
            found.append(made)
        # Best first: more matched interests, then places notable enough to have a Wikidata or
        # Wikipedia entry (the rest are often unnamed parks or small plots).
        wanted_tags = {i.value for i in wanted}
        found.sort(key=lambda x: (-len(wanted_tags & set(x[0].tags)), not x[1], x[0].name))
        out = [place for place, _ in found]
        for place in out:
            self._registry[place.id] = place
            self.http.remember(f"place:{place.id}", place.model_dump(mode="json"))
        return out

    # ------------------------------------------------------------------ helpers
    def _query(self) -> str:
        s, w, n, e = BBOX
        lines = [f'nwr{f}["name"]({s},{w},{n},{e});' for k in KINDS for f in k.filters]
        return "[out:json][timeout:25];(" + "".join(lines) + ");out center tags 800;"

    def _download(self) -> list[dict[str, Any]]:
        body = self.http.post_json(
            "overpass",
            self.settings.overpass_url,
            data={"data": self._query()},
            ttl_h=self.settings.cache_ttl_places_h,
            timeout_s=self.settings.overpass_timeout_s,
        )
        if not isinstance(body, dict) or "elements" not in body:
            raise ProviderError("overpass: unexpected response")
        elements: list[dict[str, Any]] = body["elements"]
        return elements

    def _elements(self) -> list[dict[str, Any]]:
        """Every place type in urban Dubai from one query, cached for a week (searches filter
        it locally), so Overpass is asked at most once a week.

        Public Overpass servers often take 30+ s, so a run never waits: on a cache miss the
        download starts in a background thread and this search falls back to the curated data
        ("still loading"); later runs find it in the cache. After a failure, Overpass is left
        alone for a while."""
        cached = self.http.cached_post(self.settings.overpass_url, {"data": self._query()})
        if isinstance(cached, dict) and "elements" in cached:
            elements: list[dict[str, Any]] = cached["elements"]
            return elements
        if not self.background:
            return self._download()
        with self._lock:
            if time.monotonic() < self._down_until:
                raise ProviderError("overpass: skipped (failed recently)")
            if self._loader is None or not self._loader.is_alive():
                self._loader = threading.Thread(
                    target=self._load_in_background, name="overpass-download", daemon=True
                )
                self._loader.start()
        raise ProviderError("overpass: live places are still downloading")

    def _load_in_background(self) -> None:
        try:
            self._download()  # stores the response in the cache
        except ProviderError:
            with self._lock:
                self._down_until = time.monotonic() + RETRY_AFTER_S

    def wait_for_download(self, timeout_s: float | None = None) -> None:
        """For scripts and tests: block until a background download has finished."""
        loader = self._loader
        if loader is not None:
            loader.join(timeout_s)

    def _covered(self, name: str, loc: LatLon) -> bool:
        """A curated place is the same place: within 200 m, the same name, or one name's words
        all inside the other's ("Al Mamzar Park" vs "Al Mamzar Beach Park") within 3 km (big
        parks and beaches have their centre far from the curated pin)."""
        words = set(_norm(name).split()) - {"al", "the", "of", "dubai"}
        for a in self.curated:
            d = a.location.distance_km(loc)
            if d <= MERGE_RADIUS_KM:
                return True
            theirs = set(_norm(a.name).split()) - {"al", "the", "of", "dubai"}
            if words and theirs and (words <= theirs or theirs <= words) and d <= 3.0:
                return True
        return False

    def _kind_of(self, tags: dict[str, str], kinds: list[_Kind]) -> _Kind | None:
        for k in kinds:
            for f in k.filters:
                for key, op, value in re.findall(r'\["([^"]+)"(=|~)"([^"]+)"\]', f):
                    have = tags.get(key)
                    if have is None:
                        break
                    if (op == "=" and have != value) or (op == "~" and not re.search(value, have)):
                        break
                else:
                    return k
        return None

    def _to_attraction(
        self, el: dict[str, Any], kinds: list[_Kind], wanted: set[Interest]
    ) -> tuple[Attraction, bool] | None:
        """The live place and whether it is notable (has a Wikidata/Wikipedia link)."""
        tags: dict[str, str] = el.get("tags") or {}
        name = tags.get("name:en") or tags.get("name")
        loc = _where(el)
        kind = self._kind_of(tags, kinds)
        if not name or loc is None or kind is None:
            return None
        if self._covered(name, loc):
            return None  # a curated record covers it: curated prices and crowd levels win
        hood = min(self.hoods, key=lambda h: h.location.distance_km(loc))
        if hood.location.distance_km(loc) > MAX_HOOD_KM:
            return None
        from data.scripts.curate import osm_hours  # pure parser shared with the curation

        interests = [i.value for i in kind.interests if not wanted or i in wanted] or [
            kind.interests[0].value
        ]
        lo, hi = kind.price
        notable = "wikidata" in tags or "wikipedia" in tags
        try:
            place = Attraction(
                id=f"{PREFIX}{el['type']}-{el['id']}",
                name=name,
                category=kind.category,
                neighborhood_id=hood.id,
                location=loc,
                tags=[*interests, "osm-live"] + ([] if kind.indoor else ["outdoor"]),
                crowd_level=CrowdLevel.MEDIUM,
                opening_hours=osm_hours(tags.get("opening_hours")),
                typical_duration_min=kind.duration_min,
                price=estimated(
                    PriceRange(min_aed=lo, max_aed=hi),
                    f"unknown entry price: typical range for a {kind.category} "
                    "(OpenStreetMap has no prices)",
                ),
                source=Source.API,
                reference=f"OpenStreetMap via Overpass (live): {el['type']}/{el['id']}",
                indoor=kind.indoor,
            )
        except ValidationError:
            return None
        return place, notable


class MergedPlaces:
    """Curated places first, then a few live extras (a share of the limit) for attraction
    searches; everything else is the curated provider's answer."""

    name = "overpass+local"

    def __init__(self, live: OverpassPlacesProvider, curated: Any) -> None:
        self.live = live
        self.curated = curated

    # Everything except attraction searches and live ids is the curated provider's job; it is
    # next in the chain, so these hand over without a fallback event.
    def neighborhoods(self) -> list[Neighborhood]:
        raise NotApplicable("curated")

    def search_restaurants(self, query: RestaurantQuery) -> list[Restaurant]:
        raise NotApplicable("curated")

    def get_place(self, place_id: str) -> Attraction | Restaurant | None:
        if place_id.startswith(PREFIX):
            return self.live.get_place(place_id)
        raise NotApplicable("curated id")

    def search_attractions(self, query: AttractionQuery) -> list[Attraction]:
        curated: list[Attraction] = self.curated.search_attractions(query)
        try:
            extras = self.live.search_attractions(query)
        except NotApplicable:
            return curated
        room = max(2, int(query.limit * self.live.extra_share))
        extras = extras[:room]
        return [*curated[: max(0, query.limit - len(extras))], *extras]
