"""Calculations unit tests — energy math, day grouping, totals, formatting."""

import pandas as pd
import pytest

from calculations import (
    _assign_days,
    _kwh_added_per_stop,
    _kwh_consumed_per_leg,
    compute_totals,
    enrich,
    format_minutes,
)


def _make_df(rows: list[dict]) -> pd.DataFrame:
    """Build a parsed-shape dataframe from a minimal spec."""
    cols = {
        "waypoint_raw": "",
        "waypoint_clean": "",
        "arrival_soc": float("nan"),
        "depart_soc": float("nan"),
        "charge_duration_min": 0.0,
        "distance_mi": 0.0,
        "drive_duration_min": 0.0,
        "arrival": "",
        "departure": "",
        "overnight_nights": 0,
        "notes": "",
    }
    full = [{**cols, **row} for row in rows]
    return pd.DataFrame(full)


class TestKwhConsumedPerLeg:
    def test_simple_drop(self):
        """Depart 1.0 -> next arrival 0.7 with 75 kWh battery -> 22.5 kWh used."""
        df = _make_df(
            [
                {"depart_soc": 1.0},
                {"arrival_soc": 0.7, "depart_soc": 0.9},
                {"arrival_soc": 0.5},
            ]
        )
        series = _kwh_consumed_per_leg(df, 75.0)
        assert series.iloc[0] == pytest.approx(22.5)  # 0.3 * 75
        assert series.iloc[1] == pytest.approx(30.0)  # 0.4 * 75
        # Last row: no next arrival, should be 0
        assert series.iloc[2] == 0.0

    def test_negative_clipped_to_zero(self):
        """Physics impossible: arrival > depart on next row. Clip to 0."""
        df = _make_df(
            [
                {"depart_soc": 0.5},
                {"arrival_soc": 0.7},  # higher than previous depart — would be negative
            ]
        )
        assert _kwh_consumed_per_leg(df, 75.0).iloc[0] == 0.0


class TestKwhAddedPerStop:
    def test_charging_stop(self):
        """Arrived 0.1, departed 0.8 with 75 kWh -> 52.5 kWh added."""
        df = _make_df([{"arrival_soc": 0.1, "depart_soc": 0.8}])
        assert _kwh_added_per_stop(df, 75.0).iloc[0] == pytest.approx(52.5)

    def test_destination_no_charging(self):
        """Hotel stop: arrived 0.3, departed 0.3 -> 0 kWh added."""
        df = _make_df([{"arrival_soc": 0.3, "depart_soc": 0.3}])
        assert _kwh_added_per_stop(df, 75.0).iloc[0] == 0.0

    def test_trip_endpoint_nan(self):
        """First/last rows missing one SoC -> 0 (no charging happened)."""
        df = _make_df(
            [
                {"depart_soc": 1.0},  # trip start, no arrival_soc
                {"arrival_soc": 0.1, "depart_soc": 0.5},  # normal charge
                {"arrival_soc": 0.3},  # trip end, no depart_soc
            ]
        )
        series = _kwh_added_per_stop(df, 75.0)
        assert series.iloc[0] == 0.0
        assert series.iloc[2] == 0.0


class TestAssignDays:
    def test_all_day_one_no_overnights(self):
        df = _make_df([{}, {}, {}])
        assert list(_assign_days(df)) == [1, 1, 1]

    def test_single_overnight_increments(self):
        df = _make_df(
            [
                {"overnight_nights": 0},
                {"overnight_nights": 1},
                {"overnight_nights": 0},
            ]
        )
        # After row 1 processes, day counter jumps to 2 for row 2
        assert list(_assign_days(df)) == [1, 1, 2]

    def test_four_night_jump(self):
        """(+4) marker means next row sits 4 days later."""
        df = _make_df(
            [
                {"overnight_nights": 0},
                {"overnight_nights": 4},
                {"overnight_nights": 0},
            ]
        )
        assert list(_assign_days(df)) == [1, 1, 5]


class TestEnrich:
    def test_adds_expected_columns(self):
        df = _make_df(
            [
                {"depart_soc": 1.0},
                {"arrival_soc": 0.3, "depart_soc": 0.8},
                {"arrival_soc": 0.3},
            ]
        )
        out = enrich(df, battery_kwh=75.0, charge_rate_usd_per_kwh=0.45)
        for col in ("kwh_consumed_leg", "kwh_added", "day", "cost_per_stop"):
            assert col in out.columns

    def test_cost_scales_with_rate(self):
        df = _make_df(
            [
                {"arrival_soc": 0.1, "depart_soc": 0.5},  # charges 30 kWh at 75 batt
            ]
        )
        out = enrich(df, 75.0, 0.50)
        # 30 kWh * $0.50 = $15.00
        assert out["cost_per_stop"].iloc[0] == pytest.approx(15.0)

    def test_non_charging_rows_cost_zero(self):
        df = _make_df(
            [
                {"arrival_soc": 0.5, "depart_soc": 0.5},  # no charging
            ]
        )
        out = enrich(df, 75.0, 0.45)
        assert out["cost_per_stop"].iloc[0] == 0.0


class TestComputeTotals:
    def test_returns_expected_keys(self):
        df = _make_df([{"distance_mi": 100.0}])
        totals = compute_totals(enrich(df, 75.0, 0.45))
        for key in (
            "total_miles",
            "total_drive_min",
            "total_charge_min",
            "total_kwh_consumed",
            "total_kwh_added",
            "num_charging_stops",
            "trip_days",
            "total_cost",
            "cost_per_mile",
        ):
            assert key in totals

    def test_cost_per_mile_zero_when_no_miles(self):
        """Divide-by-zero guard on total_miles = 0."""
        df = _make_df([{"distance_mi": 0.0}])
        totals = compute_totals(enrich(df, 75.0, 0.45))
        assert totals["cost_per_mile"] == 0.0

    def test_cost_per_mile_division(self):
        df = _make_df(
            [
                {"arrival_soc": 0.2, "depart_soc": 0.6, "distance_mi": 100.0},
            ]
        )
        totals = compute_totals(enrich(df, 75.0, 0.50))
        # 30 kWh * $0.50 = $15 / 100 mi = $0.15/mi
        assert totals["cost_per_mile"] == pytest.approx(0.15)


class TestFormatMinutes:
    @pytest.mark.parametrize(
        "mins,expected",
        [
            (0, "0m"),
            (45, "45m"),
            (60, "1h 0m"),
            (125, "2h 5m"),
            (24 * 60 + 30, "1d 0h 30m"),
            (2 * 24 * 60 + 21 * 60 + 48, "2d 21h 48m"),
        ],
    )
    def test_cases_with_days(self, mins, expected):
        assert format_minutes(mins) == expected

    def test_allow_days_false_keeps_hours_bucket(self):
        """30 hours should render as '30h 0m', not '1d 6h 0m'."""
        assert format_minutes(30 * 60, allow_days=False) == "30h 0m"

    def test_nan_returns_dash(self):
        assert format_minutes(float("nan")) == "—"
