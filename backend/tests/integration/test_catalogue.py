"""The declared catalogue, and who sees what in it.

The directory used to be the contents of a build directory. It is a manifest
now, and three things follow from that — each of them a product decision rather
than a technical one, and each tested here:

* An announced event with no course data yet is **listed and not enterable**.
  Hiding fourteen fifteenths of a season misrepresents the season.
* The showcase course is listed to **signed-out visitors only**. It is a tuned
  graphic whose legs disagree on scale by 14.5x, and beside surveyed courses it
  would invite the two to be read as the same kind of thing.
* A course the catalogue no longer names is **retired, not deleted**, because
  deleting one would orphan every plan already solved against it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import Course, Plan, Race
from raceos.domain.enums import CourseAvailability, CourseVisibility
from raceos.ingest.bundle_loader import load_bundle_file

pytestmark = pytest.mark.integration

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"
TRAMUNTANA = BUNDLE_DIR / "tramuntana-full.bundle.json"

needs_bundle = pytest.mark.skipif(
    not TRAMUNTANA.is_file(), reason="generated bundles are git-ignored build artefacts"
)


@pytest.fixture
def catalogue(api: TestClient, api_db):
    """One bundled course, one coming-soon listing, one showcase."""
    if not TRAMUNTANA.is_file():
        pytest.skip("generated bundles are git-ignored build artefacts")
    load_bundle_file(api_db, TRAMUNTANA)
    bundled = api_db.scalar(select(Course).where(Course.slug == "tramuntana-full"))
    bundled.availability = CourseAvailability.AVAILABLE
    bundled.visibility = CourseVisibility.CATALOGUE
    bundled.next_edition_date = date(2026, 9, 20)

    coming = Course(
        slug="coming-soon-703",
        name="IRONMAN 70.3 Coming Soon",
        place="Nowhere, Spain",
        distance_type=bundled.distance_type,
        difficulty=bundled.difficulty,
        timezone="Europe/Madrid",
        lat=41.6,
        lng=2.65,
        availability=CourseAvailability.COMING_SOON,
        visibility=CourseVisibility.CATALOGUE,
        next_edition_date=date(2026, 10, 4),
    )
    showcase = Course(
        slug="showcase-703",
        name="Showcase 70.3",
        place="Kalmar, Sweden",
        distance_type=bundled.distance_type,
        difficulty=bundled.difficulty,
        timezone="Europe/Stockholm",
        lat=56.657,
        lng=16.362,
        availability=CourseAvailability.AVAILABLE,
        visibility=CourseVisibility.SHOWCASE,
    )
    api_db.add_all([coming, showcase])
    api_db.commit()
    return {"bundled": bundled.slug, "coming": coming.slug, "showcase": showcase.slug}


@needs_bundle
def test_a_signed_out_visitor_sees_the_season_and_the_showcase(
    api: TestClient, catalogue: dict
) -> None:
    slugs = {row["slug"] for row in api.get("/api/v1/courses").json()["data"]}
    assert catalogue["bundled"] in slugs
    assert catalogue["coming"] in slugs, "an announced race is listed before its map exists"
    assert catalogue["showcase"] in slugs


@needs_bundle
def test_a_signed_in_athlete_sees_the_season_without_the_showcase(
    api: TestClient, catalogue: dict, signed_up: dict
) -> None:
    slugs = {
        row["slug"]
        for row in api.get("/api/v1/courses", headers=signed_up["headers"]).json()["data"]
    }
    assert catalogue["bundled"] in slugs
    assert catalogue["coming"] in slugs
    assert catalogue["showcase"] not in slugs


@needs_bundle
def test_a_coming_soon_race_carries_its_date_and_says_it_is_not_ready(
    api: TestClient, catalogue: dict
) -> None:
    rows = {row["slug"]: row for row in api.get("/api/v1/courses").json()["data"]}
    coming = rows[catalogue["coming"]]
    assert coming["availability"] == "coming_soon"
    assert coming["next_edition_date"] == "2026-10-04"
    # No bundle, so nothing pretends there is one.
    assert coming["provenance"] is None
    assert coming["cutoff_minutes"] is None


@needs_bundle
def test_the_directory_is_ordered_by_date(api: TestClient, catalogue: dict) -> None:
    """A directory read to answer "which race next?" cannot be alphabetical."""
    dated = [
        row["next_edition_date"]
        for row in api.get("/api/v1/courses").json()["data"]
        if row["next_edition_date"]
    ]
    assert dated == sorted(dated)


@needs_bundle
def test_a_coming_soon_race_cannot_be_entered(
    api: TestClient, catalogue: dict, signed_up: dict
) -> None:
    """Refused with the date, so the answer is "not yet" rather than "no"."""
    response = api.post(
        "/api/v1/races",
        headers=signed_up["headers"],
        json={
            "course_ref": catalogue["coming"],
            "event_date": (datetime.now(UTC).date() + timedelta(days=60)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    assert response.status_code == 409
    message = response.json()["error"]["message"]
    assert "4 October 2026" in message
    assert "add the course yourself" in message


@needs_bundle
def test_the_showcase_cannot_be_entered_as_a_race(
    api: TestClient, catalogue: dict, signed_up: dict
) -> None:
    """It is marketing, not an event. Answered as a missing slug — see `visible_to`."""
    response = api.post(
        "/api/v1/races",
        headers=signed_up["headers"],
        json={
            "course_ref": catalogue["showcase"],
            "event_date": (datetime.now(UTC).date() + timedelta(days=60)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    assert response.status_code == 404


@needs_bundle
def test_an_available_race_can_be_entered(
    api: TestClient, catalogue: dict, signed_up: dict
) -> None:
    """The control: the gate refuses the right rows and nothing else."""
    response = api.post(
        "/api/v1/races",
        headers=signed_up["headers"],
        json={
            "course_ref": catalogue["bundled"],
            "event_date": (datetime.now(UTC).date() + timedelta(days=60)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    assert response.status_code == 201, response.text


@needs_bundle
def test_retiring_a_course_hides_it_without_orphaning_a_plan(
    api: TestClient, catalogue: dict, signed_up: dict, api_db
) -> None:
    """A course with a plan against it is withdrawn, never deleted."""
    entered = api.post(
        "/api/v1/races",
        headers=signed_up["headers"],
        json={
            "course_ref": catalogue["bundled"],
            "event_date": (datetime.now(UTC).date() + timedelta(days=60)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    assert entered.status_code == 201
    race_id = entered.json()["id"]

    course = api_db.scalar(select(Course).where(Course.slug == catalogue["bundled"]))
    course.visibility = CourseVisibility.RETIRED
    api_db.commit()

    slugs = {row["slug"] for row in api.get("/api/v1/courses").json()["data"]}
    assert catalogue["bundled"] not in slugs

    # The athlete's race is untouched, and still names its course.
    race = api.get(f"/api/v1/races/{race_id}", headers=signed_up["headers"]).json()
    assert race["course_name"]
    assert api_db.scalar(select(Race).where(Race.id == race["id"])) is not None
    assert api_db.scalar(select(Plan).where(Plan.race_id == race["id"])) is None
