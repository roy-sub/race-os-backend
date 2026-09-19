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
    # No bundle, so nothing pretends there is one. `elevation_gain_m` in
    # particular: the column's zero is an unmeasured course, not a flat one,
    # and the directory printed "0 m" against every announced race until it
    # came back as null.
    assert coming["provenance"] is None
    assert coming["cutoff_minutes"] is None
    assert coming["elevation_gain_m"] is None


@needs_bundle
def test_the_directory_leads_with_what_a_visitor_can_open(api: TestClient, catalogue: dict) -> None:
    """Showcase, then raceable, then announced — and by date inside each band.

    A directory whose first rows cannot be entered reads as a directory of dead
    ends, however correct its date order is.
    """
    rows = api.get("/api/v1/courses").json()["data"]
    slugs = [row["slug"] for row in rows]
    assert slugs[0] == catalogue["showcase"], "the one map a visitor may open leads"
    assert slugs[1] == catalogue["bundled"], "then the races that can be entered"
    assert slugs[2] == catalogue["coming"], "announced-but-unbuilt rows come last"


@needs_bundle
def test_a_signed_in_athlete_opens_on_a_race_they_can_enter(
    api: TestClient, catalogue: dict, signed_up: dict
) -> None:
    """No showcase once signed in, so the list opens on a raceable row."""
    rows = api.get("/api/v1/courses", headers=signed_up["headers"]).json()["data"]
    assert rows[0]["slug"] == catalogue["bundled"]


@needs_bundle
def test_the_directory_is_ordered_by_date_inside_each_band(
    api: TestClient, catalogue: dict
) -> None:
    """A directory read to answer "which race next?" cannot be alphabetical."""
    rows = api.get("/api/v1/courses").json()["data"]
    coming = [
        row["next_edition_date"]
        for row in rows
        if row["availability"] == "coming_soon" and row["next_edition_date"]
    ]
    assert coming == sorted(coming)


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


# ---------------------------------------------------------------------------
# The season as declared, rather than a synthetic stand-in
# ---------------------------------------------------------------------------
#
# Everything above builds its own courses, so it tests the *rules*. These test
# the *manifest*: that what `catalogue.py` promises is actually on disk and
# actually gated. A catalogue that names a bundle nobody generated is a race
# the directory offers and the product cannot open.


def test_every_available_race_names_a_bundle_that_exists() -> None:
    """`AVAILABLE` is a promise that the course can be planned.

    The seed logs a warning and carries on when a named bundle is missing,
    which is right for a deploy and wrong for a test: the row would ship as
    enterable with no geometry behind it.
    """
    from raceos.db.catalogue import CATALOGUE

    missing = [
        entry.slug
        for entry in CATALOGUE
        if entry.availability is CourseAvailability.AVAILABLE
        and (
            entry.bundle_slug is None
            or not (BUNDLE_DIR / f"{entry.bundle_slug}.bundle.json").is_file()
        )
    ]
    if not BUNDLE_DIR.is_dir():
        pytest.skip("generated bundles are git-ignored build artefacts")
    assert missing == [], f"available races with no generated bundle: {missing}"


def test_a_coming_soon_race_names_no_bundle() -> None:
    """The converse, and the reason `availability` is a stored column.

    A row with course data that is still marked coming-soon is a race the
    product could open and refuses to, which is worse than either state.
    """
    from raceos.db.catalogue import CATALOGUE

    wrong = [
        entry.slug
        for entry in CATALOGUE
        if entry.availability is CourseAvailability.COMING_SOON and entry.bundle_slug is not None
    ]
    assert wrong == []


def test_the_showcase_is_the_only_showcase() -> None:
    """Exactly one, and it is the one the marketing pages name."""
    from raceos.db.catalogue import CATALOGUE, SHOWCASE_SLUG

    showcases = [e.slug for e in CATALOGUE if e.visibility is CourseVisibility.SHOWCASE]
    assert showcases == [SHOWCASE_SLUG]


def test_no_race_that_has_already_been_run_is_still_listed() -> None:
    """The September 2026 events are over; a catalogue is not an archive."""
    from raceos.db.catalogue import CATALOGUE

    retired = {
        "nice-703-world-championship",
        "ironman-wales",
        "belgrade-703",
        "erkner-703",
        "italy-emilia-romagna-full",
        "italy-emilia-romagna-703",
        "weymouth-703",
    }
    assert retired.isdisjoint({e.slug for e in CATALOGUE})


def test_every_catalogue_slug_is_unique() -> None:
    from raceos.db.catalogue import CATALOGUE

    slugs = [e.slug for e in CATALOGUE]
    assert len(slugs) == len(set(slugs))
