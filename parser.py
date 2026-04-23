"""Read and clean an ABRP trip export (.xlsx) into a tidy DataFrame.

The ABRP `.xlsx` layout observed in the sample:
    row 0 : label cell "ABRP Plan"
    row 1 : plan URL
    row 2 : blank
    row 3 : column headers (Waypoint, Arrival SoC, ...)
    row 4..N-2 : waypoint rows
    row N-1    : totals/summary row (first cell looks like "3 days 8 h 20 min")

The spec nominally said headers live at row index 2, but the real file puts
them at index 3. We locate them by scanning for the first cell "Waypoint"
so small layout shifts don't break the parser.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from settings import ABRP_SHEET_NAME

# Vendor tags we want off the end of waypoint names, e.g. "... [Tesla]".
_VENDOR_TAG_RE = re.compile(r"\s*\[[^\]]+\]\s*$")

# Overnight marker in the Departure cell, e.g. "9:00 AM (+1)" or "9:00 AM (+4)".
_OVERNIGHT_RE = re.compile(r"\s*\(\+(\d+)\)\s*$")

# Duration parser: captures "2 h 56 min", "30 min", "1 h", "2 days 21 h 48 min".
_DUR_RE = re.compile(
    r"(?:(\d+)\s*days?)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?",
    re.IGNORECASE,
)


def _parse_duration_to_min(val) -> float:
    """Turn a duration string like '2 h 56 min' into minutes.

    Empty / NaN -> 0. Unparseable -> 0 (we never want a None bubbling up to
    the charts). Handles 'days' too so we can reuse this for the totals row
    if we ever need to.
    """
    if pd.isna(val):
        return 0.0
    s = str(val).strip()
    if not s:
        return 0.0
    m = _DUR_RE.fullmatch(s)
    if not m or not any(m.groups()):
        return 0.0
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    mins = int(m.group(3) or 0)
    return days * 24 * 60 + hours * 60 + mins


def _parse_distance_to_mi(val) -> float:
    """Turn '185 mi' / '7.4 mi' / '0 ft' into miles.

    Feet get converted (ft / 5280) so the totals math stays honest, even
    though the ABRP last leg is typically "0 ft" by construction.
    """
    if pd.isna(val):
        return 0.0
    s = str(val).strip().lower()
    if not s:
        return 0.0
    m = re.match(r"([\d.]+)\s*(mi|ft)\b", s)
    if not m:
        return 0.0
    num = float(m.group(1))
    unit = m.group(2)
    if unit == "mi":
        return num
    return num / 5280.0  # ft -> mi


def _split_overnight(departure) -> tuple[str, int]:
    """Strip the '(+N)' overnight marker and return (clean_time, nights)."""
    if pd.isna(departure):
        return ("", 0)
    s = str(departure).strip()
    m = _OVERNIGHT_RE.search(s)
    if not m:
        return (s, 0)
    nights = int(m.group(1))
    cleaned = _OVERNIGHT_RE.sub("", s).strip()
    return (cleaned, nights)


def _strip_vendor_tag(name) -> str:
    if pd.isna(name):
        return ""
    return _VENDOR_TAG_RE.sub("", str(name)).strip()


def _find_header_row(raw: pd.DataFrame) -> int:
    """Locate the row whose first cell is literally 'Waypoint'.

    Scans the entire sheet — ABRP today emits the header at row 3
    (title / URL / blank / header), but they've inserted metadata rows
    in past format revisions. The sheet is already in memory, so the
    full scan is microseconds.
    """
    for i in range(len(raw)):
        cell = raw.iat[i, 0]
        if isinstance(cell, str) and cell.strip().lower() == "waypoint":
            return i
    raise ValueError("Could not find a 'Waypoint' header row anywhere in the sheet.")


def _is_totals_row(row: pd.Series) -> bool:
    """The trailing totals row has no Arrival SoC and a duration-looking first cell."""
    first = row.iloc[0]
    arrival_soc = row.get("Arrival SoC")
    if not isinstance(first, str):
        return False
    looks_like_total = (
        ("day" in first.lower()) or (" h " in first.lower()) or first.lower().endswith(" h")
    )
    return looks_like_total and pd.isna(arrival_soc)


def parse_abrp(path: str | Path) -> pd.DataFrame:
    """Parse an ABRP .xlsx export into a cleaned waypoint DataFrame.

    Columns returned:
        waypoint_raw, waypoint_clean,
        arrival_soc, depart_soc,
        charge_duration_min, distance_mi, drive_duration_min,
        arrival, departure, overnight_nights, notes
    """
    raw = pd.read_excel(path, sheet_name=ABRP_SHEET_NAME, header=None)

    header_idx = _find_header_row(raw)
    headers = raw.iloc[header_idx].tolist()
    body = raw.iloc[header_idx + 1 :].copy()
    body.columns = headers
    body = body.reset_index(drop=True)

    # Drop the trailing totals row if present.
    if len(body) and _is_totals_row(body.iloc[-1]):
        body = body.iloc[:-1].copy()

    # Drop rows that are completely blank (defensive).
    body = body.dropna(how="all").reset_index(drop=True)

    departures_split = body["Departure"].apply(_split_overnight)

    out = pd.DataFrame(
        {
            "waypoint_raw": body["Waypoint"].astype("string").fillna(""),
            "waypoint_clean": body["Waypoint"].apply(_strip_vendor_tag),
            "arrival_soc": pd.to_numeric(body["Arrival SoC"], errors="coerce"),
            "depart_soc": pd.to_numeric(body["Depart SoC"], errors="coerce"),
            "charge_duration_min": body["Charge duration"].apply(_parse_duration_to_min),
            "distance_mi": body["Distance"].apply(_parse_distance_to_mi),
            "drive_duration_min": body["Drive duration"].apply(_parse_duration_to_min),
            "arrival": body["Arrival"].astype("string").fillna(""),
            "departure": departures_split.apply(lambda t: t[0]).astype("string"),
            "overnight_nights": departures_split.apply(lambda t: t[1]).astype(int),
            "notes": body["Notes"].astype("string").fillna(""),
        }
    )

    return out


if __name__ == "__main__":
    df = parse_abrp("data/sample_trip.xlsx")
    pd.set_option("display.max_colwidth", 50)
    pd.set_option("display.width", 200)
    print(f"Parsed {len(df)} rows, {len(df.columns)} columns.")
    print("Columns:", list(df.columns))
    print()
    print(df.to_string())
    print()
    print("Dtypes:")
    print(df.dtypes)
    print()
    print(f"Rows with overnight_nights > 0: {(df['overnight_nights'] > 0).sum()}")
    print(f"Rows with charge_duration_min > 0: {(df['charge_duration_min'] > 0).sum()}")
    print(f"Sum distance_mi: {df['distance_mi'].sum():.1f} mi")
    print(f"Sum drive_duration_min: {df['drive_duration_min'].sum():.0f} min")
    print(f"Sum charge_duration_min: {df['charge_duration_min'].sum():.0f} min")
