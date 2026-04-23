"""Enrichment unit tests — pure math helpers, matchers, exception types.

No live-network tests — Photon/OSRM calls are integration concerns and
belong in a separate tier if we ever add one.
"""

import pytest

from enrichment import (
    EnrichmentError,
    GeocodeError,
    RoutingError,
    _geometric_estimate,
    _haversine_mi,
    _in_bbox,
    _match_supercharger,
    _neighbor_bbox,
    _normalize_for_photon,
    _offset_toward,
)

# ----- Haversine -----


class TestHaversine:
    def test_same_point_is_zero(self):
        assert _haversine_mi(37.7, -122.4, 37.7, -122.4) == pytest.approx(0.0, abs=0.01)

    def test_sf_to_nyc_known_distance(self):
        # San Francisco → New York City: ~2572 miles great-circle.
        mi = _haversine_mi(37.7749, -122.4194, 40.7128, -74.0060)
        assert mi == pytest.approx(2572, abs=50)  # ±50 mi tolerance

    def test_denver_to_colorado_springs(self):
        # Denver → Colorado Springs: ~62 mi
        mi = _haversine_mi(39.7392, -104.9903, 38.8339, -104.8214)
        assert mi == pytest.approx(62, abs=5)

    def test_symmetry(self):
        a = _haversine_mi(37.7, -122.4, 40.7, -74.0)
        b = _haversine_mi(40.7, -74.0, 37.7, -122.4)
        assert a == pytest.approx(b)


# ----- Offset + Geometric estimate -----


class TestOffsetToward:
    def test_zero_distance_returns_origin(self):
        coord = _offset_toward(40.0, -100.0, 41.0, -99.0, 0.0)
        assert coord is not None
        assert coord == pytest.approx((40.0, -100.0), abs=1e-6)

    def test_halfway_point(self):
        """Offset by half the separation should land at the midpoint."""
        # (40, -100) to (40, -99) at lat 40: ~53 mi between. Offset 26.5 mi
        # should land ~halfway.
        coord = _offset_toward(40.0, -100.0, 40.0, -99.0, 26.5)
        assert coord is not None
        lat, lon = coord
        assert lat == pytest.approx(40.0, abs=0.01)
        assert lon == pytest.approx(-99.5, abs=0.1)


class TestGeometricEstimate:
    def test_placed_near_closer_neighbor(self):
        """Offset closer to the short-leg neighbor."""
        # prev (40, -100) <-- 100 mi --> (target) <-- 5 mi --> next (40, -98)
        # Target should be ~5 mi west of next.
        coord = _geometric_estimate(
            40.0,
            -100.0,
            40.0,
            -98.0,
            planned_prev_mi=100,
            planned_next_mi=5,
        )
        assert coord is not None
        lat, lon = coord
        # Expect lat ~= 40, lon slightly west of -98
        assert lat == pytest.approx(40.0, abs=0.01)
        assert -98.1 <= lon <= -97.9


# ----- Neighbor bbox -----


class TestNeighborBbox:
    def test_overlapping_disks_returns_tuple(self):
        # Two points ~200 mi apart with overlapping-disk radii.
        bbox = _neighbor_bbox(40.0, -100.0, 40.0, -97.0, 150, 150)
        assert bbox is not None
        lat_min, lon_min, lat_max, lon_max = bbox
        assert lat_min < lat_max
        assert lon_min < lon_max

    def test_disjoint_disks_returns_none(self):
        # Two distant points with tiny radii — disks don't overlap.
        bbox = _neighbor_bbox(40.0, -100.0, 40.0, -90.0, 1, 1)
        assert bbox is None

    def test_bbox_roughly_centered_between_neighbors(self):
        bbox = _neighbor_bbox(40.0, -100.0, 40.0, -98.0, 100, 100)
        assert bbox is not None
        lat_min, lon_min, lat_max, lon_max = bbox
        # bbox center should sit roughly on the line between the two
        assert lat_min <= 40.0 <= lat_max
        assert lon_min <= -99.0 <= lon_max


class TestInBbox:
    def test_point_inside(self):
        assert _in_bbox(40.0, -99.0, (39.0, -100.0, 41.0, -98.0))

    def test_point_outside_lat(self):
        assert not _in_bbox(42.0, -99.0, (39.0, -100.0, 41.0, -98.0))

    def test_point_outside_lon(self):
        assert not _in_bbox(40.0, -101.0, (39.0, -100.0, 41.0, -98.0))

    def test_point_on_boundary_included(self):
        """Boundary is inclusive."""
        assert _in_bbox(40.0, -100.0, (40.0, -100.0, 41.0, -99.0))


# ----- Photon input normalization -----


class TestNormalizeForPhoton:
    def test_strips_zip_and_country(self):
        """Full postal form that Photon chokes on — end result is clean."""
        out = _normalize_for_photon("6103 Majestic Ave, Oakland, CA 94605-1861, United States")
        assert "United States" not in out
        assert "94605" not in out
        assert "6103 Majestic Ave" in out
        assert "Oakland" in out
        assert "CA" in out

    def test_strips_five_digit_zip(self):
        assert "94605" not in _normalize_for_photon("Somewhere, CA 94605")

    def test_leaves_clean_input_alone(self):
        assert _normalize_for_photon("Yellowstone National Park, Wyoming") == (
            "Yellowstone National Park, Wyoming"
        )

    def test_empty_string(self):
        assert _normalize_for_photon("") == ""


# ----- Supercharger matcher -----


class TestMatchSupercharger:
    def test_known_station_by_name(self):
        """Exact name match against the bundled sidecar."""
        station = _match_supercharger("Tesla Supercharger Carlin, NV")
        assert station is not None
        assert "Carlin" in station["name"]
        assert station["state"].upper() == "NV"

    def test_disambiguated_name_match(self):
        """Names with street disambiguator should resolve to the right station."""
        station = _match_supercharger("Tesla Supercharger Lincoln, NE - West O St")
        assert station is not None
        # Should be the West O St one, not any other Lincoln, NE charger.
        assert "West O" in station["name"] or "West O" in (station.get("street") or "")

    def test_non_tesla_returns_none(self):
        assert _match_supercharger("1930 E Idaho St, Elko") is None

    def test_empty_returns_none(self):
        assert _match_supercharger("") is None

    def test_unknown_supercharger_returns_none(self):
        """Made-up Supercharger name should not match."""
        assert _match_supercharger("Tesla Supercharger Narnia, ZZ") is None


# ----- Exception hierarchy -----


class TestExceptionHierarchy:
    def test_geocode_error_subclass(self):
        assert issubclass(GeocodeError, EnrichmentError)

    def test_routing_error_subclass(self):
        assert issubclass(RoutingError, EnrichmentError)

    def test_subclasses_independent(self):
        """GeocodeError and RoutingError share a parent but aren't each other."""
        assert not issubclass(GeocodeError, RoutingError)
        assert not issubclass(RoutingError, GeocodeError)

    def test_can_catch_subclass_as_base(self):
        with pytest.raises(EnrichmentError):
            raise GeocodeError("test")

    def test_cause_chain_preserved(self):
        """`raise X from e` should set __cause__ for stack trace."""
        native = ValueError("original")
        try:
            try:
                raise native
            except ValueError as e:
                raise GeocodeError("wrapped") from e
        except GeocodeError as outer:
            assert outer.__cause__ is native
