"""The race-week checklist.

The dates were always derivable — the ICS export derives them from the event
date rather than from weekday names — but a calendar event cannot be ticked
off and nothing was stored.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import Course, Race
from raceos.domain.enums import CourseAvailability
from raceos.ingest.bundle_loader import load_bundle_file

pytestmark = pytest.mark.integration

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"
TRAMUNTANA = BUNDLE_DIR / "tramuntana-full.bundle.json"
needs_bundle = pytest.mark.skipif(
    not TRAMUNTANA.is_file(), reason="generated bundles are git-ignored build artefacts"
)


@pytest.fixture
def seeded(api: TestClient, migrated_engine):
    from sqlalchemy.orm import sessionmaker

    if not TRAMUNTANA.is_file():
        pytest.skip("generated bundles are git-ignored build artefacts")
    with sessionmaker(bind=migrated_engine)() as session:
        load_bundle_file(session, TRAMUNTANA)
        session.scalar(
            select(Course).where(Course.slug == "tramuntana-full")
        ).availability = CourseAvailability.AVAILABLE
        session.commit()


def _enter(api: TestClient, headers, *, days_away: int) -> str:
    created = api.post(
        "/api/v1/races",
        headers=headers,
        json={
            "course_ref": "tramuntana-full",
            "event_date": (datetime.now(UTC).date() + timedelta(days=days_away)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    assert created.status_code in (200, 201), created.text
    return str(created.json()["id"])


@needs_bundle
def test_a_new_race_already_has_a_checklist(seeded, api: TestClient, signed_up) -> None:
    """Generated on read, so a race entered five minutes ago has one — rather
    than waiting for a cron that may not run before race week."""
    race_id = _enter(api, signed_up["headers"], days_away=5)

    body = api.get(f"/api/v1/races/{race_id}/race-week", headers=signed_up["headers"])
    assert body.status_code == 200, body.text
    tasks = body.json()["tasks"]
    assert {t["key"] for t in tasks} >= {"registration", "pack_bags", "bike_check_in", "race_day"}
    assert all(t["generated"] for t in tasks)
    assert body.json()["remaining"] == len(tasks)


@needs_bundle
def test_the_dates_come_from_the_event_date_not_from_weekday_names(
    seeded, api: TestClient, signed_up, api_db
) -> None:
    """A Tuesday race gets a Saturday check-in. Storing "Thursday" would be
    wrong for every race not held on a Sunday."""
    race_id = _enter(api, signed_up["headers"], days_away=9)
    race = api_db.get(Race, UUID(race_id))

    tasks = {
        t["key"]: date.fromisoformat(t["due_date"])
        for t in api.get(
            f"/api/v1/races/{race_id}/race-week", headers=signed_up["headers"]
        ).json()["tasks"]
    }
    assert tasks["race_day"] == race.event_date
    assert tasks["bike_check_in"] == race.event_date - timedelta(days=1)
    assert tasks["pack_bags"] == race.event_date - timedelta(days=2)
    assert tasks["registration"] == race.event_date - timedelta(days=3)


@needs_bundle
def test_the_calendar_and_the_checklist_cannot_disagree(seeded, api: TestClient) -> None:
    """One derivation behind both. Two lists of the same week disagree, and a
    task ticked off on one screen that is still open on the other is how
    somebody misses a bag hand-in."""
    from raceos.exports.files import race_week_events, race_week_items

    event_date = date(2026, 9, 19)
    events = race_week_events(
        event_date=event_date, course_name="Tramuntana", has_special_needs=True
    )
    items = race_week_items(has_special_needs=True)

    assert len(events) == len(items)
    assert [e.on_date for e in events] == [
        event_date - timedelta(days=i.days_before) for i in items
    ]


@needs_bundle
def test_regenerating_never_un_ticks_something(seeded, api: TestClient, signed_up) -> None:
    """The tick is the athlete's. A rebuild refreshes dates and copy, because
    a race can be re-dated — it has no business touching what they did."""
    headers = signed_up["headers"]
    race_id = _enter(api, headers, days_away=6)
    tasks = api.get(f"/api/v1/races/{race_id}/race-week", headers=headers).json()["tasks"]
    target = next(t for t in tasks if t["key"] == "registration")

    ticked = api.patch(
        f"/api/v1/races/race-week/tasks/{target['id']}", headers=headers, json={"completed": True}
    )
    assert ticked.status_code == 200, ticked.text
    assert ticked.json()["completed_at"] is not None

    # Read again, which regenerates.
    again = api.get(f"/api/v1/races/{race_id}/race-week", headers=headers).json()
    still = next(t for t in again["tasks"] if t["key"] == "registration")
    assert still["completed_at"] is not None
    assert again["remaining"] == len(again["tasks"]) - 1


@needs_bundle
def test_re_dating_a_race_moves_the_derived_tasks(
    seeded, api: TestClient, signed_up, api_db
) -> None:
    headers = signed_up["headers"]
    race_id = _enter(api, headers, days_away=6)
    api.get(f"/api/v1/races/{race_id}/race-week", headers=headers)

    moved_to = (datetime.now(UTC).date() + timedelta(days=13)).isoformat()
    assert (
        api.patch(
            f"/api/v1/races/{race_id}", headers=headers, json={"event_date": moved_to}
        ).status_code
        == 200
    )

    tasks = api.get(f"/api/v1/races/{race_id}/race-week", headers=headers).json()["tasks"]
    race_day = next(t for t in tasks if t["key"] == "race_day")
    assert race_day["due_date"] == moved_to


@needs_bundle
def test_an_athlete_can_add_their_own_task(seeded, api: TestClient, signed_up) -> None:
    headers = signed_up["headers"]
    race_id = _enter(api, headers, days_away=8)
    due = (datetime.now(UTC).date() + timedelta(days=4)).isoformat()

    created = api.post(
        f"/api/v1/races/{race_id}/race-week/tasks",
        headers=headers,
        json={"title": "Collect hire car", "due_date": due},
    )
    assert created.status_code == 201, created.text
    assert created.json()["generated"] is False

    tasks = api.get(f"/api/v1/races/{race_id}/race-week", headers=headers).json()["tasks"]
    # One list, in date order — not the derived ones and then the personal ones.
    dates = [t["due_date"] for t in tasks]
    assert dates == sorted(dates)
    assert any(t["title"] == "Collect hire car" for t in tasks)


@needs_bundle
def test_a_personal_task_survives_regeneration(seeded, api: TestClient, signed_up) -> None:
    headers = signed_up["headers"]
    race_id = _enter(api, headers, days_away=8)
    due = (datetime.now(UTC).date() + timedelta(days=4)).isoformat()
    api.post(
        f"/api/v1/races/{race_id}/race-week/tasks",
        headers=headers,
        json={"title": "Collect hire car", "due_date": due},
    )

    for _ in range(3):
        tasks = api.get(f"/api/v1/races/{race_id}/race-week", headers=headers).json()["tasks"]
    assert sum(1 for t in tasks if t["title"] == "Collect hire car") == 1


@needs_bundle
def test_a_derived_task_cannot_be_deleted(seeded, api: TestClient, signed_up) -> None:
    """Removing "bike check-in" because it is inconvenient is not something
    the checklist should help with. Ticking it off is."""
    headers = signed_up["headers"]
    race_id = _enter(api, headers, days_away=8)
    tasks = api.get(f"/api/v1/races/{race_id}/race-week", headers=headers).json()["tasks"]
    derived = next(t for t in tasks if t["generated"])

    refused = api.delete(f"/api/v1/races/race-week/tasks/{derived['id']}", headers=headers)
    assert refused.status_code == 409


@needs_bundle
def test_an_athlete_can_delete_their_own(seeded, api: TestClient, signed_up) -> None:
    headers = signed_up["headers"]
    race_id = _enter(api, headers, days_away=8)
    created = api.post(
        f"/api/v1/races/{race_id}/race-week/tasks",
        headers=headers,
        json={
            "title": "Collect hire car",
            "due_date": (datetime.now(UTC).date() + timedelta(days=4)).isoformat(),
        },
    )
    assert (
        api.delete(
            f"/api/v1/races/race-week/tasks/{created.json()['id']}", headers=headers
        ).status_code
        == 204
    )


@needs_bundle
def test_a_task_due_after_race_day_is_refused(seeded, api: TestClient, signed_up) -> None:
    """Race week ends on race day. A task due after it would never be shown."""
    headers = signed_up["headers"]
    race_id = _enter(api, headers, days_away=8)
    response = api.post(
        f"/api/v1/races/{race_id}/race-week/tasks",
        headers=headers,
        json={
            "title": "Celebrate",
            "due_date": (datetime.now(UTC).date() + timedelta(days=20)).isoformat(),
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["field"] == "due_date"


@needs_bundle
def test_the_strip_is_hidden_until_it_is_worth_showing(
    seeded, api: TestClient, signed_up
) -> None:
    """Beyond three weeks out the answer to every item is "not yet"."""
    far = _enter(api, signed_up["headers"], days_away=120)
    near = _enter(api, signed_up["headers"], days_away=4)

    assert (
        api.get(f"/api/v1/races/{far}/race-week", headers=signed_up["headers"]).json()["visible"]
        is False
    )
    assert (
        api.get(f"/api/v1/races/{near}/race-week", headers=signed_up["headers"]).json()["visible"]
        is True
    )


@needs_bundle
def test_days_away_is_computed_so_the_client_does_no_date_arithmetic(
    seeded, api: TestClient, signed_up
) -> None:
    race_id = _enter(api, signed_up["headers"], days_away=5)
    tasks = api.get(
        f"/api/v1/races/{race_id}/race-week", headers=signed_up["headers"]
    ).json()["tasks"]
    race_day = next(t for t in tasks if t["key"] == "race_day")
    assert race_day["days_away"] == 5


@needs_bundle
def test_nobody_reads_or_edits_another_athletes_checklist(
    seeded, api: TestClient, signed_up
) -> None:
    headers = signed_up["headers"]
    race_id = _enter(api, headers, days_away=8)
    tasks = api.get(f"/api/v1/races/{race_id}/race-week", headers=headers).json()["tasks"]

    other = api.post(
        "/api/v1/auth/signup",
        json={"email": "stranger.week@example.com", "password": "correct-horse-battery-42"},
    )
    intruder = {"Authorization": f"Bearer {other.json()['access_token']}"}

    assert api.get(f"/api/v1/races/{race_id}/race-week", headers=intruder).status_code == 404
    assert (
        api.patch(
            f"/api/v1/races/race-week/tasks/{tasks[0]['id']}",
            headers=intruder,
            json={"completed": True},
        ).status_code
        == 404
    )


def test_the_race_week_endpoints_reject_an_absent_token(api: TestClient) -> None:
    import uuid as _uuid

    assert api.get(f"/api/v1/races/{_uuid.uuid4()}/race-week").status_code == 401
