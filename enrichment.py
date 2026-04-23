"""Waypoint coordinate resolution + OSRM route fetching + elevation lookup.

Coordinate resolution is a **flat three-tier dispatch**:

    1. Tesla Supercharger sidecar  (local dataset, authoritative, no API)
    2. Photon geocoder              (OSM-backed, single call with optional bbox)
    3. Geometric estimate           (math from neighbors' planned distances)

A waypoint takes *exactly one* of these paths. There is no layered retry
inside a tier — Photon gets one shot per waypoint; if it misses or lands
outside the neighbor-bbox, we fall through to the geometric estimate.
That's strictly better than parking the waypoint at a known-bad coord.

Route fetching uses the **OSRM public demo server** (keyless, no per-minute
rate limit) — each leg is one GET that returns a GeoJSON LineString of
[lon, lat] vertices. OSRM does not include elevation; vertices are
enriched in `build_route` via `elevation_grid.lookup_m_batch`, which
reads a committed local `.npz` grid built by
`scripts/build_elevation_grid.py`. Supercharger waypoints additionally
carry authoritative `elev_m_sidecar` from the sidecar and use that value for
their map/elevation markers.
"""

from __future__ import annotations

import json
import re
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

# ----- Error types -----
#
# Every failure in this module surfaces as an EnrichmentError (or a
# subclass). The native exceptions from `requests`, `json`, etc. are
# chained via `raise ... from exc` so the original traceback is still
# reachable for debugging.


class EnrichmentError(Exception):
    """Any failure during geocoding, routing, or elevation enrichment."""


class GeocodeError(EnrichmentError):
    """A waypoint couldn't be resolved to lat/lon via Photon."""


class RoutingError(EnrichmentError):
    """A leg couldn't be routed via OSRM."""


# ----- Endpoints + timeouts -----

PHOTON_URL = "https://photon.komoot.io/api/"
# OSRM's public demo server. No API key, no per-minute rate limit (fair-use
# only). Same GeoJSON LineString output as ORS but without elevation — we
# fill elevation in locally from elevation_grid.py instead.
OSRM_BASE_URL = "https://router.project-osrm.org/route/v1/driving"

REQUEST_TIMEOUT_GEOCODE = 20
REQUEST_TIMEOUT_ROUTE = 30

# Cache-buster; bump when the OSRM request shape changes.
_ROUTING_CACHE_VERSION = 3

# ----- Geometry helpers -----

EARTH_RADIUS_MI = 3958.7613
MI_PER_DEG_LAT = 69.0


