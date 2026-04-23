"""ABRP Trip Dashboard — Streamlit entrypoint.

Renders an uploaded ABRP `.xlsx` trip export as a single-page dashboard:

    Sidebar:  battery capacity · charging rate · file upload · day filter
    Main:     six summary cards
              trip map (pydeck, colored waypoint markers, style picker)
              elevation profile (Plotly, backed by local US grid)
              per-leg bars (miles / drive min / charge min / kWh)
              geocoding + routing status panels
              raw parsed waypoints (collapsible)

All network calls are keyless (Photon for addresses, OSRM for routing).
Elevations come from a committed local grid — see `elevation_grid.py`.
The map and elevation chart are rendered into st.empty() placeholders
near the top so the page reads top→bottom even though the data they
need is fetched further down the script.
"""

import html
import math

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pydeck as pdk
import streamlit as st
from plotly.subplots import make_subplots

from calculations import compute_totals, enrich, format_minutes
from enrichment import EnrichmentError, build_route, geocode_waypoints
from parser import parse_abrp
from settings import (
    BATTERY_KWH_MAX,
    BATTERY_KWH_MIN,
    BATTERY_KWH_STEP,
    CHARGE_RATE_MAX,
    CHARGE_RATE_MIN,
    CHARGE_RATE_STEP,
    DEFAULT_BATTERY_KWH,
    DEFAULT_CHARGE_RATE_USD_PER_KWH,
)

METERS_TO_FEET = 3.28084


def _soc_to_color(soc) -> list[int]:
    """Map arrival SoC (0..1) to a red->yellow->green RGBA for the map markers.

    Trip endpoints (row 0's arrival_soc, last row's depart_soc) are NaN —
    render those as neutral gray so we don't imply a "color reading" for
    data that doesn't exist. Uses pd.isna so numpy scalars (float32/float64)
    are caught in addition to plain Python NaN and None.
    """
    if pd.isna(soc):
        return [160, 160, 160, 200]
    s = max(0.0, min(1.0, float(soc)))
    if s < 0.5:
        return [255, int(255 * s / 0.5), 0, 220]
    return [int(255 * (1 - (s - 0.5) / 0.5)), 255, 0, 220]


def _fmt_pct(value) -> str:
    """Format 0.17 -> '17%', NaN/None -> 'n/a'. Used in the map tooltip."""
    if pd.isna(value):
        return "n/a"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{f * 100:.0f}%"


st.set_page_config(page_title="ABRP Trip Dashboard", layout="wide")
st.title("ABRP Trip Dashboard")

# ----- Sidebar: vehicle + upload -----
with st.sidebar:
    st.header("Vehicle settings")
    battery_kwh = st.number_input(
        "Battery capacity (kWh)",
        min_value=BATTERY_KWH_MIN,
        max_value=BATTERY_KWH_MAX,
        value=DEFAULT_BATTERY_KWH,
        step=BATTERY_KWH_STEP,
        help="Usable battery capacity. Default is a typical Tesla Model Y LR.",
    )
    charge_rate_usd_per_kwh = st.number_input(
        "Charging rate ($/kWh)",
        min_value=CHARGE_RATE_MIN,
        max_value=CHARGE_RATE_MAX,
        value=DEFAULT_CHARGE_RATE_USD_PER_KWH,
        step=CHARGE_RATE_STEP,
        format="%.2f",
        help=(
            "Flat rate applied to every charging stop for the cost estimate. "
            "US Tesla Supercharger averages sit around $0.36-0.47/kWh; adjust "
            "to match your actual plan or state."
        ),
    )

    st.header("Trip data")
    uploaded = st.file_uploader(
        "ABRP plan export (.xlsx)",
        type=["xlsx"],
        help="Export from A Better Route Planner — the file with sheet 'ABRP Plan'.",
    )

if uploaded is None:
    st.info("Upload an ABRP `.xlsx` export from the sidebar to get started.")
    st.stop()

