"""Parser unit tests — ABRP xlsx -> cleaned dataframe."""

import math

import pandas as pd
import pytest

from parser import (
    _find_header_row,
    _parse_distance_to_mi,
    _parse_duration_to_min,
    _split_overnight,
    _strip_vendor_tag,
)

# ----- Pure helpers -----


class TestDurationParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("2 h 56 min", 176),
            ("30 min", 30),
            ("1 h", 60),
            ("2 days 21 h 48 min", 2 * 24 * 60 + 21 * 60 + 48),
            ("", 0),
        ],
    )
    def test_known_cases(self, text, expected):
        assert _parse_duration_to_min(text) == expected

    def test_nan_returns_zero(self):
        assert _parse_duration_to_min(float("nan")) == 0

    def test_garbage_returns_zero(self):
        assert _parse_duration_to_min("not a duration") == 0


class TestDistanceParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("185 mi", 185.0),
            ("7.4 mi", 7.4),
            ("0 mi", 0.0),
        ],
    )
    def test_miles(self, text, expected):
        assert _parse_distance_to_mi(text) == expected

    def test_feet_converted_to_miles(self):
        # 5280 ft = 1 mi
        assert _parse_distance_to_mi("5280 ft") == pytest.approx(1.0)
        assert _parse_distance_to_mi("0 ft") == 0.0

    def test_nan_returns_zero(self):
        assert _parse_distance_to_mi(float("nan")) == 0.0


class TestOvernightSplit:
    @pytest.mark.parametrize(
        "text,expected_time,expected_nights",
        [
            ("9:00 AM (+1)", "9:00 AM", 1),
            ("12:30 PM (+4)", "12:30 PM", 4),
            ("9:00 AM", "9:00 AM", 0),
            ("", "", 0),
        ],
    )
    def test_known_cases(self, text, expected_time, expected_nights):
        time, nights = _split_overnight(text)
        assert time == expected_time
        assert nights == expected_nights


class TestVendorTagStripping:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Tesla Supercharger Truckee, CA [Tesla]", "Tesla Supercharger Truckee, CA"),
            ("Somewhere, NV [ChargePoint]", "Somewhere, NV"),
            ("1930 E Idaho St, Elko", "1930 E Idaho St, Elko"),  # no tag
        ],
    )
    def test_known_cases(self, raw, expected):
        assert _strip_vendor_tag(raw) == expected


# ----- Header detection -----


class TestHeaderRow:
    def test_finds_waypoint_row(self):
        df = pd.DataFrame(
            [
                ["ABRP Plan", None],
                ["https://...", None],
                [None, None],
                ["Waypoint", "Arrival SoC"],
                ["row0", "0.5"],
            ]
        )
        assert _find_header_row(df) == 3

    def test_case_insensitive_match(self):
        df = pd.DataFrame([["WAYPOINT", None]])
        assert _find_header_row(df) == 0

    def test_raises_when_missing(self):
        df = pd.DataFrame([["nothing to see here"]] * 5)
        with pytest.raises(ValueError, match="Could not find"):
            _find_header_row(df)


# ----- Full parse -----


class TestParseAbrp:
    def test_returns_nonempty_dataframe(self, parsed_sample):
        assert isinstance(parsed_sample, pd.DataFrame)
        assert len(parsed_sample) > 0

    def test_has_expected_columns(self, parsed_sample):
        expected = {
            "waypoint_raw",
            "waypoint_clean",
            "arrival_soc",
            "depart_soc",
            "charge_duration_min",
            "distance_mi",
            "drive_duration_min",
            "arrival",
            "departure",
            "overnight_nights",
            "notes",
        }
        assert set(parsed_sample.columns) == expected

    def test_first_row_has_no_arrival_soc(self, parsed_sample):
        """Trip starts fresh — no one arrived at the origin."""
        assert math.isnan(parsed_sample.iloc[0]["arrival_soc"])

    def test_last_row_has_no_depart_soc(self, parsed_sample):
        """Trip ends — no departure from the destination."""
        assert math.isnan(parsed_sample.iloc[-1]["depart_soc"])

    def test_totals_row_dropped(self, parsed_sample):
        """The summary row ("X days Y h Z min" in first cell) must be
        filtered out by the parser."""
        for raw in parsed_sample["waypoint_raw"]:
            low = str(raw).lower()
            assert "days" not in low or "day" not in low.split()[0]

    def test_overnight_nights_are_ints(self, parsed_sample):
        assert parsed_sample["overnight_nights"].dtype.kind == "i"
        assert (parsed_sample["overnight_nights"] >= 0).all()

    def test_durations_are_numeric(self, parsed_sample):
        assert pd.api.types.is_numeric_dtype(parsed_sample["charge_duration_min"])
        assert pd.api.types.is_numeric_dtype(parsed_sample["drive_duration_min"])
        assert (parsed_sample["charge_duration_min"] >= 0).all()
        assert (parsed_sample["drive_duration_min"] >= 0).all()

    def test_distances_are_numeric_nonneg(self, parsed_sample):
        assert pd.api.types.is_numeric_dtype(parsed_sample["distance_mi"])
        assert (parsed_sample["distance_mi"] >= 0).all()

    def test_vendor_tags_stripped_from_clean(self, parsed_sample):
        """No [Tesla] etc. should survive in waypoint_clean."""
        for clean in parsed_sample["waypoint_clean"]:
            assert not str(clean).endswith("]")
