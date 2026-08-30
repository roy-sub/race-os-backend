"""Checkout end to end: authorize, capture on success, void on failure.

Against the real in-memory gateway, which enforces the same state machine as
the provider — an authorization can be captured once, or voided once, never
both. No credential is involved anywhere in this file.
"""

from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import Course, CourseBundle, Invoice, Purchase, Race, User
from raceos.domain.enums import PurchaseStatus, RefundReason, UserTier
from raceos.ingest.bundle_loader import load_bundle_file
from raceos.payments.base import sign_webhook
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
def drafted(api: TestClient, signed_up, migrated_engine, api_db, paywall):
    """An athlete with constraints, a race, and an unpaid draft plan."""
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
    race = Race(
        user_id=user.id,
        course_id=course_id,
        course_bundle_id=bundle_id,
        event_date=date(2026, 9, 19),
        start_time_local=time(7, 0),
    )
    api_db.add(race)
    api_db.commit()

    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": str(race.id)})
    assert draft.status_code == 201, draft.text
    return {
        "headers": headers,
        "plan_id": draft.json()["id"],
        "race_id": str(race.id),
        "user_id": user.id,
    }


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


def test_the_price_list_matches_what_the_pricing_page_publishes(api: TestClient) -> None:
    """A quote the server gives and a price the page shows cannot disagree."""
    prices = {
        (row["tier"], row["currency"]): row["amount_cents"]
        for row in api.get("/api/v1/prices").json()
    }
    assert prices[("per_race", "GBP")] == 1500
    assert prices[("per_race", "USD")] == 1900
    assert prices[("season", "EUR")] == 5500
    assert prices[("coach", "USD")] == 9900


# ---------------------------------------------------------------------------
# Authorize
# ---------------------------------------------------------------------------


@needs_bundle
def test_authorizing_places_a_hold_and_charges_nothing(drafted, api: TestClient, api_db) -> None:
    body = buy_plan(api, drafted["headers"], drafted["plan_id"])

    assert body["purchase"]["status"] == "authorized"
    assert body["amount_cents"] == 1500
    assert body["client_secret"], "the client needs this to confirm a payment method"

    purchase = api_db.scalar(select(Purchase))
    assert purchase.captured_at is None
    assert purchase.authorized_at is not None
    assert api_db.scalar(select(Invoice)) is None, "no invoice before a capture"


@needs_bundle
def test_the_same_idempotency_key_never_places_a_second_hold(
    drafted, api: TestClient, api_db
) -> None:
    """A duplicate submit returns the first result rather than charging twice."""
    headers = {**drafted["headers"], "Idempotency-Key": "checkout-once"}
    payload = {"plan_id": drafted["plan_id"], "currency": "GBP"}

    first = api.post("/api/v1/checkout/authorize", headers=headers, json=payload)
    second = api.post("/api/v1/checkout/authorize", headers=headers, json=payload)

    assert first.status_code == second.status_code == 201
    assert first.json()["purchase"]["id"] == second.json()["purchase"]["id"]
    assert len(api_db.scalars(select(Purchase)).all()) == 1


@needs_bundle
def test_a_second_hold_on_the_same_plan_is_refused(drafted, api: TestClient) -> None:
    """Re-solving an existing plan is free, so a second hold is a bug."""
    buy_plan(api, drafted["headers"], drafted["plan_id"])
    again = api.post(
        "/api/v1/checkout/authorize",
        headers=drafted["headers"],
        json={"plan_id": drafted["plan_id"], "currency": "GBP"},
    )
    assert again.status_code == 409


