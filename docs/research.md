# Data Research — Dubai AI Travel Planner

> How the project will get the data its agents need, what each source provides, how it is
> accessed, its limits, and how it maps onto the agents. Companion to
> [problemStatement.md](problemStatement.md) and [architecture.md](architecture.md).

> **Cost policy:** the project uses **free and open data sources only**. No paid APIs
> are used. The LLM is the only metered dependency.

> **Verification note:** API products, free tiers, fares and attraction prices change. Every
> number in this file is a *planning reference* and must be checked against the official
> source (linked) before it goes into the curated dataset, which records its own
> `source_url` and `as_of` date for each value.

---

## 1. Data Needs by Agent

| Agent | Data needed | Primary source | Fallback |
|-------|-------------|----------------|----------|
| Destination Research | Attractions, cultural sites, beaches, restaurants, neighborhoods, opening hours, crowd level | Curated dataset (from Visit Dubai, venue sites) + OSM Overpass | Curated dataset |
| Logistics | Hotel areas, coordinates, travel times/distances, transit options | Curated neighborhood matrix + OSRM (driving/walking) + RTA info | Haversine × mode-speed heuristic |
| Budget | Hotel nightly rates, meal prices, entry fees, metro fares, taxi tariffs | Curated price tables (RTA, venue sites, hotel ranges); optional Amadeus test env | Curated ranges (tagged `estimated`) |
| Review | Opening hours, crowd levels, weather, all of the above | Same as above + Open-Meteo | Seasonal climate averages |
| Orchestrator | Defaults (pace hours, meal times), currency rules | Config | — |

---

## 2. Source-by-Source Research

### 2.1 Visit Dubai (official tourism portal)

- **URL:** https://www.visitdubai.com
- **Provides:** Official descriptions of attractions, neighborhoods (Al Fahidi, Al Seef, Deira, Jumeirah, Dubai Marina, Hatta…), cultural experiences, events, beaches, and often entry prices and hours.
- **Access:** No public API. Used **manually** to curate the seed dataset (read the pages, record facts with the URL and date).
- **Considerations:** Review the site's terms of use; store only facts we summarise (name, category, hours, price, location) and link back. Do not bulk-scrape or copy long descriptions.
- **Source tag:** `verified` (curated, with `source_url`, `as_of`).

### 2.2 Data.Dubai / Dubai Pulse (government open data)

- **URLs:** https://data.dubai (current open-data portal); Dubai Pulse (https://www.dubaipulse.gov.ae) is the older platform — check which one hosts each dataset now.
- **Provides (candidate datasets to look for):** tourism statistics, hotel establishment lists and classifications (DET / Dubai Economy & Tourism), RTA datasets (metro stations, bus stops, routes, ridership, and possibly a **GTFS** public-transport feed), points of interest, public beaches and parks.
- **Access:** Dataset downloads (CSV/JSON) and some APIs; some require (free) registration.
- **Use in project:**
  - Metro/tram station coordinates → nearest-station lookups for neighborhoods and attractions.
  - GTFS feed (if available) → offline metro/tram journey times (see §2.4).
  - Hotel establishment lists → which hotels exist per area and star rating (to back up the hotel-area table).
  - Ridership or visitor stats → a rough input for the `crowd_level` heuristic.
- **Source tag:** `verified` (government dataset, record dataset id + date).

### 2.3 OpenStreetMap via Overpass API (places & restaurants)

- **Docs:** https://wiki.openstreetmap.org/wiki/Overpass_API · public endpoint `https://overpass-api.de/api/interpreter`
- **Provides:** POIs with tags: `tourism=museum|attraction|viewpoint`, `historic=*`, `amenity=restaurant|cafe`, `cuisine=*`, `leisure=beach_resort|park`, `natural=beach`, `opening_hours`, `website`, coordinates.
- **Access:** Free, no key. The public endpoint has fair-use limits (keep queries small; set a descriptive generic User-Agent; cache results). Can self-host for heavier use, or download a UAE extract from Geofabrik and query it offline.
- **Licence:** ODbL — attribution "© OpenStreetMap contributors" is required in the UI; derived databases have share-alike obligations.
- **Example query (restaurants near Al Fahidi):**

  ```
  [out:json][timeout:25];
  (
    node["amenity"="restaurant"](around:800,25.2637,55.2972);
    way["amenity"="restaurant"](around:800,25.2637,55.2972);
  );
  out center tags 50;
  ```

- **Strengths / weaknesses:** Good coverage of coordinates and categories. Weak on prices, ratings and up-to-date hours — these come from the curated dataset.
- **Use in project:** the live places provider; bootstrapping the seed dataset (coordinates and categories), then curated by hand.
- **Source tag:** `api`.

### 2.4 Routing and travel times (free options)

