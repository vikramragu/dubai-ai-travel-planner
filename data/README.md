# Curated Dubai dataset

The offline data layer the agents use. Everything here comes from free and open sources;
nothing needs an API key. See [docs/research.md](../docs/research.md) for the wider source survey.

## What's in it

| File (`data/seed/`) | Contents | Where it comes from | Provenance |
|---|---|---|---|
| `neighborhoods.json` | 21 neighborhoods: centre, nearest metro station, beach access | Hand-curated (`data/curation/`) | centres approximate |
| `attractions.json` | 50 attractions: tags, crowd level, visit time, opening hours, price | Curated list matched to **OpenStreetMap** for coordinates, OSM id and (when tagged) opening hours | record `verified` (links to the OSM element) or `estimated` (3 places not in OSM); price `verified` for Etihad and Al Shindagha museums (Dubai government resolution), otherwise `estimated` |
| `restaurants.json` | 62 restaurants, mostly local and regional food | Chosen from the **OpenStreetMap** export; price per person from the price band | record `verified`/`estimated` as above; prices `estimated` |
| `hotels.json` | 17 hotel areas (neighborhood × tier), monthly season multipliers, fees per tier | Author estimates of typical online-travel-agency ranges; Tourism Dirham schedule from 2025-26 guides | `estimated` (no free hotel-price API exists) |
| `pricing.json` | Nol fares (metro, bus, tram; Red, Silver, Gold, Blue), abra fare, taxi tariffs | **RTA** Nol fares page and **Dubai Taxi** tariff page; app-booking fees from press | fares and street-hail tariffs `verified` (2026-11-15); app/airport tariffs and abra `estimated` |
| `travel_matrix.json` | Times and distances between every neighborhood pair by taxi, walk, metro, abra | Heuristic: haversine × detour factor; taxi speed per stretch (22 km/h for the first 3 km rising to 85 km/h beyond 30 km) (`data/heuristics.py`) | `estimated`; Phase 7 replaces with OSRM / GTFS |
| `climate.json` | Monthly max/min temperature, rainfall, rainy days, humidity | **Open-Meteo** historical archive (ERA5), daily data 2015–2024 | `verified` |

Coverage of the example request (Phase 2 exit criterion, at least 5 each): 26 culture attractions,
9 beaches, 44 local-food restaurants, 5 budget hotel areas, 24 low-crowd attractions
(`uv run python -m data.build_db` prints the current numbers).

### Provenance rules

- `verified`: copied from a named source on a given date (`reference` + `as_of`). For a place
  record, it means the name and location were matched to the linked OpenStreetMap element.
- `estimated`: an author estimate, a heuristic, or a figure from secondary sources (resellers,
  press). `reference` explains the basis.
- `api`: only for values fetched live during a run (Phase 7).

### Known limits

- Opening hours: 25 come from OSM tags, 41 are curated approximations, 46 (mostly restaurants)
  are unknown (`null`). Check hours before a demo.
- Seasonal attractions (Global Village, Miracle Garden) are open roughly October–May; they carry a
  `seasonal` tag but opening hours don't model the season.
- Hotel rates and the season multipliers are estimates. Rates are before fees; apply `hotel_fees`.
- Taxi costs exclude Salik road tolls (about AED 4–6 per gate).
- Crowd levels are author judgements (research.md §2.9).

## Layout

```
data/
├── curation/        hand-written inputs: neighborhoods, attractions, restaurants (+ match rules)
├── raw/osm/         OpenStreetMap export, one file per neighborhood (+ _lookups.json)
├── seed/            validated seed JSON (generated or hand-curated; committed)
├── scripts/         osm_export, curate, build_matrix, climate_normals, staleness_report
├── providers/       base protocols, local SQLite provider, fallback chains
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
uv run python -m data.scripts.build_matrix                    # neighborhoods -> travel_matrix.json
uv run python -m data.scripts.climate_normals 2015 2024       # Open-Meteo -> climate.json
```

`tests/unit/test_seed_scripts.py` re-runs `curate` and `build_matrix` offline and fails if the
committed seed files differ, so the seed can't drift from its inputs or be edited by hand.
To change a place, edit `data/curation/*.json` and rerun `curate`.

## Licences and attribution

- OpenStreetMap data © OpenStreetMap contributors, available under the
  [ODbL](https://www.openstreetmap.org/copyright). The UI must show this attribution.
- Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0), based on Copernicus ERA5.
- Fares and tariffs from [rta.ae](https://rta.ae) and [dubaitaxi.ae](https://www.dubaitaxi.ae);
  museum prices from Dubai Administrative Resolution No. 256 of 2024 (dlp.dubai.gov.ae).