@needs_bundle
def test_nobody_can_authorize_against_another_athletes_plan(drafted, api: TestClient) -> None:
    other = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "jonas.feldt@example.com",
            "password": "correct-horse-battery",
            "name": "Jonas Feldt",
        },
    ).json()
    response = api.post(
        "/api/v1/checkout/authorize",
        headers={"Authorization": f"Bearer {other['access_token']}"},
        json={"plan_id": drafted["plan_id"], "currency": "GBP"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Capture and void
# ---------------------------------------------------------------------------


@needs_bundle
def test_a_successful_solve_captures_and_issues_an_invoice(
    drafted, api: TestClient, api_db
) -> None:
    buy_plan(api, drafted["headers"], drafted["plan_id"])
    solved = api.post(
        f"/api/v1/plans/{drafted['plan_id']}/solve", headers=drafted["headers"], json={}
    )
    assert solved.status_code == 200, solved.text

    purchase = api_db.scalar(select(Purchase))
    api_db.refresh(purchase)
    assert purchase.status is PurchaseStatus.CAPTURED
    assert purchase.captured_at is not None

    invoice = api_db.scalar(select(Invoice))
    assert invoice is not None
    assert invoice.amount_cents == purchase.amount_cents
    assert invoice.invoice_number.startswith("RO-")


@needs_bundle
def test_an_infeasible_solve_voids_the_hold(drafted, api: TestClient, api_db) -> None:
    """An infeasible verdict is a successful solve that produced a refusal.

    The athlete asked for a plan and was told they miss a cut-off, so they got
    no plan and nothing is charged.

    The athlete is made genuinely too slow for the barriers rather than given
    an ambitious goal: a goal that is out of reach is not infeasibility, the
    solver simply returns the achievable time. Only missing a cut-off is.
    """
    too_slow = {
        "swim_threshold_pace": 240,
        "bike_threshold_power": 80,
        "run_threshold_pace": 540,
    }
    for key, value in too_slow.items():
        assert (
            api.put(
                f"/api/v1/constraints/{key}", headers=drafted["headers"], json={"value": value}
            ).status_code
            == 200
        )
    buy_plan(api, drafted["headers"], drafted["plan_id"])
    response = api.post(
        f"/api/v1/plans/{drafted['plan_id']}/solve", headers=drafted["headers"], json={}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INFEASIBLE"

    purchase = api_db.scalar(select(Purchase))
    api_db.refresh(purchase)
    assert purchase.status is PurchaseStatus.VOIDED
    assert purchase.captured_at is None


@needs_bundle
def test_a_hold_can_be_released_when_the_builder_is_abandoned(
    drafted, api: TestClient, api_db
) -> None:
    """Otherwise it sits on the card until the provider expires it, which
    looks exactly like being charged."""
    buy_plan(api, drafted["headers"], drafted["plan_id"])
    response = api.post(
        "/api/v1/checkout/void",
        headers=drafted["headers"],
        json={"plan_id": drafted["plan_id"], "currency": "GBP"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "voided"

    again = api.post(
        "/api/v1/checkout/void",
        headers=drafted["headers"],
        json={"plan_id": drafted["plan_id"], "currency": "GBP"},
    )
    assert again.status_code == 404, "there is no longer an open authorization"


@needs_bundle
def test_a_re_solve_is_free(drafted, api: TestClient, api_db) -> None:
    buy_plan(api, drafted["headers"], drafted["plan_id"])
    solved = api.post(
        f"/api/v1/plans/{drafted['plan_id']}/solve", headers=drafted["headers"], json={}
    ).json()
    api.post(f"/api/v1/plans/{solved['id']}/resolve", headers=drafted["headers"])

    assert len(api_db.scalars(select(Purchase)).all()) == 1, "a re-solve charged again"
    assert len(api_db.scalars(select(Invoice)).all()) == 1


# ---------------------------------------------------------------------------
# Entitlements over the API
# ---------------------------------------------------------------------------


@needs_bundle
def test_the_entitlement_matrix_reflects_the_purchase(drafted, api: TestClient) -> None:
    headers = drafted["headers"]
    race_id = drafted["race_id"]

    before = {
        row["action"]: row
        for row in api.get(f"/api/v1/entitlements?race_id={race_id}", headers=headers).json()
    }
    assert before["course_recon"]["allowed"] is True
    assert before["export_plan"]["allowed"] is False
    assert before["export_plan"]["purchasable_per_race"] is True

    buy_plan(api, headers, drafted["plan_id"])
    api.post(f"/api/v1/plans/{drafted['plan_id']}/solve", headers=headers, json={})

    after = {
        row["action"]: row
        for row in api.get(f"/api/v1/entitlements?race_id={race_id}", headers=headers).json()
    }
    assert after["export_plan"]["allowed"] is True
    assert after["race_mode"]["allowed"] is True
    assert after["post_race_analysis"]["allowed"] is False, "still not a season tier"


@needs_bundle
def test_a_free_athlete_cannot_solve_or_export(drafted, api: TestClient) -> None:
    headers = drafted["headers"]
    solve = api.post(f"/api/v1/plans/{drafted['plan_id']}/solve", headers=headers, json={})
    assert solve.status_code == 402
    assert solve.json()["error"]["details"]["purchasable_per_race"] is True


@needs_bundle
def test_a_season_subscriber_solves_without_paying_per_race(
    drafted, api: TestClient, api_db
) -> None:
    from datetime import UTC, datetime

    from raceos.db.models import Subscription
    from raceos.domain.enums import SubscriptionStatus

    user = api_db.get(User, drafted["user_id"])
    user.tier = UserTier.SEASON
    api_db.add(
        Subscription(
            user_id=user.id,
            tier=UserTier.SEASON,
            status=SubscriptionStatus.ACTIVE,
            renews_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
    )
    api_db.commit()

    solved = api.post(
        f"/api/v1/plans/{drafted['plan_id']}/solve", headers=drafted["headers"], json={}
    )
    assert solved.status_code == 200, solved.text
    assert api_db.scalar(select(Purchase)) is None, "a subscriber was charged per race"


# ---------------------------------------------------------------------------
# Invoices and refunds
# ---------------------------------------------------------------------------


@needs_bundle
def test_invoices_are_listed_and_scoped_to_their_owner(drafted, api: TestClient) -> None:
    buy_plan(api, drafted["headers"], drafted["plan_id"])
    api.post(f"/api/v1/plans/{drafted['plan_id']}/solve", headers=drafted["headers"], json={})

    mine = api.get("/api/v1/invoices", headers=drafted["headers"]).json()
    assert len(mine) == 1

    other = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "jonas.feldt@example.com",
            "password": "correct-horse-battery",
            "name": "Jonas Feldt",
        },
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert api.get("/api/v1/invoices", headers=other_headers).json() == []
    assert api.get(f"/api/v1/invoices/{mine[0]['id']}", headers=other_headers).status_code == 404


@needs_bundle
def test_a_refund_returns_money_and_records_who_and_why(drafted, api: TestClient, api_db) -> None:
    from raceos.config import get_settings
    from raceos.services import billing_service

    buy_plan(api, drafted["headers"], drafted["plan_id"])
    api.post(f"/api/v1/plans/{drafted['plan_id']}/solve", headers=drafted["headers"], json={})

    invoice = api_db.scalar(select(Invoice))
    actor = api_db.get(User, drafted["user_id"])
    refund = billing_service.refund_invoice(
        session=api_db,
        invoice=invoice,
        actor=actor,
        reason=RefundReason.RACE_CANCELLED,
        amount_cents=None,
        note="Organiser cancelled the event.",
        settings=get_settings(),
    )
    api_db.commit()

    assert refund.amount_cents == invoice.amount_cents
    assert refund.reason is RefundReason.RACE_CANCELLED
    assert refund.actor_user_id == actor.id
    purchase = api_db.scalar(select(Purchase))
    api_db.refresh(purchase)
    assert purchase.status is PurchaseStatus.REFUNDED


# ---------------------------------------------------------------------------
# Webhook — the signature is the authentication
# ---------------------------------------------------------------------------


@needs_bundle
def test_an_unsigned_webhook_is_rejected(api: TestClient) -> None:
    response = api.post("/webhooks/payments", json={"id": "evt_1", "type": "ping"})
    assert response.status_code == 422


@needs_bundle
def test_a_forged_webhook_signature_is_rejected(api: TestClient, api_settings) -> None:
    payload = json.dumps({"id": "evt_1", "type": "payment_intent.succeeded"}).encode()
    header = sign_webhook(payload=payload, secret="not-the-configured-secret")
    response = api.post(
        "/webhooks/payments",
        content=payload,
        headers={"Stripe-Signature": header, "Content-Type": "application/json"},
    )
    assert response.status_code == 422


@needs_bundle
def test_a_signed_capture_webhook_reconciles_local_state(
    drafted, api: TestClient, api_db, api_settings
) -> None:
    """The provider is authoritative about money.

    A capture that succeeded there but failed to persist here is exactly what
    this path repairs.
    """
    buy_plan(api, drafted["headers"], drafted["plan_id"])
    purchase = api_db.scalar(select(Purchase))
    intent_id = purchase.payment_provider_intent_id

    payload = json.dumps(
        {
            "id": "evt_capture",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": intent_id, "amount": 1500}},
        }
    ).encode()
    header = sign_webhook(
        payload=payload,
        secret=api_settings.stripe_webhook_secret.get_secret_value(),
    )
    response = api.post(
        "/webhooks/payments",
        content=payload,
        headers={"Stripe-Signature": header, "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "captured"

    api_db.refresh(purchase)
    assert purchase.status is PurchaseStatus.CAPTURED
    assert api_db.scalar(select(Invoice)) is not None


@needs_bundle
def test_replaying_the_same_capture_webhook_does_not_invoice_twice(
    drafted, api: TestClient, api_db, api_settings
) -> None:
    buy_plan(api, drafted["headers"], drafted["plan_id"])
    intent_id = api_db.scalar(select(Purchase)).payment_provider_intent_id
    payload = json.dumps(
        {
            "id": "evt_capture",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": intent_id, "amount": 1500}},
        }
    ).encode()
    headers = {
        "Stripe-Signature": sign_webhook(
            payload=payload,
            secret=api_settings.stripe_webhook_secret.get_secret_value(),
        ),
        "Content-Type": "application/json",
    }
    api.post("/webhooks/payments", content=payload, headers=headers)
    api.post("/webhooks/payments", content=payload, headers=headers)

    assert len(api_db.scalars(select(Invoice)).all()) == 1


@needs_bundle
def test_an_unknown_event_type_is_acknowledged_not_rejected(api: TestClient, api_settings) -> None:
    """A 400 for an event we do not use makes the provider retry it forever."""
    payload = json.dumps({"id": "evt_x", "type": "invoice.upcoming", "data": {}}).encode()
    response = api.post(
        "/webhooks/payments",
        content=payload,
        headers={
            "Stripe-Signature": sign_webhook(
                payload=payload,
                secret=api_settings.stripe_webhook_secret.get_secret_value(),
            ),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "ignored"


# ---------------------------------------------------------------------------
# Authorization on every endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/entitlements"),
        ("POST", "/api/v1/checkout/authorize"),
        ("POST", "/api/v1/checkout/void"),
        ("GET", "/api/v1/invoices"),
        ("GET", f"/api/v1/invoices/{UUID(int=0)}"),
    ],
)
def test_every_billing_endpoint_rejects_an_absent_token(
    api: TestClient, method: str, path: str
) -> None:
    response = api.request(method, path, json={"plan_id": str(UUID(int=0))})
    assert response.status_code == 401


def test_the_price_list_is_deliberately_public(api: TestClient) -> None:
    """A pricing page that needs a login cannot sell anything."""
    assert api.get("/api/v1/prices").status_code == 200
