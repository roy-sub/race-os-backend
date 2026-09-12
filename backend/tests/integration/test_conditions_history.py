"""What race day has actually been like.

The prototype's conditions panel quoted a median air temperature, a wetsuit
likelihood and a finish-time distribution, and none of the three was backed by
anything. These pin that every figure now comes from an observation, and that
the one that cannot be observed is absent rather than drawn.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import Course, CourseConditionsHistory
from raceos.domain.enums import CourseAvailability
from raceos.ingest.bundle_loader import load_bundle_file
from raceos.services import conditions_service

pytestmark = pytest.mark.integration

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"
TRAMUNTANA = BUNDLE_DIR / "tramuntana-full.bundle.json"
needs_bundle = pytest.mark.skipif(
    not TRAMUNTANA.is_file(), reason="generated bundles are git-ignored build artefacts"
)


@pytest.fixture
def course(api: TestClient, migrated_engine, api_db):
    from sqlalchemy.orm import sessionmaker

    if not TRAMUNTANA.is_file():
        pytest.skip("generated bundles are git-ignored build artefacts")
    with sessionmaker(bind=migrated_engine)() as session:
        load_bundle_file(session, TRAMUNTANA)
        row = session.scalar(select(Course).where(Course.slug == "tramuntana-full"))
        row.availability = CourseAvailability.AVAILABLE
        row.next_edition_date = date(2027, 9, 19)
        session.commit()
    return api_db.scalar(select(Course).where(Course.slug == "tramuntana-full"))


def _observe(api_db, course, *, year: int, air: float, water: float | None, rain: float) -> None:
    api_db.add(
        CourseConditionsHistory(
            course_id=course.id,
            observed_on=date(year, 9, 19),
            observed_hour=7,
            air_temp_c=air,
            humidity_pct=62.0,
            wind_speed_ms=3.4,
            wind_dir_deg=210.0,
            precipitation_mm=rain,
            cloud_cover_pct=20.0,
            water_temp_c=water,
            source=conditions_service.SOURCE_ARCHIVE,
            fetched_at=datetime.now(UTC),
        )
    )


# ---------------------------------------------------------------------------
# What is returned
# ---------------------------------------------------------------------------


@needs_bundle
def test_the_panel_is_free_and_needs_no_account(course, api: TestClient) -> None:
    """Recon is the front door. Putting the conditions behind a wall would make
    the product impossible to evaluate."""
    response = api.get("/api/v1/courses/tramuntana-full/conditions-history")
    assert response.status_code == 200, response.text


@needs_bundle
def test_with_nothing_collected_it_says_so_rather_than_showing_zeroes(
    course, api: TestClient
) -> None:
    body = api.get("/api/v1/courses/tramuntana-full/conditions-history").json()
    assert body["observations"] == []
    assert body["summary"]["observations"] == 0
    assert body["summary"]["median_air_temp_c"] is None
    assert body["empty_reason"] == "not_collected_yet"


@needs_bundle
def test_medians_come_from_observations_and_carry_their_count(
    course, api: TestClient, api_db
) -> None:
    """A median of two years is a different claim from a median of ten, and a
    panel showing the number without the count makes them look the same."""
    for year, air in ((2022, 24.0), (2023, 28.0), (2024, 26.0)):
        _observe(api_db, course, year=year, air=air, water=None, rain=0.0)
    api_db.commit()

    body = api.get("/api/v1/courses/tramuntana-full/conditions-history").json()
    assert body["summary"]["observations"] == 3
    assert body["summary"]["median_air_temp_c"] == 26.0
    assert body["summary"]["warmest_air_temp_c"] == 28.0
    assert body["summary"]["coolest_air_temp_c"] == 24.0
    assert len(body["observations"]) == 3


@needs_bundle
def test_one_freak_year_does_not_move_the_headline(course, api: TestClient, api_db) -> None:
    """Medians rather than means, for exactly this."""
    for year, air in ((2021, 25.0), (2022, 26.0), (2023, 25.5), (2024, 41.0)):
        _observe(api_db, course, year=year, air=air, water=None, rain=0.0)
    api_db.commit()

    summary = api.get("/api/v1/courses/tramuntana-full/conditions-history").json()["summary"]
    # 25.75, reported to a tenth: nobody plans against a hundredth of a degree.
    assert summary["median_air_temp_c"] == 25.8
    # The outlier is still reported, as the range rather than the middle.
    assert summary["warmest_air_temp_c"] == 41.0


# ---------------------------------------------------------------------------
# What is deliberately absent
# ---------------------------------------------------------------------------


@needs_bundle
def test_there_is_no_finish_time_distribution(course, api: TestClient) -> None:
    """It needs actual results, which this system does not have and cannot
    obtain. The prototype drew one anyway."""
    body = api.get("/api/v1/courses/tramuntana-full/conditions-history").json()
    assert body["finish_times_available"] is False
    assert "finish_times" not in body


@needs_bundle
def test_no_water_temperature_means_no_wetsuit_likelihood(course, api: TestClient, api_db) -> None:
    """Estimating it from air temperature would be a number with the shape of
    evidence and none of the substance."""
    for year in (2022, 2023, 2024):
        _observe(api_db, course, year=year, air=27.0, water=None, rain=0.0)
    api_db.commit()

    summary = api.get("/api/v1/courses/tramuntana-full/conditions-history").json()["summary"]
    assert summary["water_observations"] == 0
    assert summary["median_water_temp_c"] is None
    assert summary["wetsuit_legal_fraction"] is None


@needs_bundle
def test_wetsuit_likelihood_where_water_temperature_is_known(
    course, api: TestClient, api_db
) -> None:
    """Computed by running each observed year through the same ruleset a plan
    uses, so the panel and the plan cannot disagree about legality."""
    # Two legal (<= 24.5), two not.
    for year, water in ((2021, 22.0), (2022, 23.5), (2023, 26.0), (2024, 27.0)):
        _observe(api_db, course, year=year, air=27.0, water=water, rain=0.0)
    api_db.commit()

    summary = api.get("/api/v1/courses/tramuntana-full/conditions-history").json()["summary"]
    assert summary["water_observations"] == 4
    assert summary["wetsuit_legal_fraction"] == 0.5


@needs_bundle
def test_rain_at_the_start_is_counted_not_guessed(course, api: TestClient, api_db) -> None:
    for year, rain in ((2022, 0.0), (2023, 1.4), (2024, 0.0), (2021, 0.0)):
        _observe(api_db, course, year=year, air=25.0, water=None, rain=rain)
    api_db.commit()

    summary = api.get("/api/v1/courses/tramuntana-full/conditions-history").json()["summary"]
    assert summary["wet_start_fraction"] == 0.25


# ---------------------------------------------------------------------------
# The backfill
# ---------------------------------------------------------------------------


@needs_bundle
def test_a_course_with_no_announced_edition_has_nothing_to_anchor_to(
    course, api: TestClient, api_db, api_settings
) -> None:
    """ "What is the weather like at this course" with no month in it is a
    question about the climate, not about race day."""
    course.next_edition_date = None
    api_db.commit()

    outcome = conditions_service.backfill_course(
        api_db, course=course, settings=api_settings, client=None, today=date(2026, 9, 12)
    )
    assert outcome["fetched"] == 0
    assert outcome["reason"] == "no announced edition date"

    body = api.get("/api/v1/courses/tramuntana-full/conditions-history").json()
    assert body["empty_reason"] == "no_edition_date"


@needs_bundle
def test_the_backfill_asks_only_for_what_is_missing(course, api_db, api_settings) -> None:
    """A past day's weather does not change, so a stored row is never
    re-fetched. That is what makes the job cheap daily and safe twice."""
    for year in range(2017, 2027):
        _observe(api_db, course, year=year, air=25.0, water=None, rain=0.0)
    api_db.commit()

    outcome = conditions_service.backfill_course(
        api_db, course=course, settings=api_settings, today=date(2026, 9, 12)
    )
    assert outcome["fetched"] == 0
    assert outcome["reason"] == "already complete"


@needs_bundle
def test_an_unreachable_archive_leaves_the_panel_standing(
    course, api: TestClient, api_db, api_settings
) -> None:
    """The suite runs with no network, so this is the real outage path. A
    conditions panel is an improvement to recon, never a precondition."""
    outcome = conditions_service.backfill_course(
        api_db, course=course, settings=api_settings, today=date(2026, 9, 12)
    )
    api_db.commit()
    assert outcome["fetched"] == 0

    assert api.get("/api/v1/courses/tramuntana-full/conditions-history").status_code == 200


@needs_bundle
def test_a_partial_observation_is_skipped_rather_than_stored(course, api_db, api_settings) -> None:
    """A row missing its temperature would read as an observation and be a gap."""

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            # Wind present, temperature absent.
            return {"hourly": {"wind_speed_10m": [3.0] * 24}}

    class _Client:
        def get(self, url: str, params: dict[str, object]) -> _Response:
            return _Response()

    outcome = conditions_service.backfill_course(
        api_db,
        course=course,
        settings=api_settings,
        client=_Client(),  # type: ignore[arg-type]
        today=date(2026, 9, 12),
    )
    assert outcome["fetched"] == 0
    assert api_db.scalar(select(CourseConditionsHistory)) is None


@needs_bundle
def test_the_archive_lag_is_respected(course, api_db, api_settings) -> None:
    """The archive trails real time by about five days. Asking for yesterday
    returns nothing, which reads as a failure rather than as "not yet"."""
    today = date(2026, 9, 12)
    course.next_edition_date = today + timedelta(days=2)
    api_db.commit()

    wanted = conditions_service._target_dates(course, today=today, years=3)
    assert all(
        when <= today - timedelta(days=conditions_service.ARCHIVE_LAG_DAYS) for when in wanted
    )


@needs_bundle
def test_the_conditions_panel_is_hidden_for_a_course_the_directory_hides(
    course, api: TestClient, api_db, signed_up
) -> None:
    """The same visibility rule as the directory, not a second one that could
    drift from it."""
    from raceos.domain.enums import CourseVisibility

    course.visibility = CourseVisibility.RETIRED
    api_db.commit()
    assert api.get("/api/v1/courses/tramuntana-full/conditions-history").status_code == 404
