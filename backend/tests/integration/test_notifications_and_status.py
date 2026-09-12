"""The notification matrix, and the status an athlete actually reads.

Three of the types below were being sent as `digest` because no better type
existed — including a support agent asking to read somebody's account, which
an athlete could mute by switching off a weekly summary.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import Course, Notification, Subscription, User
from raceos.domain.enums import (
    CRITICAL_NOTIFICATION_TYPES,
    CourseAvailability,
    NotificationType,
    SubscriptionStatus,
    UserTier,
)
from raceos.ingest.bundle_loader import load_bundle_file

pytestmark = pytest.mark.integration

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"
TRAMUNTANA = BUNDLE_DIR / "tramuntana-full.bundle.json"
needs_bundle = pytest.mark.skipif(
    not TRAMUNTANA.is_file(), reason="generated bundles are git-ignored build artefacts"
)

#: Athlete M from SOLVER_MODEL.md §B.2, so a solve has something to read.
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


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


def test_every_type_appears_in_the_preferences_matrix(api: TestClient, signed_up) -> None:
    """A complete matrix means the settings screen never guesses what an absent
    row meant, and a type added later cannot silently inherit "off"."""
    rows = api.get("/api/v1/notification-preferences", headers=signed_up["headers"])
    assert rows.status_code == 200, rows.text
    returned = {row["type_key"] for row in rows.json()}
    assert returned == {t.value for t in NotificationType}


def test_a_privacy_notice_cannot_be_muted_by_a_convenience_preference(
    api: TestClient, signed_up
) -> None:
    """Support access used to be sent as `digest`, so an athlete who switched
    the weekly summary off would never have been told somebody asked to read
    their account."""
    assert NotificationType.SUPPORT_ACCESS in CRITICAL_NOTIFICATION_TYPES
    assert NotificationType.PAYMENT_FAILED in CRITICAL_NOTIFICATION_TYPES
    assert NotificationType.DIGEST not in CRITICAL_NOTIFICATION_TYPES


def test_the_digest_is_no_longer_carrying_other_peoples_messages() -> None:
    """Three events were filed under `digest` for want of a type. Each has one
    now, so muting the weekly summary mutes only the weekly summary."""
    from raceos.services import admin_service, coach_service

    for module in (admin_service, coach_service):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert (
            "NotificationType.DIGEST" not in source
        ), f"{Path(module.__file__).name} is still filing something as a digest"


def test_a_coach_invite_is_its_own_type(api: TestClient, signed_up, api_db) -> None:
    """Not a digest. An athlete with digests off would never have seen it."""
    from raceos.config import Settings
    from raceos.services import coach_service

    athlete = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    coach = User(
        email="coach.notify@example.com",
        name="Sam Reyes",
        is_coach=True,
        tier=UserTier.COACH,
    )
    api_db.add(coach)
    api_db.flush()
    # Managing athletes needs a live agreement, not just the tier.
    api_db.add(
        Subscription(
            user_id=coach.id,
            tier=UserTier.COACH,
            status=SubscriptionStatus.ACTIVE,
            renews_at=datetime.now(UTC) + timedelta(days=200),
        )
    )
    api_db.commit()

    coach_service.invite(
        api_db,
        coach=coach,
        athlete_email=athlete.email,
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
    )
    api_db.commit()

    sent = api_db.scalar(select(Notification).where(Notification.user_id == athlete.id))
    assert sent is not None
    assert sent.type_key is NotificationType.COACH_SHARED


# ---------------------------------------------------------------------------
# Display status
# ---------------------------------------------------------------------------


@needs_bundle
def test_a_paid_but_unsolved_race_does_not_read_as_merely_a_draft(
    seeded, api: TestClient, signed_up, api_db, paywall
) -> None:
    """ "Draft, not solved" hides the one thing the athlete has already done."""
    from tests.integration.conftest import buy_plan

    headers = signed_up["headers"]
    for key, value in ATHLETE_M.items():
        assert (
            api.put(
                f"/api/v1/constraints/{key}", headers=headers, json={"value": value}
            ).status_code
            == 200
        )
    race = api.post(
        "/api/v1/races",
        headers=headers,
        json={
            "course_ref": "tramuntana-full",
            "event_date": (datetime.now(UTC).date() + timedelta(days=90)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    plan = api.post("/api/v1/plans", headers=headers, json={"race_id": race.json()["id"]})

    before = api.get("/api/v1/dashboard", headers=headers).json()["races"][0]
    assert before["display_status"] == "draft"
    assert before["purchased"] is False

    buy_plan(api, headers, plan.json()["id"])
    # A hold is not a payment. The purchase has to be captured to count.
    held = api.get("/api/v1/dashboard", headers=headers).json()["races"][0]
    assert held["purchased"] is False, "an authorization is not a payment"

    assert (
        api.post(f"/api/v1/plans/{plan.json()['id']}/solve", headers=headers, json={}).status_code
        == 200
    )

    after = api.get("/api/v1/dashboard", headers=headers).json()["races"][0]
    assert after["purchased"] is True
    assert after["display_status"] == "active"


@needs_bundle
def test_downloading_an_export_makes_the_plan_read_as_exported(
    seeded, api: TestClient, signed_up
) -> None:
    """"Exported" was specified as a status and could not be built, because
    nothing recorded that an export had happened. Now one fact does."""
    headers = signed_up["headers"]
    for key, value in ATHLETE_M.items():
        api.put(f"/api/v1/constraints/{key}", headers=headers, json={"value": value})
    race = api.post(
        "/api/v1/races",
        headers=headers,
        json={
            "course_ref": "tramuntana-full",
            "event_date": (datetime.now(UTC).date() + timedelta(days=90)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    plan = api.post("/api/v1/plans", headers=headers, json={"race_id": race.json()["id"]})
    plan_id = plan.json()["id"]
    from tests.integration.conftest import buy_plan

    buy_plan(api, headers, plan_id)
    solved = api.post(f"/api/v1/plans/{plan_id}/solve", headers=headers, json={})
    assert solved.status_code == 200, solved.text[:400]

    assert api.get("/api/v1/dashboard", headers=headers).json()["races"][0][
        "display_status"
    ] == "active"

    assert (
        api.get(f"/api/v1/plans/{plan_id}/export/race-card.pdf", headers=headers).status_code
        == 200
    )

    assert api.get("/api/v1/dashboard", headers=headers).json()["races"][0][
        "display_status"
    ] == "exported"


@needs_bundle
def test_the_export_stamp_records_the_first_download_not_the_last(
    seeded, api: TestClient, signed_up, api_db
) -> None:
    """A `last_` column would turn every download into a write on the plan
    row, for a fact that does not get truer the fourth time."""
    from raceos.db.models import Plan

    headers = signed_up["headers"]
    for key, value in ATHLETE_M.items():
        api.put(f"/api/v1/constraints/{key}", headers=headers, json={"value": value})
    race = api.post(
        "/api/v1/races",
        headers=headers,
        json={
            "course_ref": "tramuntana-full",
            "event_date": (datetime.now(UTC).date() + timedelta(days=90)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    plan = api.post("/api/v1/plans", headers=headers, json={"race_id": race.json()["id"]})
    plan_id = plan.json()["id"]
    from tests.integration.conftest import buy_plan

    buy_plan(api, headers, plan_id)
    api.post(f"/api/v1/plans/{plan_id}/solve", headers=headers, json={})

    api.get(f"/api/v1/plans/{plan_id}/export/race-card.pdf", headers=headers)
    first = api_db.get(Plan, UUID(plan_id)).first_exported_at
    api_db.expire_all()
    api.get(f"/api/v1/plans/{plan_id}/export/bags.pdf", headers=headers)

    assert api_db.get(Plan, UUID(plan_id)).first_exported_at == first


@needs_bundle
def test_a_past_race_reads_as_raced_whatever_else_is_true_of_it(
    seeded, api: TestClient, signed_up, api_db
) -> None:
    """The most settled fact wins. An athlete whose plan both raced and drifted
    does not want to be told it needs review."""
    from raceos.db.models import Race

    headers = signed_up["headers"]
    created = api.post(
        "/api/v1/races",
        headers=headers,
        json={
            "course_ref": "tramuntana-full",
            "event_date": (datetime.now(UTC).date() + timedelta(days=10)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    race = api_db.get(Race, __import__("uuid").UUID(created.json()["id"]))
    race.event_date = date(2025, 5, 4)
    api_db.commit()

    past = api.get("/api/v1/my-plans", headers=headers).json()["past"]
    assert past, "a race in the past should appear under past"
    assert past[0]["display_status"] == "raced"


def test_a_race_with_no_plan_at_all_says_so(api: TestClient, signed_up) -> None:
    body = api.get("/api/v1/dashboard", headers=signed_up["headers"]).json()
    assert body["races"] == []


# ---------------------------------------------------------------------------
# Renewal notices
# ---------------------------------------------------------------------------


def _internal_headers(api_settings) -> dict[str, str]:
    return {"X-Internal-Job-Secret": api_settings.internal_job_secret.get_secret_value()}


def test_a_renewal_notice_goes_out_a_week_before_the_charge(
    api: TestClient, signed_up, api_db, api_settings
) -> None:
    """Before it, not after. After the charge the message is a receipt; before
    it, it is the chance to cancel."""
    user = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    user.tier = UserTier.SEASON
    api_db.add(
        Subscription(
            user_id=user.id,
            tier=UserTier.SEASON,
            status=SubscriptionStatus.ACTIVE,
            renews_at=datetime.now(UTC) + timedelta(days=6, hours=12),
        )
    )
    api_db.commit()

    run = api.post(
        "/internal/jobs/subscription-renewal-notices", headers=_internal_headers(api_settings)
    )
    assert run.status_code == 200, run.text
    assert run.json()["result"]["notices_sent"] == 1

    sent = api_db.scalar(
        select(Notification).where(
            Notification.user_id == user.id,
            Notification.type_key == NotificationType.SUBSCRIPTION_RENEWING,
        )
    )
    assert sent is not None


def test_nobody_who_has_already_cancelled_is_told_it_is_renewing(
    api: TestClient, signed_up, api_db, api_settings
) -> None:
    """Because it is not."""
    user = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    renews = datetime.now(UTC) + timedelta(days=6, hours=12)
    api_db.add(
        Subscription(
            user_id=user.id,
            tier=UserTier.SEASON,
            status=SubscriptionStatus.ACTIVE,
            renews_at=renews,
            cancel_at=renews,
        )
    )
    api_db.commit()

    run = api.post(
        "/internal/jobs/subscription-renewal-notices", headers=_internal_headers(api_settings)
    )
    assert run.json()["result"]["notices_sent"] == 0
    assert run.json()["result"]["skipped_already_cancelling"] == 1


def test_a_renewal_further_out_than_the_window_waits(
    api: TestClient, signed_up, api_db, api_settings
) -> None:
    """A one-day window means a daily cron sends exactly one notice per
    renewal, with no sent-marker column to keep in step."""
    user = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    api_db.add(
        Subscription(
            user_id=user.id,
            tier=UserTier.SEASON,
            status=SubscriptionStatus.ACTIVE,
            renews_at=datetime.now(UTC) + timedelta(days=30),
        )
    )
    api_db.commit()

    run = api.post(
        "/internal/jobs/subscription-renewal-notices", headers=_internal_headers(api_settings)
    )
    assert run.json()["result"]["notices_sent"] == 0