# ----- File-size guard -----
# Real ABRP exports are < 100 KB. Cap at 5 MB to reject absurd/malicious
# uploads before pandas/openpyxl touches them (zip-bomb defense in depth).
# Streamlit also enforces a 10 MB ceiling via .streamlit/config.toml.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
if uploaded.size > MAX_UPLOAD_BYTES:
    st.error(
        f"File is {uploaded.size / (1024 * 1024):.1f} MB — max allowed is "
        f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB. ABRP exports are typically "
        "well under 1 MB; if yours is this large it may not be an ABRP file."
    )
    st.stop()

# ----- Parse + enrich -----
try:
    # pandas' type stubs declare `path: str | Path`, but `pd.read_excel`
    # happily accepts Streamlit's UploadedFile at runtime. Silence the
    # stub-only warning rather than plumb a temp file through the call.
    df = parse_abrp(uploaded)  # type: ignore[arg-type]
except Exception as e:
    st.error(f"Couldn't parse that file as an ABRP plan export.\n\n**{type(e).__name__}:** {e}")
    st.stop()

enriched = enrich(df, battery_kwh, charge_rate_usd_per_kwh)
max_day = int(enriched["day"].max()) if len(enriched) else 1

# ----- Sidebar: filters (depends on parsed data) -----
with st.sidebar:
    st.header("Filters")
    if max_day > 1:
        day_range = st.slider(
            "Day range",
            min_value=1,
            max_value=max_day,
            value=(1, max_day),
            step=1,
            help="Filter which trip days feed the cards and charts.",
        )
    else:
        st.caption("Trip is a single day — no day filter.")
        day_range = (1, 1)

lo, hi = day_range
view = enriched[enriched["day"].between(lo, hi)].reset_index(drop=True)
totals = compute_totals(view)

# ----- Header caption -----
range_note = f"day {lo}" if lo == hi else f"days {lo}–{hi}"
st.caption(
    f"Showing {len(view)} of {len(enriched)} waypoints · {range_note} of {max_day} · battery {battery_kwh:g} kWh"
)

if len(view) == 0:
    st.warning("No waypoints fall in that day range.")
    st.stop()

# ----- Summary cards -----
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Total miles", f"{totals['total_miles']:,.0f}")
c2.metric("Drive time", format_minutes(totals["total_drive_min"]))
c3.metric("Charge time", format_minutes(totals["total_charge_min"], allow_days=False))
c4.metric("kWh used", f"{totals['total_kwh_consumed']:,.1f}")
c5.metric("Charging stops", f"{totals['num_charging_stops']}")
c6.metric(
    "Est. charge cost",
    f"${totals['total_cost']:,.2f}",
    delta=f"${totals['cost_per_mile']:.2f}/mi",
    delta_color="off",
)

# ----- Map + Elevation placeholders -----
# The actual map and elevation chart depend on geocoding + routing (below)
# which can take 10–60 seconds on a cold cache. We reserve the visual slots
# here so the page reads top→bottom in the right order, then backfill them
# once the data is ready. While routing runs the user sees a loading info
# box in each slot instead of an empty space.
st.subheader("Trip map")
# Radio (not selectbox) so the picker can't be typed into — Streamlit's
# selectbox is actually a combobox with keyboard filtering, which feels
# like a bug when you expect a fixed picker.
# "satellite" is dropped: Carto (our keyless basemap provider) has no
# satellite preset, and pydeck 0.9 doesn't expose a TileLayer wrapper
# we could use to drop in an Esri/ArcGIS raster source without a larger
# pydeck upgrade or a Mapbox token.
map_style = st.radio(
    "Basemap",
    options=["road", "dark", "light", "dark_no_labels", "light_no_labels"],
    index=0,
    horizontal=True,
    key="map_style",
    help="Changes the underlying map tiles.",
)
map_placeholder = st.empty()
map_placeholder.info("⏳ Waiting for geocoding + routing below…")

