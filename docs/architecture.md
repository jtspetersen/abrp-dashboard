# Architecture

A compact walkthrough of how the dashboard is put together. Written for someone who just cloned the repo and wants to find the right file before editing.

## Modules at a glance

```
parser.py           xlsx → cleaned waypoint DataFrame (no network, no state)
calculations.py     energy math + day grouping + totals + cost (pure functions)
enrichment.py       lat/lon + route + elevation (network + @st.cache_data)
elevation_grid.py   local .npz lookup via scipy KDTree
settings.py         constants (battery range, rate range, sheet name)
app.py              Streamlit UI (sidebar + placeholders + charts + status)
scripts/build_elevation_grid.py    one-off .npz builder (not runtime)
```

Each module owns one concern. No circular imports. `settings.py` is the only module imported by everything else; it has no imports of its own.

## Dataflow for one uploaded trip

```
┌─────────────────┐
│ .xlsx upload    │
└────────┬────────┘
         │
         ▼
┌─────────────────────────┐
│ parser.parse_abrp       │   raw sheet → 11-column DataFrame
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│ calculations.enrich             │   + kwh_consumed_leg, kwh_added,
│   (battery_kwh, rate_usd_per_   │     day, cost_per_stop
│    kwh)                          │
└────────┬────────────────────────┘
         │
         │   (day-range filter here in app.py)
         ▼
┌─────────────────────────┐
│ view                    │   filtered DataFrame feeds charts + enrichment
└────────┬────────────────┘
         │
         ▼
┌────────────────────────────────────────────────────┐
│ enrichment.geocode_waypoints   [@st.cache_data]    │
│                                                    │
│   Pass 1: _apply_supercharger_pass (local JSON)    │
│   Pass 2: _apply_photon_pass      (Photon HTTP)    │
│   Pass 3: _apply_geometric_pass   (pure math)      │
│                                                    │
│   → + lat, lon, elev_m_sidecar, geocode_tier,      │
│       geocode_note, geocode_ok                     │
└────────┬───────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────────┐
│ enrichment.build_route         [@st.cache_data]    │
│                                                    │
│   Per leg: _osrm_route_segment_cached (OSRM HTTP)  │
│   Per leg: record failure details if any           │
│   Stitch legs into continuous route_df             │
│   elevation_grid.lookup_m_batch on every vertex    │
│   Build marker_df for waypoint UI pins             │
│                                                    │
│   → (route_df, marker_df, stats)                   │
└────────┬───────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────┐
│ app.py backfills        │   map placeholder + elevation placeholder
│ pydeck map              │   render into st.empty() slots reserved
│ Plotly elevation chart  │   near the top of the page
│ per-leg bar subplots    │
│ summary cards           │
└─────────────────────────┘
```

## Geocoding — 3-tier dispatch

```python
def resolve(name, neighbors):
    if is_tesla_supercharger(name):
        return supercharger_dataset.lookup(name)     # Tier 1 — authoritative
    coord = photon.search(name, bbox=neighbor_bbox)  # Tier 2 — single call
    if coord and inside(coord, bbox):
        return coord
    return geometric_estimate(neighbors)             # Tier 3 — math
```

Each tier is independent. No retry loops, no fallback chains within a tier. A waypoint takes exactly one path. Anything that fails all three is marked `geocode_tier == "failed"` — the downstream map/elevation/routing simply skip those rows instead of crashing.

The three tiers live in three small helper functions in `enrichment.py`:

- `_apply_supercharger_pass(out)` — exact name / city+state match against `data/superchargers.json`.
- `_apply_photon_pass(out)` — one GET to Photon (komoot, OSM-backed, keyless) per still-unresolved row, with a neighbor-derived bounding box when two Tier-1 neighbors are already placed.
- `_apply_geometric_pass(out, src)` — for any waypoint whose two neighbors resolved but it didn't, place it at `planned_mi` from the closer neighbor along the line to the farther one.

## Routing — OSRM

One GET per leg to `https://router.project-osrm.org/route/v1/driving/{coords}`. Returns a GeoJSON LineString. OSRM is keyless and has no per-minute rate limit (fair-use only). Inner function is `_osrm_route_segment_cached`; outer wrapper catches `RoutingError` and records structured failure info per leg so partial success (e.g. 44/46 legs) still renders what it can.

## Elevation — local grid

OSRM doesn't return elevation. Instead, `data/us_elevation_grid.npz` (bundled, 1.8 MB, 1.4M cells at 2-mile spacing) is loaded once per Streamlit session via `@st.cache_resource`. A scipy `cKDTree` answers nearest-neighbor queries in microseconds. `build_route` vectorizes all ~50k route vertices through `lookup_m_batch` in one call.

The grid is built once by `scripts/build_elevation_grid.py` against `open-elevation.com`. It's not a runtime dependency — the committed `.npz` is the source of truth.

## Caching layers

```
@st.cache_resource         long-lived singletons (KDTree, supercharger indexes)
@st.cache_data             outer pipeline results keyed on DataFrame content
@st.cache_data             inner HTTP calls keyed on their exact arguments
```

Warm-cache reruns (e.g. changing the basemap style) are instant: `geocode_waypoints` and `build_route` return their cached DataFrames without iterating, and no inner HTTP call fires.

Exceptions are deliberately **not** cached — a transient 5xx from OSRM or Photon auto-retries on the next rerun instead of sticking as a permanent failure.

## Error handling

Single exception hierarchy in `enrichment.py`:

- `EnrichmentError` — base class
- `GeocodeError` — Photon failure (no results, network error, malformed response)
- `RoutingError` — OSRM failure (non-Ok code, empty geometry, network error)

Native exceptions from `requests`, `json`, etc. are chained via `raise ... from e` so the original stack trace is still reachable on `e.__cause__`. `app.py` catches `EnrichmentError` at the two top-level sites (geocoding, routing) and surfaces a friendly banner via `st.error`.

## UI render order (app.py)

The page reads top→bottom in this order:

1. Sidebar (battery, charge rate, upload, day-range filter)
2. Summary cards (6 metrics)
3. **Trip map placeholder** (st.empty)
4. **Elevation profile placeholder** (st.empty)
5. Per-leg bars (renders immediately from `view`)
6. Geocoding status panel (runs + banner)
7. Routing status panel (runs + banner)
8. Map + elevation placeholders **backfilled** with actual content
9. Raw table (collapsible)

Step 8 is the key trick — the map and elevation chart visually appear at positions 3/4 but the data they need isn't available until after steps 6/7 run. Streamlit's `st.empty()` reserves a slot that can be filled later in the same script execution, so the page reads naturally while respecting the dataflow.
