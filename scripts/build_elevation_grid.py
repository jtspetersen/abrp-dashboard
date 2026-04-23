"""Build a local elevation grid for the lower 48 states.

One-time generator. Produces `data/us_elevation_grid.npz` with three
float32 arrays — `lats`, `lons`, `elev_m` — all the same length. At
runtime `elevation_grid.py` loads this file, builds a KDTree over
(lat, lon), and answers nearest-neighbor elevation queries locally.

Source: **open-elevation.com** free public API. No key required, large
batch sizes (1,000+ points per call), negligible rate-limiting for
reasonable use. We tried Open-Meteo first but hit aggressive 429s even
on the first batch — open-elevation answers the same question without
the throttle.

Grid spec:
  - lower 48 + ~50 mi buffer
  - lat range [24.0, 49.5] (southern FL to northern ND/MT)
  - lon range [-125.0, -66.5] (Pacific to Atlantic coasts)
  - 10-mile spacing by default (~57k cells, ~2 min build). Pass
    `--spacing 5` for higher resolution (~225k cells, ~8 min build).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

OPEN_ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"
BATCH_SIZE = 1000  # open-elevation happily takes 1000 points per call
REQUEST_TIMEOUT = 60
BATCH_SLEEP = 0.25
# Backoff windows for 429 rate limit + 5xx server errors. open-elevation
# occasionally returns 504 Gateway Timeout under sustained load — wait
# progressively longer, up to ~5 minutes before declaring the batch dead
# and bailing out to a checkpoint.
RATE_LIMIT_SLEEP = 30.0
SERVER_ERROR_BACKOFF = [30, 60, 120, 180, 300]
MAX_RETRIES = len(SERVER_ERROR_BACKOFF)
# Save progress to disk every N batches so a mid-build 504 doesn't throw
# away an hour of work.
CHECKPOINT_EVERY_N_BATCHES = 25

# Lower 48 bounding box (generous buffer at all edges).
LAT_MIN, LAT_MAX = 24.0, 49.5
LON_MIN, LON_MAX = -125.0, -66.5


def _build_grid_points(spacing_mi: float) -> tuple[np.ndarray, np.ndarray]:
    """Return 1D arrays of (lat, lon) covering the lower-48 bbox."""
    # 1 deg lat = ~69 mi (near-constant). lon miles-per-degree varies with
    # latitude but we use a constant step derived from cos(lat=37) to keep
    # the output a regular rectangular grid — simpler + small file size
    # cost relative to variable-step.
    lat_step = spacing_mi / 69.0
    lon_step = spacing_mi / (69.0 * np.cos(np.radians(37.0)))

    lats_axis = np.arange(LAT_MIN, LAT_MAX + lat_step / 2, lat_step)
    lons_axis = np.arange(LON_MIN, LON_MAX + lon_step / 2, lon_step)
    lat_mesh, lon_mesh = np.meshgrid(lats_axis, lons_axis, indexing="ij")
    return lat_mesh.flatten(), lon_mesh.flatten()


def _fetch_batch(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Fetch elevations for up to BATCH_SIZE coord pairs. Returns float32 array.

    Posts a JSON body with all locations, gets back a matching-order
    result array. Retries on transient errors with escalating backoff.
    On HTTP 5xx (gateway timeouts, bad gateways), waits progressively
    longer — open-elevation.com often recovers after a 2-3 minute
    cooldown even when several consecutive requests fail.
    """
    body = json.dumps(
        {
            "locations": [
                {"latitude": float(lat), "longitude": float(lon)} for lat, lon in zip(lats, lons)
            ]
        }
    ).encode("utf-8")

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(
                OPEN_ELEVATION_URL,
                data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read())
            results = data.get("results")
            if not isinstance(results, list) or len(results) != len(lats):
                raise ValueError(
                    f"open-elevation returned "
                    f"{len(results) if isinstance(results, list) else 'non-list'} "
                    f"results for {len(lats)} points"
                )
            elevs = [float(r.get("elevation") or 0.0) for r in results]
            return np.array(elevs, dtype=np.float32)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                sleep_s = RATE_LIMIT_SLEEP
                reason = "429 rate limit"
            elif 500 <= e.code < 600:
                sleep_s = SERVER_ERROR_BACKOFF[min(attempt, MAX_RETRIES - 1)]
                reason = f"HTTP {e.code} server error"
            else:
                sleep_s = 2**attempt
                reason = f"HTTP {e.code}"
            if attempt + 1 < MAX_RETRIES:
                print(
                    f"    [{reason}, sleeping {sleep_s:.0f}s (retry {attempt + 1}/{MAX_RETRIES})]",
                    flush=True,
                )
                time.sleep(sleep_s)
        except (urllib.error.URLError, ValueError) as e:
            last_err = e
            if attempt + 1 < MAX_RETRIES:
                sleep_s = 2**attempt
                print(f"    [{type(e).__name__}, sleeping {sleep_s}s]", flush=True)
                time.sleep(sleep_s)
    raise RuntimeError(f"elevation fetch failed after {MAX_RETRIES} retries: {last_err}")


def _partial_path(out_path: Path) -> Path:
    return out_path.with_name(out_path.stem + ".partial.npz")


