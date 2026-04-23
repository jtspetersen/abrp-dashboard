"""Local US elevation grid — nearest-neighbor lookup via KDTree.

The `.npz` is produced by `scripts/build_elevation_grid.py` (one-off).
At import time we load the three arrays (`lats`, `lons`, `elev_m`),
convert lat/lon to 3D unit-sphere coordinates, and index them with
a `scipy.spatial.cKDTree`. That 3D projection makes euclidean KD
distance behave like a great-circle distance (for small angles),
so the "nearest grid cell" answer is geometrically meaningful even
across the wide longitude range that warps flat lat/lon distance.

Runtime lookups are microsecond-scale per point and vectorize
cleanly over the ~28k route vertices a build_route call produces.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import streamlit as st

GRID_PATH = Path(__file__).parent / "data" / "us_elevation_grid.npz"


def _lat_lon_to_xyz(lat_deg: np.ndarray, lon_deg: np.ndarray) -> np.ndarray:
    """Project (lat, lon) in degrees to 3D unit-sphere (x, y, z) for KDTree."""
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)))


@st.cache_resource(show_spinner=False)
def _load_grid():
    """Load the .npz and build a KDTree. Cached so it's done once per session."""
    if not GRID_PATH.exists():
        raise FileNotFoundError(
            f"Elevation grid not found at {GRID_PATH}. "
            "Run `python scripts/build_elevation_grid.py` to generate it."
        )
    # scipy is only imported when we actually have a grid to load.
    from scipy.spatial import cKDTree

    data = np.load(GRID_PATH)
    lats = data["lats"].astype(np.float32)
    lons = data["lons"].astype(np.float32)
    elev_m = data["elev_m"].astype(np.float32)
    xyz = _lat_lon_to_xyz(lats, lons)
    tree = cKDTree(xyz)
    return {"lats": lats, "lons": lons, "elev_m": elev_m, "tree": tree}


def is_available() -> bool:
    """True iff the grid file exists and can be loaded."""
    return GRID_PATH.exists()


def lookup_m(lat: float, lon: float) -> float | None:
    """Single-point elevation lookup. Returns meters, or None if grid missing."""
    try:
        grid = _load_grid()
    except FileNotFoundError:
        return None
    xyz = _lat_lon_to_xyz(np.array([lat]), np.array([lon]))
    _, idx = grid["tree"].query(xyz, k=1)
    return float(grid["elev_m"][idx[0]])


def lookup_m_batch(lats: np.ndarray, lons: np.ndarray) -> np.ndarray | None:
    """Vectorized elevation lookup for route vertices.

    `lats` and `lons` are same-length 1D arrays of degrees. Returns a
    float32 array of elevation in meters, or None if the grid file is
    missing (caller should fall back to zeros or skip elevation).
    """
    try:
        grid = _load_grid()
    except FileNotFoundError:
        return None
    if len(lats) == 0:
        return np.array([], dtype=np.float32)
    xyz = _lat_lon_to_xyz(np.asarray(lats), np.asarray(lons))
    _, idx = grid["tree"].query(xyz, k=1)
    return grid["elev_m"][idx].astype(np.float32)