st.subheader("Elevation profile")
elev_placeholder = st.empty()
elev_placeholder.info("⏳ Waiting for geocoding + routing below…")

# ----- Per-leg bars (small multiples) -----
# Each row describes a waypoint and the leg leaving it. Distance / drive /
# kWh-consumed are that outgoing leg. Charge minutes is the charge that
# happened AT this waypoint before departing. The last row in the current
# view has no outgoing leg in-range — its metrics are effectively zero or
# belong to the next filtered day.
st.subheader("Per-leg breakdown")
leg_x = [f"{i + 1}" for i in range(len(view))]
next_wp = view["waypoint_clean"].shift(-1).fillna("(end)")
leg_hover = [
    f"Leg {i + 1}<br>{src} → {dst}"
    for i, (src, dst) in enumerate(zip(view["waypoint_clean"], next_wp))
]

legs_fig = make_subplots(
    rows=4,
    cols=1,
    shared_xaxes=True,
    vertical_spacing=0.05,
    subplot_titles=("Distance (mi)", "Drive time (min)", "Charge time (min)", "kWh consumed"),
)
bar_specs = [
    (view["distance_mi"], "#1f77b4"),
    (view["drive_duration_min"], "#2ca02c"),
    (view["charge_duration_min"], "#ff7f0e"),
    (view["kwh_consumed_leg"], "#d62728"),
]
for row_i, (series, color) in enumerate(bar_specs, start=1):
    legs_fig.add_trace(
        go.Bar(
            x=leg_x,
            y=series,
            marker_color=color,
            hovertext=leg_hover,
            hovertemplate="%{hovertext}<br>%{y}<extra></extra>",
            showlegend=False,
        ),
        row=row_i,
        col=1,
    )
legs_fig.update_xaxes(title_text="Leg # (within filtered range)", row=4, col=1)
legs_fig.update_layout(height=620, margin=dict(l=10, r=10, t=40, b=10), bargap=0.15)
st.plotly_chart(legs_fig, use_container_width=True)

# ----- Geocoding (3-tier: Supercharger sidecar -> Photon -> geometric) -----
st.subheader("Geocoding")
# st.spinner (not st.progress) so @st.cache_data replays cleanly on warm
# reruns — cache_data replays every Streamlit element call inside the
# decorated function, and those calls can't reference widgets created
# outside (which a progress bar passed in as an arg would be).
try:
    with st.spinner("Geocoding waypoints…"):
        geocoded = geocode_waypoints(view)
except EnrichmentError as e:
    st.error(f"Geocoding pipeline crashed.\n\n**{type(e).__name__}:** {e}")
    geocoded = None

if geocoded is not None:
    tier_counts = geocoded["geocode_tier"].value_counts().to_dict()
    sc_count = int(tier_counts.get("supercharger", 0))
    photon_count = int(tier_counts.get("photon", 0))
    geom_count = int(tier_counts.get("geometric", 0))
    fail_count = int(tier_counts.get("failed", 0))
    ok_count = sc_count + photon_count + geom_count

    summary_parts = []
    if sc_count:
        summary_parts.append(f"🔋 {sc_count} Supercharger")
    if photon_count:
        summary_parts.append(f"✅ {photon_count} Photon")
    if geom_count:
        summary_parts.append(f"📐 {geom_count} geometric")
    if fail_count:
        summary_parts.append(f"❌ {fail_count} failed")
    summary = " · ".join(summary_parts) if summary_parts else "no waypoints"

    if fail_count:
        st.warning(f"Geocoded {ok_count} of {len(geocoded)} waypoints · {summary}")
    else:
        st.success(f"Geocoded {ok_count} of {len(geocoded)} waypoints · {summary}")

    tier_icon = {
        "supercharger": "🔋",
        "photon": "✅",
        "geometric": "📐",
        "failed": "❌",
    }
    status_df = pd.DataFrame(
        {
            "Status": geocoded["geocode_tier"].map(tier_icon).fillna("❌"),
            "Waypoint": geocoded["waypoint_clean"],
            "Lat": geocoded["lat"],
            "Lon": geocoded["lon"],
            "Note": geocoded["geocode_note"],
        }
    )
    header = f"Geocoding status ({summary})"
    with st.expander(header, expanded=(geom_count > 0 or fail_count > 0)):
        st.dataframe(status_df, use_container_width=True, hide_index=True)

