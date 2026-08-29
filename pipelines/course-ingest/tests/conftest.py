from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = Path(__file__).resolve().parent / "fixtures"

from course_ingest.config import load_config  # noqa: E402
from course_ingest.sources.overture import FixtureRoadSource  # noqa: E402
from course_ingest.sources.terrarium import FixtureDemSource  # noqa: E402
from course_ingest.spec import load_spec  # noqa: E402

FIXTURE_DEM_ZOOM = 12

needs_fixtures = pytest.mark.skipif(
    not (FIXTURES / "roads.json.gz").exists(),
    reason="offline fixtures not built; run `python tests/build_fixtures.py`",
)


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def sources(cfg):
    roads = FixtureRoadSource(FIXTURES / "roads.json.gz")
    # The checked-in DEM slice is coarser than production's z14 so the fixture
    # stays a couple of megabytes; see tests/build_fixtures.py.
    dem = FixtureDemSource(FIXTURES / "dem", sample_zoom=FIXTURE_DEM_ZOOM)
    return roads, dem


@pytest.fixture(scope="session")
def fixture_spec():
    return load_spec(FIXTURES / "test-sprint.yaml")
