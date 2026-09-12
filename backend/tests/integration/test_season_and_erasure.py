"""The Season Pass view, and deleting an account.

Two things a settings screen listed and nothing served: a season at a glance
with constraint drift across it, and the account deletion the consequences
were already written for.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import Course, Subscription, User
from raceos.domain.enums import (
    AccountState,
    CourseAvailability,
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


def _subscribe(api_db, email: str, tier: UserTier = UserTier.SEASON) -> None:
    """Put this athlete on a live agreement, the way the webhook would."""
    user = api_db.scalar(select(User).where(User.email == email))
    user.tier = tier
    api_db.add(
        Subscription(
            user_id=user.id,
            tier=tier,
            status=SubscriptionStatus.ACTIVE,
            renews_at=datetime.now(UTC) + timedelta(days=300),
        )
    )
    api_db.commit()


# ---------------------------------------------------------------------------
# Season history
# ---------------------------------------------------------------------------


def test_season_history_is_behind_the_tier_it_is_sold_with(
    api: TestClient, signed_up, paywall
) -> None:
    response = api.get("/api/v1/season-history", headers=signed_up["headers"])
    assert response.status_code == 402
    assert "season" in response.json()["error"]["details"]["required_tiers"]


def test_a_subscriber_sees_their_constraints_as_a_series(
    api: TestClient, signed_up, api_db, paywall
) -> None:
    """`GET /constraints/{key}/history` answers "how has my FTP moved?". This
    answers "what changed about me?", which took eight requests before."""
    headers = signed_up["headers"]
    for value in (210, 218, 224):
        assert (
            api.put(
                "/api/v1/constraints/bike_threshold_power",
                headers=headers,
                json={"value": value, "source": "tested"},
            ).status_code
            == 200
        )
    _subscribe(api_db, "elena.marsh@example.com")

    body = api.get("/api/v1/season-history", headers=headers).json()
    tracks = {track["key"]: track for track in body["constraints"]}
    assert "bike_threshold_power" in tracks

    series = tracks["bike_threshold_power"]["points"]
    assert [point["value"] for point in series] == [210, 218, 224]
    assert tracks["bike_threshold_power"]["change"] == 14
    assert tracks["bike_threshold_power"]["unit"]


def test_a_value_set_once_still_has_a_series(api: TestClient, signed_up, api_db, paywall) -> None:
    """History is written when a value is *superseded*, so without the current
    value appended a constraint set once and never changed would have none."""
    headers = signed_up["headers"]
    api.put("/api/v1/constraints/weight", headers=headers, json={"value": 75})
    _subscribe(api_db, "elena.marsh@example.com")

    tracks = {
        t["key"]: t
        for t in api.get("/api/v1/season-history", headers=headers).json()["constraints"]
    }
    assert len(tracks["weight"]["points"]) == 1
    # One reading has not moved. It has only been taken.
    assert tracks["weight"]["change"] is None


@needs_bundle
def test_races_group_into_the_season_that_prepared_them(
    seeded, api: TestClient, signed_up, api_db, paywall
) -> None:
    """A January race belongs with the autumn before it, not with a season of
    its own that has one race in it."""
    headers = signed_up["headers"]
    for event_date in (date(2027, 1, 17), date(2026, 11, 8)):
        created = api.post(
            "/api/v1/races",
            headers=headers,
            json={
                "course_ref": "tramuntana-full",
                "event_date": event_date.isoformat(),
                "start_time_local": "07:00",
            },
        )
        assert created.status_code in (200, 201), created.text
    _subscribe(api_db, "elena.marsh@example.com")

    seasons = api.get("/api/v1/season-history", headers=headers).json()["seasons"]
    assert len(seasons) == 1, [s["label"] for s in seasons]
    assert seasons[0]["label"] == "2026/27"
    assert len(seasons[0]["races"]) == 2


def test_an_athlete_with_no_races_gets_an_empty_season_list_not_an_error(
    api: TestClient, signed_up, api_db, paywall
) -> None:
    _subscribe(api_db, "elena.marsh@example.com")
    body = api.get("/api/v1/season-history", headers=signed_up["headers"]).json()
    assert body["seasons"] == []


def test_nobody_reads_another_athletes_season(api: TestClient, signed_up, api_db, paywall) -> None:
    """There is no id parameter, so there is no shape of request that asks for
    somebody else's."""
    _subscribe(api_db, "elena.marsh@example.com")
    other = api.post(
        "/api/v1/auth/signup",
        json={"email": "stranger.season@example.com", "password": "correct-horse-battery-42"},
    )
    intruder = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert api.get("/api/v1/season-history", headers=intruder).status_code == 402


