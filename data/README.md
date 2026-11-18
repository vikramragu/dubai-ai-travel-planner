# Curated Dubai dataset

The data layer the agents use: a curated offline dataset, plus free live sources in `hybrid` /
`live` mode (see [Live data](#live-data-phase-7)). Everything comes from free and open sources;
nothing needs an API key. See [docs/research.md](../docs/research.md) for the wider source survey.

## What's in it

| File (`data/seed/`) | Contents | Where it comes from | Provenance |
|---|---|---|---|
| `neighborhoods.json` | 21 neighborhoods: centre, nearest metro station, beach access | Hand-curated (`data/curation/`) | centres approximate |
| `attractions.json` | 50 attractions: tags, crowd level, visit time, opening hours, price | Curated list matched to **OpenStreetMap** for coordinates, OSM id and (when tagged) opening hours | record `verified` (links to the OSM element) or `estimated` (3 places not in OSM); price `verified` for Etihad and Al Shindagha museums (Dubai government resolution), otherwise `estimated` |
| `restaurants.json` | 62 restaurants, mostly local and regional food | Chosen from the **OpenStreetMap** export; price per person from the price band | record `verified`/`estimated` as above; prices `estimated` |
| `hotels.json` | 17 hotel areas (neighborhood × tier), monthly season multipliers, fees per tier | Author estimates of typical online-travel-agency ranges; Tourism Dirham schedule from 2025-26 guides | `estimated` (no free hotel-price API exists) |
| `pricing.json` | Nol fares (metro, bus, tram; Red, Silver, Gold, Blue), abra fare, taxi tariffs | **RTA** Nol fares page and **Dubai Taxi** tariff page; app-booking fees from press | fares and street-hail tariffs `verified` (2026-11-15); app/airport tariffs and abra `estimated` |
| `road_times.json` | Driving and walking times between every curated point (21 neighborhood centres, 50 attractions, 62 restaurants: ~17,500 routes per profile) | **OSRM** on OpenStreetMap: car from router.project-osrm.org, foot from routing.openstreetmap.de, fetched once with `osrm_tables.py` | `estimated` (stored, no live traffic); legs add a 5-min taxi pickup and a city-traffic factor (×1.35 for the first ~10 km, fading on highways) |
| `travel_matrix.json` | Times and distances between every neighborhood pair by taxi, walk, metro, abra | Taxi and walk from `road_times.json`; metro and abra from the heuristic (haversine × detour, station walks and transfers; `data/heuristics.py`) | `estimated` |
| `climate.json` | Monthly max/min temperature, rainfall, rainy days, humidity | **Open-Meteo** historical archive (ERA5), daily data 2015–2024 | `verified` |

Coverage of the example request (Phase 2 exit criterion, at least 5 each): 26 culture attractions,
9 beaches, 44 local-food restaurants, 5 budget hotel areas, 24 low-crowd attractions
(`uv run python -m data.build_db` prints the current numbers).

### Provenance rules

- `verified`: copied from a named source on a given date (`reference` + `as_of`). For a place
  record, it means the name and location were matched to the linked OpenStreetMap element.
- `estimated`: an author estimate, a heuristic, or a figure from secondary sources (resellers,
  press). `reference` explains the basis.
- `api`: only for values fetched live during the current run (OSRM legs, Open-Meteo forecasts,
  OpenStreetMap places in hybrid/live mode). Stored OSRM routes are `estimated`: they were
  fetched once, not in this run, and have no live traffic.

### Known limits

- Opening hours: 25 come from OSM tags, 41 are curated approximations, 46 (mostly restaurants)
  are unknown (`null`). Check hours before a demo.
- Seasonal attractions (Global Village, Miracle Garden) are open roughly October–May; they carry a
  `seasonal` tag but opening hours don't model the season.
- Hotel rates and the season multipliers are estimates. Rates are before fees; apply `hotel_fees`.
- Taxi costs exclude Salik road tolls (about AED 4–6 per gate).
- Crowd levels are author judgements (research.md §2.9).
- Walking: OSRM's foot profile crosses Dubai Creek on the abra ferry; the planner keeps the abra
  as its own mode, so walking legs never cross the creek, and walks over 2.5 km (straight line)
  or with a route over 3× the straight line aren't offered.
- Metro times stay heuristic: the official RTA GTFS feed on Dubai Pulse needs a permission
  request, and the public copies are token-gated or out of date (checked 2026-11-18).

## Layout

```
data/
├── curation/        hand-written inputs: neighborhoods, attractions, restaurants (+ match rules)
├── raw/osm/         OpenStreetMap export, one file per neighborhood (+ _lookups.json)
├── seed/            validated seed JSON (generated or hand-curated; committed)
├── scripts/         osm_export, curate, osrm_tables, build_matrix, climate_normals,
│                    staleness_report
├── providers/       base protocols, local SQLite provider, fallback chains, live providers
│                    (http.py, runtime.py, osrm.py, open_meteo.py, overpass.py)
├── heuristics.py    travel-time estimates shared by the matrix and the routes fallback
├── dataset.py       load + cross-validate the seed, coverage report
└── build_db.py      seed JSON → dubai.sqlite (git-ignored, rebuilt on demand)
```

## Commands

```bash
uv run python -m data.build_db                       # validate seed + build data/dubai.sqlite
uv run python -m data.scripts.staleness_report 6     # values with as_of older than 6 months

# Refreshing the data (needs network; be gentle with the public servers)
uv run python -m data.scripts.osm_export [NEIGHBORHOOD ...]   # re-export OSM POIs
uv run python -m data.scripts.curate [--lookup]               # curation + OSM -> seed
uv run python -m data.scripts.osrm_tables                     # OSRM -> road_times.json (~3 min)
uv run python -m data.scripts.build_matrix                    # neighborhoods -> travel_matrix.json
uv run python -m data.scripts.climate_normals 2015 2024       # Open-Meteo -> climate.json
```

`tests/unit/test_seed_scripts.py` re-runs `curate` and `build_matrix` offline and fails if the
committed seed files differ, so the seed can't drift from its inputs or be edited by hand.
To change a place, edit `data/curation/*.json` and rerun `curate`.

## Live data (Phase 7)

`DATA_MODE` chooses the provider chains (`data/providers/__init__.py`). The curated data is
always the last link, so a run never fails because a live source is down: the failure is
logged as a `fallback` event in the run's trace and the plan carries on.

| Mode | Places | Routes | Weather |
|---|---|---|---|
| `local` (default) | curated | stored OSRM routes, heuristic for metro/abra | climate normals |
| `hybrid` | curated + a few live OpenStreetMap extras | stored routes; live OSRM only for live places; max `EXTERNAL_CALL_BUDGET` (40) network calls per run | Open-Meteo forecast (≤ 16 days ahead), else climate |
| `live` | as hybrid | live OSRM for every taxi/walking leg, no call budget | as hybrid |

- **Overpass (places):** one city-wide query for museums, heritage sites, galleries, beaches,
  parks, malls, markets and theme parks, cached for 7 days and filtered locally per search. A
  place that a curated record covers (within 200 m, the same name, or one name's words inside
  the other's) is dropped: curated prices and crowd levels win. Live places get an `estimated`
  price range by category, a medium crowd level and a typical visit length. Public Overpass
  servers often take 30–60 s, so the download runs in the background: a run never waits, and
  later runs use the cached result. Restaurants and "near this stop" searches stay curated
  (OpenStreetMap has no prices or meal types).
- **OSRM (routes):** `/route` per leg and `/table` for the neighborhood matrix; the same leg
  rules as the stored routes. A live-mode run's first plan takes about a minute (one request
  per second per server, for fair use); cached plans are instant.
- **Open-Meteo (weather):** daily forecast for trip days inside the 16-day horizon; later days
  and undated trips use the climate normals.
- **HTTP layer (`http.py`):** on-disk cache (`CACHE_DIR/http`; places 7 days, routes 7 days,
  weather 3 h), a generic User-Agent, at most one request per second per host, a per-run call
  budget, and a `tool_call` trace event for every network request.
- **Tests:** `tests/unit/test_live_providers.py` and `tests/graph/test_live_modes.py` replay
  recorded responses (`tests/fixtures/http/`) and simulate timeouts, 429/5xx and a dead network.

## Licences and attribution

- OpenStreetMap data © OpenStreetMap contributors, available under the
  [ODbL](https://www.openstreetmap.org/copyright). The UI must show this attribution.
- Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0), based on Copernicus ERA5.
- Routes by [OSRM](https://project-osrm.org/) (BSD-2) on OpenStreetMap data; public servers
  router.project-osrm.org and routing.openstreetmap.de (FOSSGIS), used within their fair-use
  rules.
- Fares and tariffs from [rta.ae](https://rta.ae) and [dubaitaxi.ae](https://www.dubaitaxi.ae);
  museum prices from Dubai Administrative Resolution No. 256 of 2024 (dlp.dubai.gov.ae).
