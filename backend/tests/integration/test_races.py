"""Races, and the demo path they unblock.

Without a way to create a race there is no `race_id`, so the plan builder
cannot start and nothing downstream — solve, race card, exports — can run.
This file walks that whole path as a new signup would.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import Course, CourseBundle, Race
from raceos.ingest.bundle_loader import load_bundle_file
from tests.integration.conftest import buy_plan

pytestmark = pytest.mark.integration

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"
TRAMUNTANA = BUNDLE_DIR / "tramuntana-full.bundle.json"
needs_bundle = pytest.mark.skipif(
    not TRAMUNTANA.is_file(), reason="generated bundles are git-ignored build artefacts"
)

ATHLETE_M = {
    "swim_threshold_pace": 105,
    "bike_threshold_power": 224,
    "run_threshold_pace": 282,
    "weight": 75,
    "sweat_rate": 1.1,
    "sodium_loss": 900,
    "gut_carb_ceiling": 75,
    "caffeine_tolerance": 300,
}


@pytest.fixture
def seeded(api: TestClient, signed_up, migrated_engine, paywall):
    from sqlalchemy.orm import sessionmaker

    if not TRAMUNTANA.is_file():
        pytest.skip("generated bundles are git-ignored build artefacts")
    with sessionmaker(bind=migrated_engine)() as session:
        load_bundle_file(session, TRAMUNTANA)
        session.commit()
    return signed_up["headers"]


def _future(days: int = 40) -> str:
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# The gap this closes
# ---------------------------------------------------------------------------


@needs_bundle
def test_a_new_signup_can_enter_a_race_by_slug(seeded, api: TestClient) -> None:
    """The directory hands out slugs, so a slug must be enough."""
    response = api.post(
        "/api/v1/races",
        headers=seeded,
        json={
            "course_ref": "tramuntana-full",
            "event_date": _future(),
            "start_time_local": "07:00",
            "bib": "1421",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["course_name"] == "Tramuntana Full"
    assert body["course_slug"] == "tramuntana-full"
    assert body["bundle_version"], "the race is pinned to a bundle"
    assert body["days_away"] == 40
    assert body["timezone"]
    assert body["plan_id"] is None, "no plan yet"


@needs_bundle
def test_the_whole_demo_path_runs_from_a_fresh_signup(seeded, api: TestClient) -> None:
    """Signup → race → plan → pay → solve → race card → exports.

    This is the path a client will be walked through. Before races existed it
    could not start at all.
    """
    headers = seeded
    for key, value in ATHLETE_M.items():
        api.put(f"/api/v1/constraints/{key}", headers=headers, json={"value": value})

    race = api.post(
        "/api/v1/races",
        headers=headers,
        json={
            "course_ref": "tramuntana-full",
            "event_date": _future(30),
            "start_time_local": "06:40",
        },
    ).json()

    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": race["id"]})
    assert draft.status_code == 201, draft.text

    buy_plan(api, headers, draft.json()["id"])
    solved = api.post(f"/api/v1/plans/{draft.json()['id']}/solve", headers=headers, json={})
    assert solved.status_code == 200, solved.text
    plan = solved.json()

    # The race card can now print its own heading without a second call.
    assert plan["event_date"] == _future(30)
    assert plan["start_time_local"] == "06:40"
    assert plan["course_name"] == "Tramuntana Full"
    assert plan["attribution"], "ODbL attribution travels with the plan"
    assert plan["splits"]
    assert plan["gates"]
    assert plan["bags"]

    exports = api.get(f"/api/v1/plans/{plan['id']}/export", headers=headers)
    assert exports.status_code == 200
    assert len(exports.json()["exports"]) == 5


@needs_bundle
def test_the_race_list_links_straight_to_an_existing_plan(seeded, api: TestClient) -> None:
    """So the UI offers "open" rather than "start" for a race already planned."""
    headers = seeded
    race = api.post(
        "/api/v1/races",
        headers=headers,
        json={
            "course_ref": "tramuntana-full",
            "event_date": _future(),
            "start_time_local": "07:00",
        },
    ).json()
    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": race["id"]})

    listed = api.get("/api/v1/races", headers=headers).json()
    assert len(listed) == 1
    assert listed[0]["plan_id"] == draft.json()["id"]
    assert listed[0]["plan_status"] == "draft"


@needs_bundle
def test_entering_the_same_race_twice_returns_the_first(seeded, api: TestClient) -> None:
    """A double-submit must not leave a duplicate to clean up."""
    payload = {
        "course_ref": "tramuntana-full",
        "event_date": _future(),
        "start_time_local": "07:00",
    }
    first = api.post("/api/v1/races", headers=seeded, json=payload).json()
    second = api.post("/api/v1/races", headers=seeded, json=payload).json()
    assert first["id"] == second["id"]
    assert len(api.get("/api/v1/races", headers=seeded).json()) == 1


@needs_bundle
def test_a_typo_in_the_year_is_caught(seeded, api: TestClient) -> None:
    response = api.post(
        "/api/v1/races",
        headers=seeded,
        json={
            "course_ref": "tramuntana-full",
            "event_date": "2099-06-21",
            "start_time_local": "07:00",
        },
    )
    assert response.status_code == 422
    assert "check the year" in response.json()["error"]["message"]


@needs_bundle
def test_an_unknown_course_is_a_404(seeded, api: TestClient) -> None:
    response = api.post(
        "/api/v1/races",
        headers=seeded,
        json={"course_ref": "no-such-course", "event_date": _future(), "start_time_local": "07:00"},
    )
    assert response.status_code == 404


@needs_bundle
def test_a_race_with_a_solved_plan_cannot_be_deleted(seeded, api: TestClient) -> None:
    """The race is the only record of which event a solved plan was for."""
    headers = seeded
    for key, value in ATHLETE_M.items():
        api.put(f"/api/v1/constraints/{key}", headers=headers, json={"value": value})
    race = api.post(
        "/api/v1/races",
        headers=headers,
        json={
            "course_ref": "tramuntana-full",
            "event_date": _future(),
            "start_time_local": "07:00",
        },
    ).json()
    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": race["id"]})
    buy_plan(api, headers, draft.json()["id"])
    api.post(f"/api/v1/plans/{draft.json()['id']}/solve", headers=headers, json={})

    assert api.delete(f"/api/v1/races/{race['id']}", headers=headers).status_code == 409


@needs_bundle
def test_an_unplanned_race_can_be_removed(seeded, api: TestClient, api_db) -> None:
    race = api.post(
        "/api/v1/races",
        headers=seeded,
        json={
            "course_ref": "tramuntana-full",
            "event_date": _future(),
            "start_time_local": "07:00",
        },
    ).json()
    assert api.delete(f"/api/v1/races/{race['id']}", headers=seeded).status_code == 204
    assert api_db.get(Race, UUID(race["id"])) is None


@needs_bundle
def test_nobody_sees_another_athletes_races(seeded, api: TestClient) -> None:
    race = api.post(
        "/api/v1/races",
        headers=seeded,
        json={
            "course_ref": "tramuntana-full",
            "event_date": _future(),
            "start_time_local": "07:00",
        },
    ).json()
    other = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "jonas.feldt@example.com",
            "password": "correct-horse-battery",
            "name": "Jonas Feldt",
        },
    ).json()
    intruder = {"Authorization": f"Bearer {other['access_token']}"}

    assert api.get("/api/v1/races", headers=intruder).json() == []
    assert api.get(f"/api/v1/races/{race['id']}", headers=intruder).status_code == 404
    assert api.delete(f"/api/v1/races/{race['id']}", headers=intruder).status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/races"),
        ("GET", "/api/v1/races"),
        ("GET", f"/api/v1/races/{UUID(int=0)}"),
        ("DELETE", f"/api/v1/races/{UUID(int=0)}"),
    ],
)
def test_every_race_endpoint_rejects_an_absent_token(
    api: TestClient, method: str, path: str
) -> None:
    assert api.request(method, path, json={}).status_code == 401


# ---------------------------------------------------------------------------
# Route geometry for the map
# ---------------------------------------------------------------------------


@needs_bundle
def test_recon_returns_real_route_coordinates(seeded, api: TestClient) -> None:
    """A public map must be able to draw the actual course."""
    body = api.get("/api/v1/courses/tramuntana-full/recon").json()

    for leg in body["legs"]:
        coords = leg["coordinates"]
        assert coords, f"{leg['leg']} has no coordinates"
        assert len(coords) <= 601, "downsampled for the browser"
        lng, lat, elev = coords[0]
        # Mallorca: roughly 2-4 E, 39-40 N. Reversed pairs land in Somalia.
        assert 2.0 < lng < 4.5, f"longitude {lng} is not first"
        assert 39.0 < lat < 40.5, f"latitude {lat} out of range"
        assert isinstance(elev, float)


@needs_bundle
def test_downsampling_keeps_the_finish(seeded, api: TestClient, api_db) -> None:
    """A route that stops short of the finish looks like a data error."""
    from raceos.services.course_service import _downsample, _leg_coordinates

    bundle = api_db.scalar(select(CourseBundle))
    course = api_db.scalar(select(Course))
    assert course is not None
    assert bundle is not None
    leg = bundle.legs[0]
    full = _leg_coordinates(leg)
    reduced = _downsample(full, 50)

    assert reduced[0] == full[0]
    assert reduced[-1] == full[-1]
    assert len(reduced) <= 51