**Option A — OSRM (Open Source Routing Machine), primary for road and walking**

- **Docs:** https://project-osrm.org · API: `/route/v1/{profile}/{lon,lat;lon,lat}` and `/table/v1/{profile}/...` (duration/distance matrix).
- **Access:** Free. The public demo server (`router.project-osrm.org`) is for light, fair-use testing only (mainly the car profile). For regular use, **self-host with Docker** on a GCC/UAE OSM extract from Geofabrik, which gives `car` and `foot` profiles with no limits.
- **Limitations:** No live traffic and no public transport. Apply a peak-hour multiplier to car durations (e.g. ×1.3–1.6 at rush hour, to be tuned against observed times).
- **Licence:** OSRM is BSD-2; the routing data is OSM (ODbL attribution).
- **Use in project:** taxi/ride-hail and walking legs; one batch `/table` call to build the neighborhood matrix.

**Option B — Public transport times**

- If Data.Dubai/RTA publishes a **GTFS** feed: compute metro/tram/bus times offline from `stop_times.txt` (station-to-station times + walk to station + transfer penalty). Optionally self-host **OpenTripPlanner** on the GTFS + OSM data for full transit routing.
- Otherwise: curate metro times between key stations manually from the RTA journey planner (S'hail) and store them in the matrix.

**Option C — OpenRouteService (optional)**

- https://openrouteservice.org — free API key with daily quotas; offers directions and a matrix endpoint for car and foot. A hosted alternative if self-hosting OSRM is not practical.

- **Use in project overall:** pre-compute a **neighborhood × neighborhood × mode** matrix once (≈ 15–20 neighborhoods) and store it locally → most runs need zero live routing calls. Live OSRM calls are made only for exact legs when enabled.
- **Source tag:** `api` when live, `verified`/`estimated` for the stored matrix (depending on how it was produced).

### 2.5 RTA — Roads & Transport Authority (official fares & transport)

- **URL:** https://www.rta.ae (fares, Nol card, metro/tram/bus/abra/taxi info, S'hail journey planner app)
- **Provides:** Metro/tram/bus zone-based fares by Nol card type (Red ticket, Silver, Gold, Blue), day passes, taxi tariffs (flagfall, per-km, minimum fare, airport and booking surcharges), marine transport (abra, water bus, ferry) fares, operating hours.
- **Access:** Published web pages / PDFs → curated manually into the `transit_fares` and `taxi_tariffs` tables. Some RTA datasets (stations, routes) are on Data.Dubai.
- **Planning reference (verify before use):**
  - Metro/bus fares are **zone-based**; Silver-card fares rise in steps for 1 zone / 2 zones / 3+ zones, and Gold class costs about double.
  - Taxis have a flagfall + per-km rate + a minimum fare, with higher flagfall for bookings and airport pickups; rates can differ by time of day.
  - The traditional **abra** across Dubai Creek costs a very small fixed fare — a good "local experience" for cultural itineraries.
- **Use in project:** the budget engine prices every `Leg` from these tables (taxi cost = tariff applied to the OSRM distance); logistics uses metro hours to rule out late-night metro legs.
- **Source tag:** `verified`.

### 2.6 Accommodation

**Option A — Curated hotel-area dataset (MVP, primary)**

- Build a `hotel_areas` table: neighborhood × tier (budget / mid / luxury) → nightly AED range, metro access, beach access, notes.
- **How to collect:** sample current listings on public booking sites and hotel websites for the target season, record min/median/max per area and tier, and date-stamp them. Use DET's hotel classification lists (Data.Dubai) to confirm which hotels/star levels exist per area.
- **Seasonality:** store a `season` column (peak ≈ Nov–Mar, shoulder, summer low) or a multiplier, because Dubai hotel prices swing a lot through the year.
- **Extra costs to include:** Dubai charges a per-room-per-night **Tourism Dirham** fee by hotel class, and hotel bills usually add service charge and VAT — model these as fixed add-ons in the budget engine (verify current rates).
- **Source tag:** `estimated` (ranges) with the sample date.

**Option B — Amadeus Hotel APIs (optional, test environment only)**

- **Docs:** https://developers.amadeus.com — Hotel List (by city/geocode) and Hotel Search (offers with prices).
- **Access:** OAuth2 client credentials; the free test environment returns cached/sample data. Production access is paid and **not used** under the cost policy.
- **Caveat:** check the current status of the Amadeus Self-Service programme before investing in it — availability and terms for new developers have changed over time. Keep the curated table as the source of truth.
- **Source tag:** `api`.

### 2.7 Activity and food costs

**Attractions (entry fees)**

- Collect from official venue sites and Visit Dubai: e.g. Al Shindagha Museum, Etihad Museum, Dubai Frame, Burj Khalifa "At the Top", Museum of the Future, Jumeirah Mosque tours (Sheikh Mohammed Centre for Cultural Understanding), Dubai Miracle Garden, desert safari price bands, Hatta activities.
- Record: adult price, child price, peak/off-peak, `price_source` (`verified` if from the official site), `as_of`.
- Mark **free** places explicitly (public beaches like Kite Beach / JBR / Al Mamzar (small fee), Al Fahidi Historical District, the souks, Dubai Creek walk, Al Seef) — these are key for budget alternatives.

**Food**

- Price bands per restaurant (AED per person per meal):
  - `street/cafeteria` (shawarma, karak, Indian/Pakistani cafeterias)
  - `casual` (Emirati and Levantine restaurants, food halls)
  - `mid` (sit-down restaurants in malls and hotels)
  - `fine`
- **How to collect:** restaurant menus/websites and a manual sample of 30–50 restaurants across the target neighborhoods; OSM `cuisine` tags help with categorisation.
- Store per-meal defaults per band (breakfast < lunch < dinner) for estimates when a specific restaurant has no data.
- **Source tag:** `verified` (from menus) or `estimated` (from bands).

### 2.8 Weather — Open-Meteo (optional)

- **Docs:** https://open-meteo.com/en/docs
- **Endpoints:** Forecast API (up to ~16 days ahead): `temperature_2m_max`, `apparent_temperature_max`, `precipitation_probability_max`, `uv_index_max`, `wind_speed_10m_max`; Historical/Climate APIs for seasonal averages beyond the forecast range.
- **Access:** Free with no API key for non-commercial use, with fair-use rate limits. Attribution required (CC BY 4.0 data).
- **Example:** `https://api.open-meteo.com/v1/forecast?latitude=25.2048&longitude=55.2708&daily=temperature_2m_max,apparent_temperature_max,uv_index_max,precipitation_probability_max&timezone=Asia/Dubai&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`
- **Use in project:** if a high > ~38 °C, move outdoor/beach slots to the morning or late afternoon and prefer indoor cultural sites at midday; add heat/UV notes. With no dates (or dates beyond the forecast range) → fall back to monthly climate averages.
- **Source tag:** `api` (forecast) / `estimated` (climate average).

### 2.9 Crowd levels (derived signal)

No free API gives "how crowded" a place is. We combine:

1. A **curated label** (`low|medium|high`) from editorial knowledge (e.g. Dubai Mall, Burj Khalifa, Global Village at weekends → high; Al Shindagha, Etihad Museum, Al Mamzar on weekdays → lower).
2. **Visitor / ridership statistics** from Data.Dubai where available (busy metro stations and major attractions → higher).
3. **Time-of-day/week rules:** weekends (Sat–Sun in the UAE), evenings and winter peak season raise the level.

The result is stored as `crowd_level` with `source=estimated`, which the Review agent uses for the "avoid crowds" check.

---

## 3. Data Integration Strategy

### 3.1 Phased approach

| Phase | What | Why |
|-------|------|-----|
| **1. Curated core** | Hand-built JSON → SQLite: ~15–20 neighborhoods, ~60 attractions, ~60 restaurants, hotel-area table, RTA fares, taxi tariffs, neighborhood travel matrix | Reliable, offline demo; covers the example request fully |
| **2. Free live data** | Overpass (POI discovery) + OSRM (road/walk legs) + Open-Meteo (weather) | Shows live data with no API keys or billing |
| **3. Transit enrichment** | RTA GTFS (if published) or OpenTripPlanner for metro/tram times | More accurate public-transport legs |
| **4. Optional** | Amadeus test env, Data.Dubai dataset refresh scripts | Extras |

### 3.2 Seed dataset build process

1. **Pick neighborhoods** covering culture, food, beach and budget hotels: Al Fahidi/Bur Dubai, Al Seef, Deira (Gold & Spice Souks), Al Rigga/Al Muraqqabat, Karama, Jumeirah 1–3, Umm Suqeim, Al Barsha, Dubai Marina/JBR, Downtown, Business Bay, Al Quoz (Alserkal Avenue), Al Mamzar, Hatta (day trip), Al Qudra (desert).
2. **Bootstrap POIs** with an Overpass export per neighborhood (coordinates, names, categories).
3. **Curate:** keep the relevant items; add tags, `crowd_level`, `typical_duration_min`, hours and prices from official sources, with `source_url` and `as_of`.
4. **Compute the travel matrix:** for each neighborhood pair and mode:
   - taxi and walk → one OSRM `/table` call per profile (taxi times × peak multiplier)
   - metro/transit → GTFS-derived or manually curated from the RTA journey planner
   - fallback heuristic (tagged `estimated`): taxi ≈ straight-line distance × 1.3 road factor ÷ ~35 km/h; transit ≈ walk to station + metro time + transfer penalty.
5. **Validate** every JSON file against the Pydantic schemas (`build_db.py`) and fail the build on missing provenance.
6. **Review dates:** a script lists records whose `as_of` is older than N months.

### 3.3 Provenance rules

| Tag | When | Example |
|-----|------|---------|
| `verified` | Value copied from an official source and dated | RTA 2-zone Silver fare; museum ticket price from the venue site |
| `api` | Returned by a live API call in this run | OSRM route duration; Open-Meteo forecast |
| `estimated` | Derived, range midpoint, heuristic or a stale curated value | Hotel nightly midpoint; heuristic taxi time; crowd level |

The UI shows a badge per value, and the budget screen splits the total into verified vs estimated parts.

### 3.4 Caching and fair use

- Runtime HTTP cache (Overpass ~7 days, OSRM ~7 days, weather ~3 hours).
- Matrix-first logistics so a typical run makes **0–10** live routing calls.
- Respect public-endpoint fair-use rules (small queries, rate limiting, generic User-Agent); prefer self-hosted OSRM/Overpass for heavier use. Log every external call in the trace.

### 3.5 What was collected (Phase 2, 2026-11-15)

Details, licences and refresh commands: [data/README.md](../data/README.md).

| Data | Source actually used | Result |
|------|----------------------|--------|
| Points of interest | One **Overpass** export per neighborhood (21 exports, ~3,300 elements) | 109 of 112 curated places matched to an OSM element (coordinates, OSM id); 25 opening-hours tags parsed |
| Attractions | Curated list (50) | Tags, crowd level, visit time, indoor flag; curated hours where OSM has none |
| Attraction prices | **Dubai Administrative Resolution No. 256 of 2024** (Etihad AED 25, Al Shindagha AED 50) | 2 `verified`; the rest `estimated` from press/reseller consensus or author estimates |
| Restaurants | Chosen from the OSM export (62), mostly local/regional | Prices per person from price band (`estimated`) |
| Metro, bus, tram fares | **RTA Nol fares page** | Silver/Blue AED 3 / 5 / 7.50, Red AED 4 / 6 / 8.50, Gold AED 6 / 10 / 15 (`verified`). No 1-day pass is listed; the shortest pass is 7 days |
| Taxi tariffs | **Dubai Taxi tariff page** | Flag-fall AED 5 (day) / 5.50 (night), AED 2.45/km, minimum AED 12 street / 13 app, waiting AED 0.50/min (`verified`); app booking fees and the AED 25 airport flag-fall from press (`estimated`) |
| Hotel areas | Author estimates of typical 2025-26 online-travel-agency ranges | 17 areas × tier, monthly multipliers (0.65 in summer – 1.35 in December), `estimated` |
| Hotel fees | 2014 Tourism Dirham schedule as reported in 2025-26 guides | AED 10 / 15 / 20 per room-night (budget / mid / luxury) + 10% service + 7% municipality + 5% VAT, `estimated` |
| Travel matrix | Heuristic (haversine × 1.35 road factor, speed bands; metro walk + wait + ride + transfer) | 287 entries (taxi, walk ≤ 2.5 km, metro, abra), `estimated` |
| Climate | **Open-Meteo historical archive (ERA5)**, daily 2015–2024 | Monthly normals: Jan max 24 °C … Aug max 41 °C, `verified` |

---

## 4. Legal and Ethical Considerations

- Follow each provider's terms: OSM ODbL attribution (Overpass, OSRM data), Open-Meteo CC BY 4.0 attribution and non-commercial free tier, public-endpoint fair-use policies.
- No scraping of booking sites; manual sampling for price ranges only, recorded as estimates.
- No personal data is stored; requests stay in the local session.
- The UI states that prices are estimates and not guaranteed, and that nothing is booked.

---

## 5. Open Questions / To Verify

- [ ] Which Data.Dubai datasets are currently available (and whether Dubai Pulse datasets have moved).
- [ ] Whether RTA publishes a GTFS feed for metro/tram/bus.
- [ ] Self-host OSRM (Docker) vs. public demo server / OpenRouteService free tier for the demo.
- [x] Current RTA fares (Nol zones, day pass) and taxi tariffs. Checked on rta.ae and dubaitaxi.ae, 2026-11-15 (§3.5).
- [x] Current Tourism Dirham rates and typical hotel service charge + VAT. Recorded from secondary sources (`estimated`); confirm on the DET site before a demo.
- [ ] Amadeus Self-Service availability for new projects.
- [x] Target travel season for the demo. Prices and climate are modelled per month, so any month works; the demo defaults to the cool season (Nov–Mar), when seasonal attractions are open.
- [ ] Confirm the remaining `estimated` attraction prices (Dubai Frame, Museum of the Future, At the Top, Jumeirah Mosque tour) on the official sites.