def _try_resume(
    out_path: Path,
    lats_expected: np.ndarray,
    spacing_mi: float,
) -> tuple[np.ndarray | None, int]:
    """Try to resume from a previous partial build. Returns (elev_all, start_idx).

    Discards the checkpoint if grid shape or spacing doesn't match — that
    means the user is rebuilding at a different resolution.
    """
    partial = _partial_path(out_path)
    if not partial.exists():
        return None, 0
    try:
        data = np.load(partial)
        if len(data["lats"]) != len(lats_expected):
            print(
                f"  partial has {len(data['lats'])} cells, expected {len(lats_expected)} — discarding"
            )
            partial.unlink()
            return None, 0
        if float(data.get("spacing_mi", -1)) != float(spacing_mi):
            print("  partial has different spacing — discarding")
            partial.unlink()
            return None, 0
        elev_all = data["elev_m"].astype(np.float32)
        nan_mask = np.isnan(elev_all)
        if not nan_mask.any():
            print(f"  partial is already complete ({len(elev_all)} cells)")
            return elev_all, len(elev_all)
        # Resume from the first NaN, aligned to a batch boundary.
        first_nan = int(nan_mask.argmax())
        start_idx = (first_nan // BATCH_SIZE) * BATCH_SIZE
        done = len(elev_all) - int(nan_mask.sum())
        print(
            f"  resuming from cell {start_idx:,} "
            f"({done:,} cells already fetched, {int(nan_mask.sum()):,} remaining)"
        )
        return elev_all, start_idx
    except Exception as e:
        print(f"  couldn't read partial ({type(e).__name__}: {e}) — starting fresh")
        return None, 0


def _save_partial(
    out_path: Path,
    lats: np.ndarray,
    lons: np.ndarray,
    elev_all: np.ndarray,
    spacing_mi: float,
) -> None:
    # Direct write — np.savez_compressed appends .npz to the path if it
    # doesn't already end in it, which makes an atomic tmp+rename dance
    # awkward to get right. If the process dies mid-write the partial
    # is discarded on next resume (shape/spacing mismatch catches corruption
    # via np.load raising), and we just start over.
    partial = _partial_path(out_path)
    np.savez_compressed(
        partial,
        lats=lats.astype(np.float32),
        lons=lons.astype(np.float32),
        elev_m=elev_all,
        spacing_mi=np.float32(spacing_mi),
    )


def build(spacing_mi: float, out_path: Path) -> None:
    lats_all, lons_all = _build_grid_points(spacing_mi)
    n = len(lats_all)
    total_batches = (n + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"Grid: {spacing_mi} mi spacing, {n:,} cells")
    print(f"Batches: {total_batches} × {BATCH_SIZE}")

    # Try to resume from a previous partial build first.
    elev_all, start_idx = _try_resume(out_path, lats_all, spacing_mi)
    if elev_all is None:
        elev_all = np.full(n, np.nan, dtype=np.float32)
        start_idx = 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    cells_fetched_this_run = 0

    try:
        for start in range(start_idx, n, BATCH_SIZE):
            end = min(start + BATCH_SIZE, n)
            batch_lats = lats_all[start:end]
            batch_lons = lons_all[start:end]
            elev_all[start:end] = _fetch_batch(batch_lats, batch_lons)
            cells_fetched_this_run += end - start

            done_batches = (end + BATCH_SIZE - 1) // BATCH_SIZE
            if done_batches % 5 == 0 or end == n:
                elapsed = time.time() - t_start
                rate = cells_fetched_this_run / elapsed if elapsed > 0 else 0
                eta = (n - end) / rate if rate > 0 else 0
                print(
                    f"  {end:>7,}/{n:,} cells "
                    f"({done_batches}/{total_batches} batches) "
                    f"· {elapsed:.0f}s elapsed · ETA {eta:.0f}s",
                    flush=True,
                )

            # Periodic checkpoint — lets a crash / 504 / keyboard-interrupt
            # resume cleanly next run.
            if done_batches % CHECKPOINT_EVERY_N_BATCHES == 0 and end < n:
                _save_partial(out_path, lats_all, lons_all, elev_all, spacing_mi)

            if end < n:
                time.sleep(BATCH_SLEEP)
    except Exception as e:
        # Save whatever we've got before propagating the error.
        print(f"\nBuild interrupted ({type(e).__name__}: {e}) — saving partial", flush=True)
        _save_partial(out_path, lats_all, lons_all, elev_all, spacing_mi)
        raise

    # Successful completion — write the final file, remove the partial.
    np.savez_compressed(
        out_path,
        lats=lats_all.astype(np.float32),
        lons=lons_all.astype(np.float32),
        elev_m=elev_all,
        spacing_mi=np.float32(spacing_mi),
    )
    partial = _partial_path(out_path)
    if partial.exists():
        partial.unlink()
    size_kb = out_path.stat().st_size / 1024
    print(f"Wrote {out_path} ({size_kb:.1f} KB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spacing",
        type=float,
        default=10.0,
        help="Grid spacing in miles (default 10). Try 5 for higher quality, slower build.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "us_elevation_grid.npz",
        help="Output .npz path (default data/us_elevation_grid.npz)",
    )
    args = parser.parse_args()
    build(args.spacing, args.out)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
