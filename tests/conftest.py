"""Shared pytest fixtures.

The tests here assume the repo layout — `pytest.ini` sets
`pythonpath = .` so imports like `from parser import parse_abrp` resolve
against the project root.
"""

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
SAMPLE_XLSX = PROJECT_ROOT / "data" / "sample_trip.xlsx"


@pytest.fixture
def sample_xlsx_path():
    """Path to the bundled sample trip export."""
    return SAMPLE_XLSX


@pytest.fixture
def parsed_sample(sample_xlsx_path):
    """Parsed waypoints dataframe from the sample xlsx."""
    from parser import parse_abrp

    return parse_abrp(sample_xlsx_path)


@pytest.fixture
def enriched_sample(parsed_sample):
    """Parsed + enriched sample (battery 75 kWh, rate $0.45/kWh — plan defaults)."""
    from calculations import enrich

    return enrich(parsed_sample, 75.0, 0.45)