# ---------------------------------------------------------------------------
# Erasure
# ---------------------------------------------------------------------------


def test_the_impact_is_readable_before_the_decision(api: TestClient, signed_up) -> None:
    """ "4 plans and 2 invoices" is a different decision from "0 and 0"."""
    response = api.get("/api/v1/auth/me/erasure-impact", headers=signed_up["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "plans": 0,
        "races": 0,
        "invoices": 0,
        "active_subscription": False,
        "coach_links": 0,
    }


def test_erasure_needs_the_words_typed_back(api: TestClient, signed_up) -> None:
    """A boolean can be sent by a mis-wired client. This cannot be undone."""
    response = api.request(
        "DELETE",
        "/api/v1/auth/me",
        headers=signed_up["headers"],
        json={"confirmation": "yes"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["field"] == "confirmation"


def test_erasing_scrubs_the_person_and_keeps_the_row(api: TestClient, signed_up, api_db) -> None:
    """A tombstone, not a hard delete. Invoices are financial records, and the
    audit log has to stay referentially intact."""
    user_id = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com")).id

    response = api.request(
        "DELETE",
        "/api/v1/auth/me",
        headers=signed_up["headers"],
        json={"confirmation": "DELETE MY ACCOUNT", "reason": "finished racing"},
    )
    assert response.status_code == 200, response.text

    api_db.expire_all()
    user = api_db.get(User, user_id)
    assert user is not None, "the row must survive so invoices still point somewhere"
    assert user.account_state is AccountState.ERASED
    assert user.name is None
    assert user.password_hash is None
    assert user.date_of_birth is None
    assert user.emergency_contact_name is None
    assert "elena.marsh" not in user.email
    assert user.email.endswith("@erased.invalid")


def test_the_token_in_another_tab_dies_with_the_account(api: TestClient, signed_up) -> None:
    headers = signed_up["headers"]
    assert api.get("/api/v1/auth/me", headers=headers).status_code == 200

    api.request(
        "DELETE", "/api/v1/auth/me", headers=headers, json={"confirmation": "DELETE MY ACCOUNT"}
    )
    assert api.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_the_erasure_is_recorded_before_the_scrub(api: TestClient, signed_up, api_db) -> None:
    """Written first, so the log records which account this was."""
    from raceos.db.models import AuditLog

    api.request(
        "DELETE",
        "/api/v1/auth/me",
        headers=signed_up["headers"],
        json={"confirmation": "DELETE MY ACCOUNT"},
    )
    row = api_db.scalar(select(AuditLog).where(AuditLog.action == "account.erase"))
    assert row is not None
    # The domain only. Enough to match a support conversation, not enough to
    # be the address it erased.
    assert row.before["email_domain"] == "example.com"
    assert row.after["account_state"] == "erased"


def test_a_live_subscription_blocks_erasure(api: TestClient, signed_up, api_db, paywall) -> None:
    """Cancelling someone's billing as a side effect of a different request is
    not something to infer."""
    _subscribe(api_db, "elena.marsh@example.com")
    response = api.request(
        "DELETE",
        "/api/v1/auth/me",
        headers=signed_up["headers"],
        json={"confirmation": "DELETE MY ACCOUNT"},
    )
    assert response.status_code == 409
    assert "cancel your subscription" in response.json()["error"]["message"].lower()


def test_erasing_twice_is_refused(api: TestClient, signed_up, api_db) -> None:
    headers = signed_up["headers"]
    api.request(
        "DELETE", "/api/v1/auth/me", headers=headers, json={"confirmation": "DELETE MY ACCOUNT"}
    )
    # The token is dead, so a second attempt cannot even authenticate — which
    # is the stronger form of the guarantee.
    again = api.request(
        "DELETE", "/api/v1/auth/me", headers=headers, json={"confirmation": "DELETE MY ACCOUNT"}
    )
    assert again.status_code == 401


def test_an_erased_account_cannot_sign_back_in(api: TestClient, signed_up) -> None:
    api.request(
        "DELETE",
        "/api/v1/auth/me",
        headers=signed_up["headers"],
        json={"confirmation": "DELETE MY ACCOUNT"},
    )
    assert (
        api.post(
            "/api/v1/auth/login",
            json={"email": "elena.marsh@example.com", "password": "correct-horse-battery-42"},
        ).status_code
        == 401
    )


def test_erasure_rejects_an_absent_token(api: TestClient) -> None:
    assert (
        api.request(
            "DELETE", "/api/v1/auth/me", json={"confirmation": "DELETE MY ACCOUNT"}
        ).status_code
        == 401
    )