def _haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two (lat, lon) points."""
    lat1r, lat2r = radians(lat1), radians(lat2)
    dlat = lat2r - lat1r
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(lat1r) * cos(lat2r) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MI * asin(sqrt(a))


def _mi_per_deg_lon(lat: float) -> float:
    """Miles per degree of longitude at a given latitude."""
    return max(1.0, MI_PER_DEG_LAT * cos(radians(lat)))


def _offset_toward(
    from_lat: float,
    from_lon: float,
    toward_lat: float,
    toward_lon: float,
    distance_mi: float,
) -> tuple[float, float] | None:
    """Return a point `distance_mi` miles from `from` along the line to `toward`."""
    dlat_mi = (toward_lat - from_lat) * MI_PER_DEG_LAT
    dlon_mi = (toward_lon - from_lon) * _mi_per_deg_lon(from_lat)
    total = sqrt(dlat_mi * dlat_mi + dlon_mi * dlon_mi)
    if total < 0.01:
        return None
    unit_lat = dlat_mi / total
    unit_lon = dlon_mi / total
    est_lat = from_lat + (unit_lat * distance_mi) / MI_PER_DEG_LAT
    est_lon = from_lon + (unit_lon * distance_mi) / _mi_per_deg_lon(from_lat)
    return (est_lat, est_lon)


def _geometric_estimate(
    prev_lat: float,
    prev_lon: float,
    next_lat: float,
    next_lon: float,
    planned_prev_mi: float,
    planned_next_mi: float,
) -> tuple[float, float] | None:
    """Estimate a waypoint's location from its neighbors' planned miles.

    Offsets from whichever neighbor is closer (shorter planned leg) toward
    the farther one by that shorter planned distance. Assumes rough
    collinearity of prev/this/next, which holds well for Superchargers
    and hotels along interstates. For a short planned next (e.g. a 6-mi
    hop from a Supercharger to an adjacent hotel) the estimate lands
    within a few miles of the real location — safely inside ORS's 5 km
    snap radius.
    """
    if planned_prev_mi <= planned_next_mi:
        return _offset_toward(prev_lat, prev_lon, next_lat, next_lon, planned_prev_mi)
    return _offset_toward(next_lat, next_lon, prev_lat, prev_lon, planned_next_mi)


def _neighbor_bbox(
    prev_lat: float,
    prev_lon: float,
    next_lat: float,
    next_lon: float,
    planned_prev_mi: float,
    planned_next_mi: float,
    slack: float = 1.1,
) -> tuple[float, float, float, float] | None:
    """Axis-aligned bbox that must contain the waypoint.

    Intersection of two disks: the waypoint is within planned_prev_mi
    (plus slack) of prev, and within planned_next_mi of next. Haversine
    ≤ driving distance, so slack=1.1 covers rounding noise on short
    legs. Used to constrain Photon's search to candidate results in the
    right region of the US.

    Returns (min_lat, min_lon, max_lat, max_lon), or None if the disks
    don't overlap (neighbors further apart than the planned distances
    allow — rare; happens if one neighbor is itself mis-placed).
    """
    r_prev = planned_prev_mi * slack
    r_next = planned_next_mi * slack

    d1_lat_span = r_prev / MI_PER_DEG_LAT
    d1_lon_span = r_prev / _mi_per_deg_lon(prev_lat)
    d2_lat_span = r_next / MI_PER_DEG_LAT
    d2_lon_span = r_next / _mi_per_deg_lon(next_lat)

    lat_min = max(prev_lat - d1_lat_span, next_lat - d2_lat_span)
    lat_max = min(prev_lat + d1_lat_span, next_lat + d2_lat_span)
    lon_min = max(prev_lon - d1_lon_span, next_lon - d2_lon_span)
    lon_max = min(prev_lon + d1_lon_span, next_lon + d2_lon_span)

    if lat_min >= lat_max or lon_min >= lon_max:
        return None
    return (lat_min, lon_min, lat_max, lon_max)


def _in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    return bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]


# ----- Tier 1: Tesla Supercharger dataset -----

_TESLA_PREFIX_RE = re.compile(r"^\s*Tesla Supercharger\s+", re.IGNORECASE)
_CITY_STATE_RE = re.compile(r"^\s*(.+?),\s*([A-Za-z]{2})\b")


@st.cache_resource(show_spinner=False)
def _load_supercharger_indexes() -> tuple[dict, dict]:
    """Load data/superchargers.json and build lookup indexes.

    Returns (by_name, by_city_state):
      - by_name: normalized "City, ST" or "City, ST - Street" -> station dict
      - by_city_state: (normalized_city, state) -> list of matching stations

    by_name handles the common case including disambiguated names like
    "Truckee, CA - Deerfield Dr". by_city_state is a fallback for a city
    with exactly one charger where the stripped input is just "City, ST".
    """
    path = Path(__file__).parent / "data" / "superchargers.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    by_name: dict[str, dict] = {}
    by_city_state: dict[tuple[str, str], list[dict]] = {}
    for station in data.get("stations", []):
        name = (station.get("name") or "").strip()
        if not name:
            continue
        by_name[name.lower()] = station
        city = (station.get("city") or "").strip().lower()
        state = (station.get("state") or "").strip().upper()
        if city and state:
            by_city_state.setdefault((city, state), []).append(station)
    return by_name, by_city_state


def _match_supercharger(waypoint_clean: str) -> dict | None:
    """Return the Supercharger station record for a Tesla waypoint, or None.

    Matches `/^Tesla Supercharger /i` at the start. Exact-name match is
    tried first so a string like "Tesla Supercharger Truckee, CA -
    Deerfield Dr" resolves deterministically even when the city has
    multiple chargers. If no exact match and the city has exactly one
    charger in the dataset, that charger is returned. Ambiguous cases
    (multiple chargers in a city, input without street disambiguator)
    fall through to None — Tier 2 (Photon) handles them instead.
    """
    if not waypoint_clean:
        return None
    m = _TESLA_PREFIX_RE.match(waypoint_clean)
    if not m:
        return None
    stripped = waypoint_clean[m.end() :].strip()
    if not stripped:
        return None

    by_name, by_city_state = _load_supercharger_indexes()

    hit = by_name.get(stripped.lower())
    if hit is not None:
        return hit

    cs = _CITY_STATE_RE.match(stripped)
    if cs:
        city = cs.group(1).strip().lower()
        state = cs.group(2).upper()
        candidates = by_city_state.get((city, state), [])
        if len(candidates) == 1:
            return candidates[0]
    return None


# ----- Tier 2: Photon geocoder -----

# Trailing bits that Photon chokes on if present — ZIPs and country names
# in particular make it return zero results even for well-known streets.
# Strip only at the tail so we don't accidentally cut real waypoint data.
_ZIP_TAIL_RE = re.compile(r",?\s*\d{5}(?:-\d{4})?\s*$")
_COUNTRY_TAIL_RE = re.compile(r",?\s*(?:united states|usa|u\.s\.a\.|u\.s\.)\s*$", re.IGNORECASE)


def _normalize_for_photon(text: str) -> str:
    """Strip ZIP codes and trailing country tokens that break Photon.

    ABRP exports often include the full postal address (e.g. "6103
    Majestic Ave, Oakland, CA 94605-1861, United States"). Photon's
    matcher weighs every token, and the ZIP/country tail makes it
    return zero results for otherwise-common addresses. Removing them
    from the tail is harmless for matching and fixes real failures.
    """
    s = (text or "").strip()
    # Apply each trimmer up to twice in case both are present at the tail.
    for _ in range(2):
        s = _COUNTRY_TAIL_RE.sub("", s).strip()
        s = _ZIP_TAIL_RE.sub("", s).strip()
    return s.strip(", ")


@st.cache_data(show_spinner=False)
def _photon_search_cached(
    text: str,
    bbox_west: float | None,
    bbox_south: float | None,
    bbox_east: float | None,
    bbox_north: float | None,
) -> tuple[float, float]:
    """Raw Photon call. Returns (lat, lon) or raises GeocodeError.

    The raise is load-bearing: st.cache_data only caches successful returns,
    so a GeocodeError (or any other unhandled exception) auto-retries on
    the next Streamlit rerun rather than sticking as a cached miss.
    """
    params: dict = {"q": text, "limit": 1, "lang": "en"}
    if None not in (bbox_west, bbox_south, bbox_east, bbox_north):
        params["bbox"] = f"{bbox_west},{bbox_south},{bbox_east},{bbox_north}"
    try:
        resp = requests.get(
            PHOTON_URL,
            params=params,
            timeout=REQUEST_TIMEOUT_GEOCODE,
            headers={"User-Agent": "abrp-dashboard/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise GeocodeError(f"Photon request failed for {text!r}: {e}") from e
    except ValueError as e:  # json decode
        raise GeocodeError(f"Photon returned non-JSON for {text!r}: {e}") from e
    features = data.get("features") or []
    if not features:
        raise GeocodeError(f"no Photon results for {text!r}")
    coords = features[0].get("geometry", {}).get("coordinates") or []
    if len(coords) < 2:
        raise GeocodeError(f"malformed Photon coordinates for {text!r}: {coords!r}")
    # Photon returns [lon, lat]; flip.
    return (float(coords[1]), float(coords[0]))


# ----- Orchestration -----


def _compute_bbox_for_waypoint(
    df: pd.DataFrame, i: int
) -> tuple[float, float, float, float] | None:
    """Neighbor-bbox for waypoint i, if both neighbors are already resolved."""
    n = len(df)
    if i <= 0 or i >= n - 1:
        return None
    prev = df.iloc[i - 1]
    nxt = df.iloc[i + 1]
    if pd.isna(prev.get("lat")) or pd.isna(nxt.get("lat")):
        return None
    try:
        planned_prev = float(df.iloc[i - 1].get("distance_mi") or 0.0)
        planned_next = float(df.iloc[i].get("distance_mi") or 0.0)
    except Exception:
        return None
    if planned_prev <= 0 or planned_next <= 0:
        return None
    return _neighbor_bbox(
        float(prev["lat"]),
        float(prev["lon"]),
        float(nxt["lat"]),
        float(nxt["lon"]),
        planned_prev,
        planned_next,
    )


def _apply_supercharger_pass(out: pd.DataFrame) -> None:
    """Tier 1 — fill in any waypoint that matches the Supercharger sidecar.

    Order-independent: each waypoint looks up its own row against the
    bundled dataset. Writes `lat`, `lon`, `elev_m_sidecar`, `geocode_tier`,
    `geocode_note` on hit. Non-Supercharger rows are left untouched.
    """
    for i in range(len(out)):
        address = str(out.at[i, "waypoint_clean"] or "")
        station = _match_supercharger(address)
        if station is None:
            continue
        out.at[i, "lat"] = float(station["lat"])
        out.at[i, "lon"] = float(station["lon"])
        if station.get("elev_m") is not None:
            out.at[i, "elev_m_sidecar"] = float(station["elev_m"])
        out.at[i, "geocode_tier"] = "supercharger"
        out.at[i, "geocode_note"] = f"supercharger dataset ({station.get('name', '')})"


def _apply_photon_pass(out: pd.DataFrame) -> None:
    """Tier 2 — resolve any still-unresolved waypoint via Photon.

    One HTTP shot per waypoint — no stripping variants, no focus+bbox
    layering. A neighbor-derived bbox (from Tier 1 neighbors already
    placed) constrains the search when available. GeocodeError silently
    falls through to Tier 3; a Photon result outside the expected bbox
    is rejected with a note and also falls through.
    """
    for i in range(len(out)):
        if out.at[i, "geocode_tier"] is not None:
            continue
        address = _normalize_for_photon(str(out.at[i, "waypoint_clean"] or ""))
        if not address:
            continue
        bbox = _compute_bbox_for_waypoint(out, i)
        try:
            if bbox is not None:
                # Photon's bbox param is west,south,east,north — flip our
                # internal (lat_min, lon_min, lat_max, lon_max) convention.
                lat, lon = _photon_search_cached(address, bbox[1], bbox[0], bbox[3], bbox[2])
            else:
                lat, lon = _photon_search_cached(address, None, None, None, None)
        except GeocodeError:
            continue
        if bbox is not None and not _in_bbox(lat, lon, bbox):
            out.at[i, "geocode_note"] = "photon result outside neighbor bbox"
            continue
        out.at[i, "lat"] = lat
        out.at[i, "lon"] = lon
        out.at[i, "geocode_tier"] = "photon"
        out.at[i, "geocode_note"] = "photon" + (" (bbox)" if bbox is not None else "")


def _apply_geometric_pass(out: pd.DataFrame, src: pd.DataFrame) -> None:
    """Tier 3 — place the waypoint on the line between its two neighbors.

    Needs both neighbors resolved and both planned distances > 0. Edge
    waypoints (first/last rows) can't be placed this way and get marked
    'failed'. Everything else that has been neither Supercharger- nor
    Photon-resolved should succeed here by construction — the geometric
    estimate is always defined when its inputs are.
    """
    n = len(out)
    for i in range(n):
        if out.at[i, "geocode_tier"] is not None:
            continue
        if i <= 0 or i >= n - 1:
            out.at[i, "geocode_tier"] = "failed"
            if not out.at[i, "geocode_note"]:
                out.at[i, "geocode_note"] = "edge waypoint — no both-sides neighbors"
            continue
        prev = out.iloc[i - 1]
        nxt = out.iloc[i + 1]
        if pd.isna(prev.get("lat")) or pd.isna(nxt.get("lat")):
            out.at[i, "geocode_tier"] = "failed"
            if not out.at[i, "geocode_note"]:
                out.at[i, "geocode_note"] = "neighbor unresolved — can't estimate"
            continue
        try:
            planned_prev = float(src.iloc[i - 1].get("distance_mi") or 0.0)
            planned_next = float(src.iloc[i].get("distance_mi") or 0.0)
        except Exception:
            planned_prev = planned_next = 0.0
        if planned_prev <= 0 or planned_next <= 0:
            out.at[i, "geocode_tier"] = "failed"
            if not out.at[i, "geocode_note"]:
                out.at[i, "geocode_note"] = "missing planned distances — can't estimate"
            continue
        geom = _geometric_estimate(
            float(prev["lat"]),
            float(prev["lon"]),
            float(nxt["lat"]),
            float(nxt["lon"]),
            planned_prev,
            planned_next,
        )
        if geom is None:
            out.at[i, "geocode_tier"] = "failed"
            if not out.at[i, "geocode_note"]:
                out.at[i, "geocode_note"] = "geometric estimate degenerate"
            continue
        out.at[i, "lat"] = float(geom[0])
        out.at[i, "lon"] = float(geom[1])
        out.at[i, "geocode_tier"] = "geometric"
        out.at[i, "geocode_note"] = (
            f"geometric estimate ({min(planned_prev, planned_next):.0f} mi from closer neighbor)"
        )


@st.cache_data(show_spinner=False)
def geocode_waypoints(df: pd.DataFrame) -> pd.DataFrame:
    """Resolve (lat, lon) for every waypoint via the 3-tier dispatch.

    Each waypoint takes exactly one of three paths:
        1. Supercharger sidecar  — local dataset, authoritative, no API
        2. Photon                — OSM-backed HTTP call, optional bbox
        3. Geometric estimate    — math from neighbors' planned distances

    Passes run in order so Tier 2 can see Tier 1's placements for its
    bbox, and Tier 3 can see both. A waypoint that falls through all
    three gets `geocode_tier == "failed"`.

    Output columns added:
        lat, lon, elev_m_sidecar, geocode_tier, geocode_note, geocode_ok.

    Cached with st.cache_data — MUST NOT call any Streamlit element that
    references widgets created outside of it (cache_data replays element
    calls on hit). Per-waypoint progress feedback lives at the call site
    via st.spinner instead.
    """
    n = len(df)
    out = df.copy().reset_index(drop=True)
    out["lat"] = pd.Series([pd.NA] * n, dtype="object")
    out["lon"] = pd.Series([pd.NA] * n, dtype="object")
    out["elev_m_sidecar"] = pd.Series([pd.NA] * n, dtype="object")
    out["geocode_tier"] = pd.Series([None] * n, dtype="object")
    out["geocode_note"] = ""

    _apply_supercharger_pass(out)
    _apply_photon_pass(out)
    _apply_geometric_pass(out, df)

    out["geocode_ok"] = (
        out["geocode_tier"].astype("object").isin({"supercharger", "photon", "geometric"})
    )
    # Normalize lat/lon back to float dtype for downstream consumers.
    out["lat"] = pd.to_numeric(out["lat"], errors="coerce")
    out["lon"] = pd.to_numeric(out["lon"], errors="coerce")
    return out


# ----- OSRM routing -----
#
# Keyless, no per-minute rate limit (fair-use). Returns the route as a
# GeoJSON LineString of 2-tuples (lon, lat) — elevation is filled in
# afterward by elevation_grid.lookup_m_batch so we don't depend on the
# router for that.


@st.cache_data(show_spinner=False)
def _osrm_route_segment_cached(
    lon1: float,
    lat1: float,
    lon2: float,
    lat2: float,
    _version: int = _ROUTING_CACHE_VERSION,
) -> list[list[float]]:
    """Single leg from OSRM. Returns [[lon, lat], ...] or raises RoutingError.

    No API key needed. Cached on the coord quartet; exceptions aren't
    cached so transient network failures auto-retry on the next
    Streamlit rerun without forcing the user to clear cache.
    """
    coords = f"{lon1:.6f},{lat1:.6f};{lon2:.6f},{lat2:.6f}"
    url = f"{OSRM_BASE_URL}/{coords}?overview=full&geometries=geojson&radiuses=unlimited;unlimited"
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT_ROUTE,
            headers={"User-Agent": "abrp-dashboard/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise RoutingError(f"OSRM request failed for leg {coords}: {e}") from e
    except ValueError as e:  # json decode
        raise RoutingError(f"OSRM returned non-JSON for leg {coords}: {e}") from e
    if data.get("code") != "Ok":
        raise RoutingError(f"OSRM returned code={data.get('code')!r}: {data.get('message', '')}")
    routes = data.get("routes") or []
    if not routes:
        raise RoutingError("OSRM returned no routes")
    geometry = routes[0].get("geometry") or {}
    coords_list = geometry.get("coordinates") or []
    out: list[list[float]] = []
    for c in coords_list:
        if not c or len(c) < 2:
            continue
        out.append([float(c[0]), float(c[1])])  # [lon, lat]
    if not out:
        raise RoutingError("OSRM returned empty geometry")
    return out


@st.cache_data(show_spinner=False)
def build_route(geocoded: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Fetch each leg's route from OSRM, fill elevation from the local grid.

    Returns (route_df, marker_df, stats) — same shape as the previous ORS
    implementation so nothing downstream changes. route_df's elev_m comes
    from elevation_grid.lookup_m_batch (Copernicus GLO-90 DEM via a
    committed local file); OSRM itself returns no elevation data.
    """
    # Deferred import to avoid loading scipy + the grid file when this
    # module is imported by code paths that don't route.
    from elevation_grid import is_available as _grid_available
    from elevation_grid import lookup_m_batch as _grid_lookup_batch

    geo = geocoded.reset_index(drop=True)
    n = len(geo)
    n_legs = max(n - 1, 0)

    leg_points: dict[int, list[list[float]]] = {}
    failures: list[dict] = []
    for i in range(n_legs):
        a = geo.iloc[i]
        b = geo.iloc[i + 1]

        if not bool(a["geocode_ok"]) or not bool(b["geocode_ok"]):
            which = []
            if not bool(a["geocode_ok"]):
                which.append("start")
            if not bool(b["geocode_ok"]):
                which.append("end")
            failures.append(
                {
                    "leg_idx": i,
                    "from": a["waypoint_clean"],
                    "to": b["waypoint_clean"],
                    "reason": f"{'/'.join(which)} not geocoded",
                }
            )
            continue

        try:
            seg = _osrm_route_segment_cached(
                float(a["lon"]),
                float(a["lat"]),
                float(b["lon"]),
                float(b["lat"]),
            )
            leg_points[i] = seg
        except RoutingError as e:
            # Surface the underlying HTTP/JSON message where available —
            # the native cause is attached via `raise ... from`, and for
            # HTTPErrors the response body lives on e.__cause__.response.
            reason = str(e)
            cause = e.__cause__
            resp = getattr(cause, "response", None) if cause is not None else None
            if resp is not None:
                body_snippet = (resp.text or "")[:200].replace("\n", " ")
                reason = f"HTTP {resp.status_code} · {body_snippet}"
            failures.append(
                {
                    "leg_idx": i,
                    "from": a["waypoint_clean"],
                    "to": b["waypoint_clean"],
                    "reason": reason,
                }
            )
    legs_ok = len(leg_points)
    legs_failed = len(failures)

    # Flatten route points with cumulative haversine miles. Skip the
    # duplicated first vertex of each subsequent leg so leg-boundary
    # waypoints aren't double-counted.
    rows: list[dict] = []
    cum_mi = 0.0
    last_pt: tuple[float, float] | None = None
    for leg_idx in sorted(leg_points.keys()):
        seg = leg_points[leg_idx]
        for j, pt in enumerate(seg):
            lon, lat = pt[0], pt[1]
            if last_pt is not None and j == 0:
                last_pt = (lon, lat)
                continue
            if last_pt is not None:
                cum_mi += _haversine_mi(last_pt[1], last_pt[0], lat, lon)
            rows.append(
                {
                    "lon": lon,
                    "lat": lat,
                    "elev_m": 0.0,  # filled in below by the elevation grid
                    "cum_mi": cum_mi,
                    "leg_idx": leg_idx,
                }
            )
            last_pt = (lon, lat)

    route_df = (
        pd.DataFrame(rows)
        if rows
        else pd.DataFrame(columns=["lon", "lat", "elev_m", "cum_mi", "leg_idx"])
    )

    # Batch-enrich elevation from the local grid. One KDTree query per vertex.
    grid_used = False
    if len(route_df) and _grid_available():
        import numpy as _np

        elev_m = _grid_lookup_batch(
            _np.asarray(route_df["lat"], dtype=_np.float64),
            _np.asarray(route_df["lon"], dtype=_np.float64),
        )
        if elev_m is not None:
            route_df["elev_m"] = elev_m
            grid_used = True

    # Attach cum_mi and elev_m to each waypoint marker by reading the
    # matching route vertex (end of prev leg preferred, since that's the
    # real shared waypoint rather than the slightly-past-start of the
    # next leg post-dedup).
    first_by_leg: dict[int, tuple[float, float]] = {}
    last_by_leg: dict[int, tuple[float, float]] = {}
    if len(route_df):
        for leg_idx, sub in route_df.groupby("leg_idx"):
            first_by_leg[leg_idx] = (
                float(sub["cum_mi"].iloc[0]),
                float(sub["elev_m"].iloc[0]),
            )
            last_by_leg[leg_idx] = (
                float(sub["cum_mi"].iloc[-1]),
                float(sub["elev_m"].iloc[-1]),
            )

    markers = []
    for i in range(n):
        row = geo.iloc[i]
        if not bool(row["geocode_ok"]):
            continue
        cum: float | None = None
        elev_m_val: float | None = None
        if (i - 1) in last_by_leg:
            cum, elev_m_val = last_by_leg[i - 1]
        elif i in first_by_leg:
            cum, elev_m_val = first_by_leg[i]
        # For Supercharger-resolved waypoints, prefer the dataset's own
        # elevation over the grid lookup — it's authoritative for that
        # exact station location and typically more accurate than the
        # 10-mile-spaced grid cell nearest the waypoint.
        wp_elev = row.get("elev_m_sidecar")
        if wp_elev is not None and pd.notna(wp_elev):
            elev_m_val = float(wp_elev)
        markers.append(
            {
                "waypoint_idx": i,
                "waypoint_clean": row["waypoint_clean"],
                "waypoint_raw": row.get("waypoint_raw"),
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "cum_mi": cum,
                "elev_m": elev_m_val,
                "arrival_soc": row.get("arrival_soc"),
                "depart_soc": row.get("depart_soc"),
                "day": int(row["day"]) if "day" in row and pd.notna(row["day"]) else None,
            }
        )

    marker_df = (
        pd.DataFrame(markers)
        if markers
        else pd.DataFrame(
            columns=[
                "waypoint_idx",
                "waypoint_clean",
                "waypoint_raw",
                "lat",
                "lon",
                "cum_mi",
                "elev_m",
                "arrival_soc",
                "depart_soc",
                "day",
            ]
        )
    )
    stats = {
        "legs_ok": legs_ok,
        "legs_failed": legs_failed,
        "n_points": len(route_df),
        "failures": failures,
        "elevation_source": "local grid" if grid_used else "none (grid missing — elev_m is 0)",
    }
    return route_df, marker_df, stats
