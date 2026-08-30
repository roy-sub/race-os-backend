"""Dashboard, My Plans and the notification inbox, end to end.

Real races, real solved plans, real notification rows. Nothing on these
screens is allowed to be a plausible-looking constant, so the assertions check
that each figure came from the row it claims to describe.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import Course, CourseBundle, Notification, Race, User
from raceos.domain.enums import (
    CRITICAL_NOTIFICATION_TYPES,
    NotificationSeverity,
    NotificationType,
    RaceStatus,
)
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
def athlete(api: TestClient, signed_up, migrated_engine, api_db, paywall):
    """An athlete with constraints, a seeded course, and no races yet."""
    from sqlalchemy.orm import sessionmaker

    if not TRAMUNTANA.is_file():
        pytest.skip("generated bundles are git-ignored build artefacts")

    with sessionmaker(bind=migrated_engine)() as session:
        load_bundle_file(session, TRAMUNTANA)
        session.commit()
        course = session.scalar(select(Course).where(Course.slug == "tramuntana-full"))
        bundle = session.scalar(select(CourseBundle))
        course_id, bundle_id = course.id, bundle.id

    headers = signed_up["headers"]
    for key, value in ATHLETE_M.items():
        api.put(f"/api/v1/constraints/{key}", headers=headers, json={"value": value})

    user = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    return {
        "headers": headers,
        "user_id": user.id,
        "course_id": course_id,
        "bundle_id": bundle_id,
    }


def _add_race(api_db, athlete, *, days_out: int, status=RaceStatus.UPCOMING) -> Race:
    race = Race(
        user_id=athlete["user_id"],
        course_id=athlete["course_id"],
        course_bundle_id=athlete["bundle_id"],
        event_date=datetime.now(UTC).date() + timedelta(days=days_out),
        start_time_local=time(7, 0),
        status=status,
    )
    api_db.add(race)
    api_db.commit()
    return race


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------


@needs_bundle
def test_an_athlete_with_no_races_gets_an_honest_empty_dashboard(athlete, api: TestClient) -> None:
    body = api.get("/api/v1/dashboard", headers=athlete["headers"]).json()

    assert body["races"] == []
    assert body["next_race"] is None
    assert body["counts"]["upcoming"] == 0
    assert body["athlete"]["name"] == "Elena Marsh"


@needs_bundle
def test_the_next_race_is_the_soonest_and_matches_the_season_list(
    athlete, api: TestClient, api_db
) -> None:
    """One read, so the two cannot disagree."""
    _add_race(api_db, athlete, days_out=90)
    soonest = _add_race(api_db, athlete, days_out=12)
    _add_race(api_db, athlete, days_out=200)

    body = api.get("/api/v1/dashboard", headers=athlete["headers"]).json()

    assert body["next_race"]["race_id"] == str(soonest.id)
    assert body["races"][0]["race_id"] == str(soonest.id)
    assert [race["days_away"] for race in body["races"]] == sorted(
        race["days_away"] for race in body["races"]
    )


@needs_bundle
def test_an_unsolved_race_says_so_rather_than_inventing_a_time(
    athlete, api: TestClient, api_db
) -> None:
    """A plausible figure the athlete cannot distinguish from a real one is
    worse than an honest gap."""
    _add_race(api_db, athlete, days_out=30)
    card = api.get("/api/v1/dashboard", headers=athlete["headers"]).json()["races"][0]

    assert card["plan_id"] is None
    assert card["projected_minutes"] is None
    assert card["projected_label"] is None
    assert card["feasibility"] == "NOT_SOLVED"
    assert card["next_action"] == "Start a plan"


@needs_bundle
def test_a_solved_race_reports_the_numbers_from_its_own_plan(
    athlete, api: TestClient, api_db
) -> None:
    race = _add_race(api_db, athlete, days_out=30)
    headers = athlete["headers"]
    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": str(race.id)})
    buy_plan(api, headers, draft.json()["id"])
    plan = api.post(f"/api/v1/plans/{draft.json()['id']}/solve", headers=headers, json={}).json()

    card = api.get("/api/v1/dashboard", headers=headers).json()["races"][0]

    assert card["plan_id"] == plan["id"]
    assert card["plan_version"] == plan["version"]
    assert card["projected_minutes"] == plan["projected_minutes"]
    assert card["projected_label"] == plan["projected_label"]
    assert card["feasibility"] == plan["feasibility"]
    assert card["bundle_version"], "the card names the geometry it was solved on"
    assert card["next_action"] == "Open plan"


@needs_bundle
def test_race_week_changes_what_the_card_asks_for(athlete, api: TestClient, api_db) -> None:
    race = _add_race(api_db, athlete, days_out=4)
    headers = athlete["headers"]
    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": str(race.id)})
    buy_plan(api, headers, draft.json()["id"])
    api.post(f"/api/v1/plans/{draft.json()['id']}/solve", headers=headers, json={})

    card = api.get("/api/v1/dashboard", headers=headers).json()["races"][0]
    assert card["is_race_week"] is True
    assert card["next_action"] == "Print the race card and pack"


@needs_bundle
def test_a_pending_drift_event_surfaces_with_what_moved(athlete, api: TestClient, api_db) -> None:
    """The summary is derived from the stored deltas, so it cannot contradict
    them."""
    from raceos.db.models import PlanDriftEvent
    from raceos.domain.enums import DriftCause, DriftSeverity

    race = _add_race(api_db, athlete, days_out=20)
    headers = athlete["headers"]
    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": str(race.id)})
    buy_plan(api, headers, draft.json()["id"])
    plan = api.post(f"/api/v1/plans/{draft.json()['id']}/solve", headers=headers, json={}).json()

    api_db.add(
        PlanDriftEvent(
            plan_id=UUID(plan["id"]),
            detected_at=datetime.now(UTC),
            cause=DriftCause.FORECAST,
            severity=DriftSeverity.NORMAL,
            field_deltas=[{"key": "bike_watts", "label": "Bike", "from": "214 w", "to": "208 w"}],
        )
    )
    api_db.commit()

    card = api.get("/api/v1/dashboard", headers=headers).json()["races"][0]
    assert card["has_pending_drift"] is True
    assert "214 w" in card["drift_summary"]
    assert "208 w" in card["drift_summary"]
    assert card["next_action"] == "Review what changed"
    assert api.get("/api/v1/dashboard", headers=headers).json()["counts"]["needs_review"] == 1


# ---------------------------------------------------------------------------
# My Plans
# ---------------------------------------------------------------------------


@needs_bundle
def test_my_plans_groups_active_draft_and_past(athlete, api: TestClient, api_db) -> None:
    headers = athlete["headers"]
    solved_race = _add_race(api_db, athlete, days_out=40)
    _add_race(api_db, athlete, days_out=60)
    _add_race(api_db, athlete, days_out=-30, status=RaceStatus.COMPLETED)

    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": str(solved_race.id)})
    buy_plan(api, headers, draft.json()["id"])
    api.post(f"/api/v1/plans/{draft.json()['id']}/solve", headers=headers, json={})

    body = api.get("/api/v1/my-plans", headers=headers).json()
    assert len(body["active"]) == 1
    assert body["active"][0]["race_id"] == str(solved_race.id)
    assert len(body["draft"]) == 1, "a race with no plan belongs under draft"
    assert len(body["past"]) == 1


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def _notify(session, user_id, settings, **kwargs):
    from raceos.db.models import User as UserModel
    from raceos.services import notification_service

    user = session.get(UserModel, user_id)
    defaults = {
        "type_key": NotificationType.BUNDLE,
        "severity": NotificationSeverity.INFO,
        "title": "The organiser moved the bike cut-off 20 minutes earlier.",
        "body": "Bundle v2026.3 changes one barrier on your September race.",
        "tag": "COURSE BUNDLE",
    }
    return notification_service.notify(
        session, user=user, settings=settings, **{**defaults, **kwargs}
    )


@needs_bundle
def test_the_inbox_is_paginated_and_queryable(
    athlete, api: TestClient, api_db, api_settings
) -> None:
    """A real resource, not a toast that has already vanished."""
    for index in range(7):
        _notify(
            api_db,
            athlete["user_id"],
            api_settings,
            title=f"Notice {index}",
            type_key=NotificationType.WEEK if index % 2 else NotificationType.BUNDLE,
        )
    api_db.commit()

    first = api.get("/api/v1/notifications?limit=3", headers=athlete["headers"]).json()
    assert first["total"] == 7
    assert first["unread"] == 7
    assert len(first["data"]) == 3

    second = api.get("/api/v1/notifications?limit=3&offset=3", headers=athlete["headers"]).json()
    assert {row["id"] for row in first["data"]}.isdisjoint({row["id"] for row in second["data"]})

    filtered = api.get("/api/v1/notifications?type=week", headers=athlete["headers"]).json()
    assert filtered["total"] == 3
    assert {row["type_key"] for row in filtered["data"]} == {"week"}


@needs_bundle
def test_marking_read_moves_the_unread_count(
    athlete, api: TestClient, api_db, api_settings
) -> None:
    for _ in range(3):
        _notify(api_db, athlete["user_id"], api_settings)
    api_db.commit()

    headers = athlete["headers"]
    inbox = api.get("/api/v1/notifications", headers=headers).json()
    api.post(f"/api/v1/notifications/{inbox['data'][0]['id']}/read", headers=headers)
    assert api.get("/api/v1/notifications", headers=headers).json()["unread"] == 2

    assert api.post("/api/v1/notifications/read-all", headers=headers).json()["marked"] == 2
    assert api.get("/api/v1/notifications", headers=headers).json()["unread"] == 0


@needs_bundle
def test_the_structured_deltas_are_stored_beside_the_prose(
    athlete, api: TestClient, api_db, api_settings
) -> None:
    """The body is derived from these, never the other way round — which is
    what keeps the phrasing boundary auditable."""
    deltas = [{"k": "BIKE", "from": "214 w", "to": "208 w"}]
    _notify(
        api_db,
        athlete["user_id"],
        api_settings,
        type_key=NotificationType.DRIFT,
        severity=NotificationSeverity.WARN,
        deltas=deltas,
    )
    api_db.commit()

    row = api.get("/api/v1/notifications", headers=athlete["headers"]).json()["data"][0]
    assert row["deltas"] == deltas


@needs_bundle
def test_nobody_reads_another_athletes_inbox(
    athlete, api: TestClient, api_db, api_settings
) -> None:
    _notify(api_db, athlete["user_id"], api_settings)
    api_db.commit()
    notification = api_db.scalar(select(Notification))

    other = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "jonas.feldt@example.com",
            "password": "correct-horse-battery",
            "name": "Jonas Feldt",
        },
    ).json()
    headers = {"Authorization": f"Bearer {other['access_token']}"}

    assert api.get("/api/v1/notifications", headers=headers).json()["total"] == 0
    assert (
        api.post(f"/api/v1/notifications/{notification.id}/read", headers=headers).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Preferences and the critical floor
# ---------------------------------------------------------------------------


@needs_bundle
def test_the_preference_matrix_covers_every_type(athlete, api: TestClient) -> None:
    rows = api.get("/api/v1/notification-preferences", headers=athlete["headers"]).json()
    assert {row["type_key"] for row in rows} == {t.value for t in NotificationType}


@needs_bundle
def test_in_app_cannot_be_switched_off_for_a_critical_type(athlete, api: TestClient) -> None:
    """The athlete chooses the channel; they do not choose whether a cut-off
    warning exists. Clamped rather than rejected, so the screen tells the
    truth about what the system will do."""
    for type_key in CRITICAL_NOTIFICATION_TYPES:
        response = api.patch(
            f"/api/v1/notification-preferences/{type_key.value}",
            headers=athlete["headers"],
            json={"channel_inapp": False, "channel_email": False},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["channel_inapp"] is True, f"{type_key.value} was silenced"
        assert body["inapp_locked"] is True
        assert body["channel_email"] is False, "the channel choice is still honoured"


@needs_bundle
def test_a_non_critical_type_can_be_switched_off_completely(athlete, api: TestClient) -> None:
    body = api.patch(
        "/api/v1/notification-preferences/digest",
        headers=athlete["headers"],
        json={"channel_inapp": False, "channel_email": False, "channel_push": False},
    ).json()
    assert body["channel_inapp"] is False
    assert body["inapp_locked"] is False


@needs_bundle
def test_a_critical_notification_is_delivered_even_with_in_app_off(
    athlete, api: TestClient, api_db, api_settings
) -> None:
    """The floor holds at delivery, not only at the settings endpoint.

    A preference row written directly — around the clamping endpoint — must
    still not silence a cut-off warning.
    """
    from raceos.db.models import NotificationPreference
    from raceos.services import notification_service

    # Signup seeds the matrix, so this writes straight to the stored row —
    # deliberately bypassing the endpoint that would have clamped it.
    row = api_db.scalar(
        select(NotificationPreference).where(
            NotificationPreference.user_id == athlete["user_id"],
            NotificationPreference.type_key == NotificationType.CUTOFF,
        )
    )
    assert row is not None
    row.channel_email = False
    row.channel_push = False
    row.channel_inapp = False
    api_db.commit()

    user = api_db.get(User, athlete["user_id"])
    result = notification_service.notify(
        session=api_db,
        user=user,
        settings=api_settings,
        type_key=NotificationType.CUTOFF,
        severity=NotificationSeverity.BAD,
        title="Your Kalmar margin fell under twenty minutes.",
        body="A re-solve leaves +0:18 on the bike cut-off.",
    )
    api_db.commit()

    assert result.delivered_inapp is True
    assert result.notification is not None


# ---------------------------------------------------------------------------
# The race window
# ---------------------------------------------------------------------------


@needs_bundle
def test_nothing_is_delivered_while_the_athlete_is_racing(
    athlete, api: TestClient, api_db, api_settings
) -> None:
    """An athlete in a swim start cannot act on a drift alert, and it is the
    worst possible moment to introduce doubt."""
    from raceos.services import notification_service

    race = _add_race(api_db, athlete, days_out=0)
    user = api_db.get(User, athlete["user_id"])

    result = notification_service.notify(
        session=api_db,
        user=user,
        settings=api_settings,
        type_key=NotificationType.DRIFT,
        severity=NotificationSeverity.WARN,
        title="Forecast moved to 31°C.",
        body="Heat adjustment moves power and fluid.",
        race_id=race.id,
        # Two hours after the 07:00 local start: they are on the bike.
        now=datetime.combine(race.event_date, time(9, 0), tzinfo=UTC),
    )
    assert result.suppressed is True
    assert result.notification is None
    assert api_db.scalar(select(Notification)) is None


@needs_bundle
def test_the_window_is_scoped_to_the_race_it_is_about(
    athlete, api: TestClient, api_db, api_settings
) -> None:
    """A drift alert for August's race is still worth sending while the
    athlete is racing in June."""
    from raceos.services import notification_service

    racing_today = _add_race(api_db, athlete, days_out=0)
    later = _add_race(api_db, athlete, days_out=60)
    user = api_db.get(User, athlete["user_id"])
    moment = datetime.combine(racing_today.event_date, time(9, 0), tzinfo=UTC)

    result = notification_service.notify(
        session=api_db,
        user=user,
        settings=api_settings,
        type_key=NotificationType.DRIFT,
        severity=NotificationSeverity.WARN,
        title="Forecast moved for your August race.",
        body="Heat adjustment moves power and fluid.",
        race_id=later.id,
        now=moment,
    )
    api_db.commit()
    assert result.suppressed is False
    assert result.notification is not None


@needs_bundle
def test_an_open_quiet_window_is_surfaced_on_the_dashboard(
    athlete, api: TestClient, api_db
) -> None:
    """An athlete who notices the alerts have gone quiet deserves to know it
    is deliberate."""
    race = _add_race(api_db, athlete, days_out=0)
    body = api.get("/api/v1/dashboard", headers=athlete["headers"]).json()

    windows = {window["race_id"] for window in body["quiet_windows"]}
    now = datetime.now(UTC)
    start = datetime.combine(race.event_date, time(7, 0), tzinfo=UTC)
    if start - timedelta(hours=3) <= now <= start + timedelta(hours=24):
        assert str(race.id) in windows
    else:  # pragma: no cover - depends on the wall clock at run time
        assert str(race.id) not in windows


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/dashboard"),
        ("GET", "/api/v1/my-plans"),
        ("GET", "/api/v1/notifications"),
        ("POST", f"/api/v1/notifications/{UUID(int=0)}/read"),
        ("POST", "/api/v1/notifications/read-all"),
        ("GET", "/api/v1/notification-preferences"),
        ("PATCH", "/api/v1/notification-preferences/drift"),
    ],
)
def test_every_dashboard_endpoint_rejects_an_absent_token(
    api: TestClient, method: str, path: str
) -> None:
    assert api.request(method, path, json={}).status_code == 401


def test_a_dashboard_date_is_a_date_not_a_string_constant() -> None:
    """`days_away` is arithmetic on the event date, never a stored label."""
    from raceos.services.dashboard_service import _days_away

    assert _days_away(date(2026, 6, 21), date(2026, 6, 15)) == 6
    assert _days_away(date(2026, 6, 21), date(2026, 6, 21)) == 0
    assert _days_away(date(2026, 6, 21), date(2026, 6, 22)) == -1
