"""Per-leg energy math, day grouping, and trip totals.

These functions take the cleaned dataframe from `parser.py` and return:
- `enrich(df, battery_kwh, charge_rate_usd_per_kwh)` -> adds `kwh_consumed_leg`,
  `kwh_added`, `day`, `cost_per_stop`
- `compute_totals(df)` -> a dict suitable for st.metric cards
- `format_minutes(...)` -> "2d 21h 48m" / "10h 31m" string formatter
"""

from __future__ import annotations

import pandas as pd

from settings import DEFAULT_CHARGE_RATE_USD_PER_KWH


def _kwh_consumed_per_leg(df: pd.DataFrame, battery_kwh: float) -> pd.Series:
    """For each row i, energy used driving from i to i+1.

    Formula: (depart_soc[i] - arrival_soc[i+1]) * battery_kwh.
    SoC drops between leaving here and arriving there, so the result is
    positive for any normal leg. The very last row has no next-row arrival
    SoC, so its leg consumption is 0 (trip is over).
    """
    next_arrival = df["arrival_soc"].shift(-1)
    delta = df["depart_soc"] - next_arrival
    kwh = delta * battery_kwh
    # NaN happens at the trip start (no depart_soc on row 0 if missing) and
    # at the very last row (no next arrival). Treat both as 0 — the trip
    # ends, no leg to attribute consumption to.
    return kwh.fillna(0.0).clip(lower=0.0)


def _kwh_added_per_stop(df: pd.DataFrame, battery_kwh: float) -> pd.Series:
    """Energy added at a charging stop.

    Only counts when depart_soc > arrival_soc. Destinations/hotels where
    arrival ≈ depart get 0, as do the trip endpoints (which have one of
    the two SoCs missing).
    """
    delta = df["depart_soc"] - df["arrival_soc"]
    kwh = (delta * battery_kwh).where(delta > 0, 0.0)
    return kwh.fillna(0.0)


def _assign_days(df: pd.DataFrame) -> pd.Series:
    """Day number per row.

    Start at day 1. After processing each row, if its overnight_nights > 0,
    the *next* row jumps forward by that many days. So a "(+4)" stay means
    the row right after sits 4 days later.
    """
    days = []
    current = 1
    for nights in df["overnight_nights"].astype(int):
        days.append(current)
        if nights > 0:
            current += nights
    return pd.Series(days, index=df.index, dtype=int)


def enrich(
    df: pd.DataFrame,
    battery_kwh: float,
    charge_rate_usd_per_kwh: float = DEFAULT_CHARGE_RATE_USD_PER_KWH,
) -> pd.DataFrame:
    """Return a copy of df with derived energy and cost columns.

    Adds: kwh_consumed_leg, kwh_added, day, cost_per_stop.

    `cost_per_stop` is a flat-rate estimate — kwh_added * charge_rate. No
    authoritative per-station Supercharger pricing exists on a free tier
    (Tesla's rates aren't API-exposed; OpenChargeMap's UsageCost is
    narrative free text), so a user-adjustable single rate is the honest
    MVP. Non-charging stops get 0 by construction since kwh_added is 0.
    """
    out = df.copy()
    out["kwh_consumed_leg"] = _kwh_consumed_per_leg(out, battery_kwh)
    out["kwh_added"] = _kwh_added_per_stop(out, battery_kwh)
    out["day"] = _assign_days(out)
    out["cost_per_stop"] = out["kwh_added"] * float(charge_rate_usd_per_kwh)
    return out


def compute_totals(df: pd.DataFrame) -> dict:
    """Trip-level totals. Expects an `enrich`-ed dataframe."""
    total_miles = float(df["distance_mi"].sum())
    total_cost = float(df["cost_per_stop"].sum()) if "cost_per_stop" in df.columns else 0.0
    return {
        "total_miles": total_miles,
        "total_drive_min": float(df["drive_duration_min"].sum()),
        "total_charge_min": float(df["charge_duration_min"].sum()),
        "total_kwh_consumed": float(df["kwh_consumed_leg"].sum()),
        "total_kwh_added": float(df["kwh_added"].sum()),
        "num_charging_stops": int((df["charge_duration_min"] > 0).sum()),
        "trip_days": int(df["day"].max()) if len(df) else 0,
        "total_cost": total_cost,
        "cost_per_mile": (total_cost / total_miles) if total_miles > 0 else 0.0,
    }


def format_minutes(total_min: float, allow_days: bool = True) -> str:
    """Render a duration like '2d 21h 48m' or '10h 31m' or '45m'.

    `allow_days=False` keeps everything in the hours bucket (so a 30-hour
    charge total renders as '30h 0m', not '1d 6h 0m').
    """
    if total_min is None or pd.isna(total_min):
        return "—"
    total = int(round(total_min))
    if total <= 0:
        return "0m"
    if allow_days and total >= 24 * 60:
        days, rem = divmod(total, 24 * 60)
        hours, mins = divmod(rem, 60)
        return f"{days}d {hours}h {mins}m"
    hours, mins = divmod(total, 60)
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"
