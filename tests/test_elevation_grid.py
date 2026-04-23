"""Elevation grid lookup tests.

Uses a synthetic 9-point grid so tests don't depend on the 1.7 MB
production `us_elevation_grid.npz` (which may not be present on every
developer's machine after a fresh clone).
"""

import numpy as np
import pytest


@pytest.fixture
def tiny_grid(tmp_path, monkeypatch):
    """Build a synthetic 3x3 grid covering a patch of the Rockies.

    Points:        Elev (m):
      (41, -105)     1900          (41, -104)     1700          (41, -103)     1500
      (40, -105)     2000          (40, -104)     1600          (40, -103)     1300
      (39, -105)     1800          (39, -104)     1500          (39, -103)     1400

    After building the file, we monkeypatch elevation_grid.GRID_PATH and
    clear the cached loader so subsequent calls re-read from the tmp file.
    """
    grid_path = tmp_path / "tiny_grid.npz"
    lats = np.array([41, 41, 41, 40, 40, 40, 39, 39, 39], dtype=np.float32)
    lons = np.array([-105, -104, -103, -105, -104, -103, -105, -104, -103], dtype=np.float32)
    elev_m = np.array([1900, 1700, 1500, 2000, 1600, 1300, 1800, 1500, 1400], dtype=np.float32)
    np.savez_compressed(
        grid_path,
        lats=lats,
        lons=lons,
        elev_m=elev_m,
        spacing_mi=np.float32(69),
    )

    import elevation_grid

    monkeypatch.setattr(elevation_grid, "GRID_PATH", grid_path)
    # st.cache_resource caches the loaded grid per session. We must clear
    # it so the next _load_grid call re-reads from our monkeypatched path.
    elevation_grid._load_grid.clear()
    yield grid_path
    elevation_grid._load_grid.clear()


class TestIsAvailable:
    def test_true_when_file_exists(self, tiny_grid):
        from elevation_grid import is_available

        assert is_available() is True


class TestLookupM:
    def test_returns_exact_cell_elev_at_grid_point(self, tiny_grid):
        """Querying an exact grid point returns that point's elevation."""
        from elevation_grid import lookup_m

        # (40, -104) is the center of the grid, elev 1600
        assert lookup_m(40.0, -104.0) == pytest.approx(1600.0)

    def test_nearest_neighbor_semantics(self, tiny_grid):
        """A point slightly off should snap to the nearest grid cell."""
        from elevation_grid import lookup_m

        # (40.01, -104.01) is very near (40, -104) → should still return 1600
        assert lookup_m(40.01, -104.01) == pytest.approx(1600.0)

    def test_returns_float(self, tiny_grid):
        from elevation_grid import lookup_m

        result = lookup_m(40.0, -104.0)
        assert isinstance(result, float)


class TestLookupMBatch:
    def test_length_matches_input(self, tiny_grid):
        from elevation_grid import lookup_m_batch

        lats = np.array([40.0, 41.0, 39.0])
        lons = np.array([-104.0, -105.0, -103.0])
        result = lookup_m_batch(lats, lons)
        assert result is not None
        assert len(result) == len(lats)

    def test_values_match_single_lookups(self, tiny_grid):
        """lookup_m_batch should agree with lookup_m row-for-row."""
        from elevation_grid import lookup_m, lookup_m_batch

        lats = np.array([40.0, 41.0, 39.0])
        lons = np.array([-104.0, -105.0, -103.0])
        batch = lookup_m_batch(lats, lons)
        assert batch is not None
        expected = np.array([lookup_m(la, lo) for la, lo in zip(lats, lons)])
        np.testing.assert_allclose(batch, expected)

    def test_empty_input_returns_empty(self, tiny_grid):
        from elevation_grid import lookup_m_batch

        out = lookup_m_batch(np.array([]), np.array([]))
        assert out is not None
        assert len(out) == 0


class TestGridMissing:
    def test_lookup_m_returns_none_when_file_missing(self, tmp_path, monkeypatch):
        """If the .npz doesn't exist, lookup should return None rather than crash."""
        import elevation_grid

        monkeypatch.setattr(elevation_grid, "GRID_PATH", tmp_path / "does_not_exist.npz")
        elevation_grid._load_grid.clear()
        try:
            assert elevation_grid.lookup_m(40.0, -104.0) is None
            assert elevation_grid.is_available() is False
        finally:
            elevation_grid._load_grid.clear()
