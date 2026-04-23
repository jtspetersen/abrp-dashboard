# ABRP Trip Dashboard

Turn an A Better Route Planner (ABRP) `.xlsx` trip export into an interactive Streamlit dashboard — map, elevation profile, per-leg charts, cost estimate, and day-range filtering. Built for visualizing long EV road trips.

## What it does

- **Parses** the ABRP `.xlsx` export — waypoints, SoC arrivals/departures, charging durations, planned distances, overnight markers.
- **Computes** per-leg kWh consumed, kWh added at each charging stop, day grouping (including multi-night stays), total cost estimate, $/mile.
- **Resolves** every waypoint's location via a flat three-tier dispatch: a bundled Tesla Supercharger dataset (authoritative), Photon (OSM-backed geocoder), or a geometric estimate from neighbors' planned distances.
- **Routes** every leg via OSRM (keyless public demo server).
- **Looks up** elevation along the route from a local 2-mile grid built from Copernicus GLO-90 DEM data.
- **Renders** a pydeck map (color-coded by arrival SoC, switchable basemap), Plotly elevation profile, per-leg bar charts, and summary cards — all filterable by day range.

## Running

```bash
pip install -r requirements.txt
python -m streamlit run app.py
```

Open the link Streamlit prints, then upload an ABRP `.xlsx` export via the sidebar.

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite (89 tests, ~1 second) is offline-only — no live Photon or OSRM calls. It covers the parser, calculations, enrichment helpers (haversine, geometric estimate, Supercharger matcher, exception hierarchy), and the elevation grid lookup (against a synthetic fixture, so no `.npz` is needed to run the tests).

## How it works

**Geocoding — three-tier dispatch.** Every waypoint follows one of three paths, no retry chains. Tesla Supercharger names match against a bundled snapshot from supercharge.info (authoritative, ~3,000 US stations, no network call). Non-Supercharger waypoints hit Photon once with a neighbor-derived bounding box. Anything still unresolved gets a geometric estimate from the planned distances to its two neighbors. A waypoint that falls through all three is marked `failed` and excluded from downstream routing.

**Routing and elevation.** Each leg between two resolved waypoints is one GET to OSRM's public demo server (no API key, no per-minute rate limit). The response returns a GeoJSON LineString of (lon, lat) vertices but no elevation — elevations come from a pre-built local grid (`data/us_elevation_grid.npz`, 1.8 MB, 1.4M cells at 2-mile spacing). A KD-tree nearest-neighbor lookup assigns an elevation to every route vertex and waypoint marker in one vectorized batch.

**Caching layers.** `@st.cache_data` wraps the outer pipeline (`geocode_waypoints`, `build_route`) so flipping a basemap or changing the date filter doesn't re-run the whole chain. Inner per-request caches on Photon and OSRM mean that individual waypoint / leg lookups are instant on the second session. Exceptions are never cached — a transient 5xx from OSRM auto-retries on the next rerun.

## Rebuilding the elevation grid

The bundled grid (2-mile spacing, ~45 min build) covers the lower 48 with a ~50 mi buffer. To change resolution or refresh against the current Copernicus data:

```bash
python scripts/build_elevation_grid.py --spacing 2   # default
python scripts/build_elevation_grid.py --spacing 10  # faster build, coarser
```

The builder checkpoints every 25 batches, so a 504 from the upstream API mid-build doesn't cost progress.

## Known limitations

- **No satellite basemap.** Carto (our keyless basemap provider) doesn't serve satellite tiles. Adding satellite would require a Mapbox token or a TileLayer pydeck upgrade — both excluded to keep runtime keyless.
- **No per-station Supercharger pricing.** No free-tier source publishes reliable, parseable per-station rates. The dashboard uses a single user-adjustable $/kWh rate in the sidebar. See the plan file for the full rationale.
- **Elevation grid smooths mountain passes.** At 2-mile spacing, a ridge-line peak can be clipped by ~200 ft. Rebuild at higher resolution if you care about sub-200-ft fidelity.
- **US only.** The elevation grid and the Supercharger sidecar are lower-48 only. International trips will fall through to Photon + geometric estimates with the grid returning nearest-edge values (usually wrong).

## Data sources + credits

Runtime:
- [**supercharge.info**](https://supercharge.info) — Tesla Supercharger locations dataset, bundled snapshot.
- [**Photon**](https://photon.komoot.io) by [komoot](https://www.komoot.com) — OSM-backed geocoder, keyless fair-use.
- [**OSRM**](http://project-osrm.org/) — Open Source Routing Machine public demo server.
- [**open-elevation.com**](https://open-elevation.com) + [**Copernicus GLO-90 DEM**](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM) — elevation source used by the grid builder.

Libraries: [Streamlit](https://streamlit.io), [pandas](https://pandas.pydata.org), [pydeck](https://pydeck.gl) (deck.gl), [Plotly](https://plotly.com/python/), [SciPy](https://scipy.org) (KD-tree).

## License

MIT — see [LICENSE](LICENSE).