# ----- Routing + elevation -----
route_df: pd.DataFrame | None = None
marker_df: pd.DataFrame | None = None
route_stats: dict | None = None
if geocoded is not None:
    st.subheader("Routing + elevation")
    n_legs = max(len(geocoded) - 1, 0)
    if n_legs == 0:
        st.info("Need at least two waypoints to fetch a route.")
    else:
        try:
            with st.spinner(f"Routing {n_legs} legs…"):
                route_df, marker_df, route_stats = build_route(geocoded)
        except EnrichmentError as e:
            st.error(
                "Couldn't reach OSRM for routing — check your internet "
                f"connection.\n\n**{type(e).__name__}:** {e}"
            )
            route_df, marker_df, route_stats = None, None, None

        if route_stats is not None:
            elev_note = f" · elevation: {route_stats.get('elevation_source', 'unknown')}"
            if route_stats["legs_failed"]:
                st.warning(
                    f"Routed {route_stats['legs_ok']} of {n_legs} legs "
                    f"({route_stats['legs_failed']} failed) · "
                    f"{route_stats['n_points']:,} route points{elev_note}. "
                    "Hit **Rerun** (R) to retry just the failed legs — "
                    "successful ones stay cached."
                )
                failures = route_stats.get("failures") or []
                if failures:
                    failures_df = pd.DataFrame(failures)[["leg_idx", "from", "to", "reason"]]
                    with st.expander(f"Failed legs ({len(failures)})", expanded=True):
                        st.dataframe(failures_df, use_container_width=True, hide_index=True)
            else:
                st.success(
                    f"Routed {route_stats['legs_ok']} of {n_legs} legs · "
                    f"{route_stats['n_points']:,} route points{elev_note}."
                )

# ----- Backfill Map + Elevation placeholders reserved near the top -----
# At this point geocoding + routing have run; route_df and marker_df are
# populated (or None if routing didn't run or produced nothing). Fill in
# the two placeholder slots we reserved above so the visible page order
# matches the spec (Summary → Map → Elevation → Per-leg → ...).

if route_df is not None and marker_df is not None and len(route_df) and len(marker_df):
    # ---- Map ----
    paths_data = []
    for leg_idx, group in route_df.groupby("leg_idx"):
        path_coords = group[["lon", "lat"]].to_numpy().tolist()
        paths_data.append({"leg_idx": int(leg_idx), "path": path_coords})
    paths_df = pd.DataFrame(paths_data) if paths_data else pd.DataFrame(columns=["leg_idx", "path"])

    map_markers = marker_df.copy()
    map_markers["fill_color"] = map_markers["arrival_soc"].apply(_soc_to_color)
    map_markers["arrival_pct"] = map_markers["arrival_soc"].apply(_fmt_pct)
    map_markers["depart_pct"] = map_markers["depart_soc"].apply(_fmt_pct)
    # Escape the waypoint name before it's interpolated into the pydeck
    # tooltip HTML — pydeck substitutes {field} server-side and does not
    # itself escape values. A waypoint name containing HTML (unlikely but
    # user-controlled via the ABRP export) would otherwise render literally.
    map_markers["waypoint_clean_html"] = map_markers["waypoint_clean"].apply(
        lambda s: html.escape(str(s) if s is not None else "")
    )

    path_layer = pdk.Layer(
        "PathLayer",
        data=paths_df,
        get_path="path",
        get_color=[30, 120, 200, 200],
        width_scale=20,
        width_min_pixels=2,
        get_width=5,
        pickable=False,
    )
    scatter_layer = pdk.Layer(
        "ScatterplotLayer",
        data=map_markers,
        get_position=["lon", "lat"],
        get_fill_color="fill_color",
        get_line_color=[255, 255, 255],
        line_width_min_pixels=1,
        get_radius=6000,
        radius_min_pixels=6,
        radius_max_pixels=14,
        pickable=True,
    )
    lat_min = float(map_markers["lat"].min())
    lat_max = float(map_markers["lat"].max())
    lon_min = float(map_markers["lon"].min())
    lon_max = float(map_markers["lon"].max())
    span = max(lat_max - lat_min, (lon_max - lon_min) / 2.0)
    zoom = max(3.0, min(10.0, 7.0 - math.log2(max(0.5, span))))
    view_state = pdk.ViewState(
        latitude=(lat_min + lat_max) / 2.0,
        longitude=(lon_min + lon_max) / 2.0,
        zoom=zoom,
        pitch=0,
        bearing=0,
    )
    tooltip = {
        "html": (
            "<b>{waypoint_clean_html}</b><br/>Arrival: {arrival_pct}<br/>Depart: {depart_pct}"
        ),
        "style": {
            "backgroundColor": "#111",
            "color": "white",
            "padding": "6px",
            "borderRadius": "4px",
        },
    }
    deck = pdk.Deck(
        layers=[path_layer, scatter_layer],
        initial_view_state=view_state,
        tooltip=tooltip,
        map_provider="carto",
        map_style=map_style,
    )
    with map_placeholder.container():
        st.pydeck_chart(deck, use_container_width=True)
        st.caption(
            "Waypoint color = arrival SoC (red = near-empty, green = near-full). "
            "Gray = trip endpoints."
        )

    # ---- Elevation profile ----
    elev_ft = route_df["elev_m"].to_numpy() * METERS_TO_FEET
    elev_fig = go.Figure()
    elev_fig.add_trace(
        go.Scatter(
            x=route_df["cum_mi"],
            y=elev_ft,
            mode="lines",
            name="Elevation",
            line=dict(color="#3d6b3d", width=1),
            hovertemplate="%{x:,.0f} mi · %{y:,.0f} ft<extra></extra>",
            fill="tozeroy",
            fillcolor="rgba(61,107,61,0.15)",
        )
    )
    wp = marker_df.dropna(subset=["cum_mi", "elev_m"]).copy()
    if len(wp):
        wp_elev_ft = wp["elev_m"].to_numpy() * METERS_TO_FEET
        elev_fig.add_trace(
            go.Scatter(
                x=wp["cum_mi"],
                y=wp_elev_ft,
                mode="markers",
                name="Waypoints",
                marker=dict(color="#d62728", size=8, line=dict(color="white", width=1.5)),
                customdata=np.stack(
                    [wp["elev_m"].to_numpy(), wp["waypoint_clean"].to_numpy()], axis=-1
                ),
                hovertemplate=(
                    "<b>%{customdata[1]}</b><br>"
                    "%{x:,.0f} mi · %{y:,.0f} ft "
                    "(raw %{customdata[0]:.0f} m)<extra></extra>"
                ),
            )
        )
    elev_fig.update_layout(
        height=380,
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis_title="Cumulative miles",
        yaxis_title="Elevation (ft)",
        hovermode="closest",
        showlegend=False,
    )
    with elev_placeholder.container():
        st.plotly_chart(elev_fig, use_container_width=True)
else:
    # Routing either didn't run (no key needed but upstream failed) or
    # produced no usable data — swap the "waiting" info for a clear
    # "unavailable" state so the slots don't linger as spinners forever.
    map_placeholder.info(
        "Map unavailable — routing didn't produce a usable route. "
        "See the Routing status below for details."
    )
    elev_placeholder.info("Elevation unavailable — routing didn't produce a usable route.")

# ----- Raw table -----
with st.expander("Parsed waypoints (raw, filtered)", expanded=False):
    st.dataframe(view, use_container_width=True)
