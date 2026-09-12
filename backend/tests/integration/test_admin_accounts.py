"""Account administration, course curation, revenue and churn.

Three surfaces that share one property: they are the screens an operator uses
to make decisions, so every figure on them is aggregated from rows the test
itself wrote. A display constant could not pass any assertion here.

The privacy line is tested rather than described. Support access in this
system is consent-gated — an agent sees an athlete's data only after that
athlete approves, for an hour, with every read logged back to them. An admin
screen that quietly carried the same data would be a second door with none of
the lock on it. `test_the_account_view_withholds_athlete_data` is what stops
that happening by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from raceos.db.models import (
    AdminRoleAssignment,
    Course,
    Invoice,
    Notification,
    Refund,
    Subscription,
    User,
)
from raceos.domain.enums import (
    AdminRole,
    CurationStatus,
    Currency,
    Difficulty,
    DistanceType,
    NotificationType,
    RefundReason,
    SubscriptionStatus,
    UserTier,
)

pytestmark = pytest.mark.integration


def _staff(api: TestClient, api_db, email: str, *roles: AdminRole) -> dict[str, str]:
    created = api.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "correct-horse-battery", "name": email},
    ).json()
    user = api_db.scalar(select(User).where(User.email == email))
    for role in roles:
        api_db.add(AdminRoleAssignment(user_id=user.id, role=role))
    api_db.commit()
    return {"Authorization": f"Bearer {created['access_token']}"}


def _athlete(api: TestClient, email: str, name: str = "Athlete") -> dict[str, object]:
    created = api.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "correct-horse-battery", "name": name},
    ).json()
    return {
        "headers": {"Authorization": f"Bearer {created['access_token']}"},
        "id": created["user"]["id"],
    }


# ---------------------------------------------------------------------------
# Who may open it
# ---------------------------------------------------------------------------


def test_an_ordinary_athlete_cannot_list_accounts(api: TestClient) -> None:
    athlete = _athlete(api, "nosy@example.com")
    assert api.get("/api/v1/admin/users", headers=athlete["headers"]).status_code == 403


def test_support_cannot_list_accounts(api: TestClient, api_db) -> None:
    """The point of the whole support-grant mechanism.

    An agent who could list every account and read its billing would have
    walked around the athlete's consent entirely, and the athlete would never
    see it in their access log because no grant was ever involved.
    """
    headers = _staff(api, api_db, "support@staff.example.com", AdminRole.SUPPORT)
    response = api.get("/api/v1/admin/users", headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["details"]["required_role"] == ["admin"]


def test_ops_cannot_list_accounts_but_can_read_revenue(api: TestClient, api_db) -> None:
    """Two different jobs. Revenue is an ops figure; who holds an account is not."""
    headers = _staff(api, api_db, "ops@staff.example.com", AdminRole.OPS)

    assert api.get("/api/v1/admin/users", headers=headers).status_code == 403
    assert api.get("/api/v1/admin/revenue", headers=headers).status_code == 200


def test_admin_implies_ops(api: TestClient, api_db) -> None:
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)

    assert api.get("/api/v1/admin/users", headers=headers).status_code == 200
    assert api.get("/api/v1/admin/revenue", headers=headers).status_code == 200
    assert api.get("/api/v1/admin/courses/submitted", headers=headers).status_code == 200


# ---------------------------------------------------------------------------
# Finding an account
# ---------------------------------------------------------------------------


def test_search_matches_email_and_name(api: TestClient, api_db) -> None:
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    _athlete(api, "marta.riu@example.com", name="Marta Riu")
    _athlete(api, "someone.else@example.com", name="Someone Else")

    by_email = api.get("/api/v1/admin/users?q=marta.riu", headers=headers).json()
    by_name = api.get("/api/v1/admin/users?q=Marta", headers=headers).json()

    assert [row["email"] for row in by_email["results"]] == ["marta.riu@example.com"]
    assert [row["email"] for row in by_name["results"]] == ["marta.riu@example.com"]


def test_the_total_counts_everything_matching_not_the_page(api: TestClient, api_db) -> None:
    """"Three results" and "the first three of nine hundred" are different
    facts, and only one of them means stop searching."""
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    for index in range(5):
        _athlete(api, f"crowd{index}@example.com")

    page = api.get("/api/v1/admin/users?q=crowd&limit=2", headers=headers).json()

    assert len(page["results"]) == 2
    assert page["total"] == 5


def test_filters_narrow_by_tier_and_role(api: TestClient, api_db) -> None:
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    athlete = _athlete(api, "season@example.com")
    row = api_db.get(User, athlete["id"])
    row.tier = UserTier.SEASON
    api_db.commit()

    by_tier = api.get("/api/v1/admin/users?tier=season", headers=headers).json()
    by_role = api.get("/api/v1/admin/users?role=admin", headers=headers).json()

    assert [r["email"] for r in by_tier["results"]] == ["season@example.com"]
    assert [r["email"] for r in by_role["results"]] == ["admin@staff.example.com"]


def test_a_row_carries_the_roles_it_holds(api: TestClient, api_db) -> None:
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    _staff(api, api_db, "both@staff.example.com", AdminRole.OPS, AdminRole.SUPPORT)

    rows = api.get("/api/v1/admin/users?q=both@", headers=headers).json()["results"]

    assert rows[0]["roles"] == ["ops", "support"]


def test_an_active_subscription_outranks_a_cancelled_one_on_the_list(
    api: TestClient, api_db
) -> None:
    """An account can carry several subscription rows over its life. What an
    operator needs at a glance is whether money is moving now."""
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    athlete = _athlete(api, "resubscribed@example.com")
    api_db.add_all(
        [
            Subscription(
                user_id=athlete["id"],
                tier=UserTier.SEASON,
                status=SubscriptionStatus.CANCELLED,
            ),
            Subscription(
                user_id=athlete["id"],
                tier=UserTier.SEASON,
                status=SubscriptionStatus.ACTIVE,
            ),
        ]
    )
    api_db.commit()

    rows = api.get("/api/v1/admin/users?q=resubscribed", headers=headers).json()["results"]

    assert rows[0]["subscription_status"] == "active"


# ---------------------------------------------------------------------------
# The privacy line
# ---------------------------------------------------------------------------


def test_the_account_view_withholds_athlete_data(api: TestClient, api_db) -> None:
    """The whole reason this surface is separate from the support summary.

    Personal facts an athlete gave us for racing — their date of birth, who to
    ring if they collapse — are not account administration and do not belong
    on a screen that needs no consent to open.
    """
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    athlete = _athlete(api, "private@example.com", name="Private Person")
    row = api_db.get(User, athlete["id"])
    row.date_of_birth = datetime(1988, 4, 2).date()
    row.emergency_contact_name = "Someone Close"
    row.emergency_contact_phone = "+34 600 000 000"
    api_db.commit()

    detail = api.get(f"/api/v1/admin/users/{athlete['id']}", headers=headers).json()
    body = str(detail)

    assert "Someone Close" not in body
    assert "600 000 000" not in body
    assert "1988" not in body
    for field in ("date_of_birth", "emergency_contact_name", "emergency_contact_phone"):
        assert field not in detail


def test_the_account_view_counts_plans_without_showing_them(api: TestClient, api_db) -> None:
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    athlete = _athlete(api, "counted@example.com")

    detail = api.get(f"/api/v1/admin/users/{athlete['id']}", headers=headers).json()

    assert detail["plan_count"] == 0
    assert "plans" not in detail
    # Stated on the response rather than left to inference, so an operator
    # does not read an empty account and go hunting for a screen that shows
    # them what is not there.
    assert "support-access grant" in detail["athlete_data"]


def test_an_unknown_account_is_a_404(api: TestClient, api_db) -> None:
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)

    assert api.get(f"/api/v1/admin/users/{uuid4()}", headers=headers).status_code == 404


# ---------------------------------------------------------------------------
# Revenue
# ---------------------------------------------------------------------------


def _invoice(api_db, user_id, cents: int, currency: Currency, *, days_ago: int = 1) -> Invoice:
    issued = datetime.now(UTC) - timedelta(days=days_ago)
    invoice = Invoice(
        user_id=user_id,
        description="Race plan",
        invoice_number=f"RO-TEST-{uuid4().hex[:10]}",
        amount_cents=cents,
        currency=currency,
        issued_at=issued,
    )
    api_db.add(invoice)
    api_db.commit()
    return invoice


def test_revenue_is_reported_per_currency_and_never_summed(api: TestClient, api_db) -> None:
    """No exchange rate is stored anywhere in this system, so a combined total
    would be a number with no unit."""
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    athlete = _athlete(api, "payer@example.com")
    _invoice(api_db, athlete["id"], 4500, Currency.GBP)
    _invoice(api_db, athlete["id"], 2500, Currency.GBP)
    _invoice(api_db, athlete["id"], 9900, Currency.EUR)

    body = api.get("/api/v1/admin/revenue?days=30", headers=headers).json()
    by_currency = {row["currency"]: row for row in body["currencies"]}

    assert by_currency["GBP"]["invoiced_cents"] == 7000
    assert by_currency["GBP"]["invoice_count"] == 2
    assert by_currency["EUR"]["invoiced_cents"] == 9900
    # The figure an operator might otherwise assume is there.
    assert "total_cents" not in body
    assert not any("total_cents" in row for row in body["currencies"])


def test_a_currency_nobody_paid_in_is_absent_rather_than_zero(api: TestClient, api_db) -> None:
    """"Nobody paid in euros" and "we do not sell in euros" are different
    facts, and a zero row says neither."""
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    athlete = _athlete(api, "payer@example.com")
    _invoice(api_db, athlete["id"], 4500, Currency.GBP)

    body = api.get("/api/v1/admin/revenue", headers=headers).json()

    assert [row["currency"] for row in body["currencies"]] == ["GBP"]


def test_a_refund_reduces_net_in_the_invoices_own_currency(api: TestClient, api_db) -> None:
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    admin = api_db.scalar(select(User).where(User.email == "admin@staff.example.com"))
    athlete = _athlete(api, "refunded@example.com")
    invoice = _invoice(api_db, athlete["id"], 4500, Currency.EUR)
    api_db.add(
        Refund(
            invoice_id=invoice.id,
            reason=RefundReason.RACE_CANCELLED,
            amount_cents=4500,
            actor_user_id=admin.id,
        )
    )
    api_db.commit()

    body = api.get("/api/v1/admin/revenue", headers=headers).json()
    eur = next(row for row in body["currencies"] if row["currency"] == "EUR")

    assert eur["invoiced_cents"] == 4500
    assert eur["refunded_cents"] == 4500
    assert eur["net_cents"] == 0


def test_the_window_excludes_older_invoices(api: TestClient, api_db) -> None:
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    athlete = _athlete(api, "payer@example.com")
    _invoice(api_db, athlete["id"], 4500, Currency.GBP, days_ago=2)
    _invoice(api_db, athlete["id"], 9999, Currency.GBP, days_ago=90)

    body = api.get("/api/v1/admin/revenue?days=30", headers=headers).json()
    gbp = next(row for row in body["currencies"] if row["currency"] == "GBP")

    assert gbp["invoiced_cents"] == 4500


# ---------------------------------------------------------------------------
# Churn
# ---------------------------------------------------------------------------


def _subscription(api_db, user_id, status: SubscriptionStatus, *, started_days_ago: int):
    row = Subscription(user_id=user_id, tier=UserTier.SEASON, status=status)
    api_db.add(row)
    api_db.commit()
    # `created_at` is server-set; the window arithmetic is what is under test,
    # so the row is aged deliberately rather than by waiting a month.
    row.created_at = datetime.now(UTC) - timedelta(days=started_days_ago)
    api_db.commit()
    return row


def test_churn_is_undefined_rather_than_zero_when_nothing_could_churn(
    api: TestClient, api_db
) -> None:
    """A rate over an empty denominator is not a perfect month."""
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)

    churn = api.get("/api/v1/admin/revenue", headers=headers).json()["churn"]

    assert churn["subscriptions_at_risk"] == 0
    assert churn["churn_pct"] is None


def test_growth_cannot_flatter_retention(api: TestClient, api_db) -> None:
    """The denominator is what existed when the window opened.

    Two of four old subscriptions ended this month: fifty per cent. Counting
    the twenty that signed up since would report eight and hide it.
    """
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    for index in range(2):
        athlete = _athlete(api, f"stayed{index}@example.com")
        _subscription(api_db, athlete["id"], SubscriptionStatus.ACTIVE, started_days_ago=200)
    for index in range(2):
        athlete = _athlete(api, f"left{index}@example.com")
        _subscription(api_db, athlete["id"], SubscriptionStatus.CANCELLED, started_days_ago=200)
    for index in range(20):
        athlete = _athlete(api, f"new{index}@example.com")
        _subscription(api_db, athlete["id"], SubscriptionStatus.ACTIVE, started_days_ago=3)

    churn = api.get("/api/v1/admin/revenue?days=30", headers=headers).json()["churn"]

    assert churn["subscriptions_at_risk"] == 4
    assert churn["subscriptions_lost"] == 2
    assert churn["churn_pct"] == 50.0
    assert churn["active_now"] == 22


# ---------------------------------------------------------------------------
# Course curation
# ---------------------------------------------------------------------------


def _submitted_course(api_db, submitter_id, *, slug: str, name: str) -> Course:
    """A course row shaped like one the submission pipeline produces.

    The ingest itself is covered by the submission tests; what matters here is
    the curation decision and who can see the row afterwards.
    """
    course = Course(
        slug=slug,
        name=name,
        place="Sóller",
        timezone="Europe/Madrid",
        lat=39.77,
        lng=2.71,
        distance_type=DistanceType.FULL,
        difficulty=Difficulty.HARD,
        submitted_by_user_id=submitter_id,
    )
    api_db.add(course)
    api_db.commit()
    return course


def test_a_submitted_course_starts_unreviewed_and_private(api: TestClient, api_db) -> None:
    """Today's behaviour, unchanged. Nothing became more visible."""
    owner = _athlete(api, "owner@example.com")
    stranger = _athlete(api, "stranger@example.com")
    course = _submitted_course(api_db, owner["id"], slug="my-race", name="My Race")

    assert course.curation_status is CurationStatus.UNREVIEWED
    mine = api.get("/api/v1/courses?q=My Race", headers=owner["headers"]).json()
    theirs = api.get("/api/v1/courses?q=My Race", headers=stranger["headers"]).json()

    assert [row["slug"] for row in mine["data"]] == ["my-race"]
    assert theirs["data"] == []


def test_publishing_lists_it_to_everyone(api: TestClient, api_db) -> None:
    headers = _staff(api, api_db, "ops@staff.example.com", AdminRole.OPS)
    owner = _athlete(api, "owner@example.com")
    stranger = _athlete(api, "stranger@example.com")
    course = _submitted_course(api_db, owner["id"], slug="my-race", name="My Race")

    response = api.post(
        f"/api/v1/admin/courses/{course.id}/publish",
        headers=headers,
        json={"note": "Surveyed against the organiser's route."},
    )

    assert response.status_code == 200
    theirs = api.get("/api/v1/courses?q=My Race", headers=stranger["headers"]).json()
    assert [row["slug"] for row in theirs["data"]] == ["my-race"]


def test_publishing_does_not_launder_a_course_into_looking_surveyed(
    api: TestClient, api_db
) -> None:
    """Provenance does not expire because somebody approved it. A reader must
    still be able to tell which kind of row they are looking at."""
    headers = _staff(api, api_db, "ops@staff.example.com", AdminRole.OPS)
    owner = _athlete(api, "owner@example.com")
    stranger = _athlete(api, "stranger@example.com")
    course = _submitted_course(api_db, owner["id"], slug="my-race", name="My Race")
    api.post(f"/api/v1/admin/courses/{course.id}/publish", headers=headers, json={})

    row = api.get("/api/v1/courses?q=My Race", headers=stranger["headers"]).json()["data"][0]

    assert row["is_user_submitted"] is True


def test_rejecting_takes_nothing_away_from_the_submitter(api: TestClient, api_db) -> None:
    headers = _staff(api, api_db, "ops@staff.example.com", AdminRole.OPS)
    owner = _athlete(api, "owner@example.com")
    stranger = _athlete(api, "stranger@example.com")
    course = _submitted_course(api_db, owner["id"], slug="my-race", name="My Race")

    api.post(
        f"/api/v1/admin/courses/{course.id}/reject",
        headers=headers,
        json={"note": "The bike leg follows a closed road."},
    )

    mine = api.get("/api/v1/courses?q=My Race", headers=owner["headers"]).json()
    theirs = api.get("/api/v1/courses?q=My Race", headers=stranger["headers"]).json()
    assert [row["slug"] for row in mine["data"]] == ["my-race"]
    assert theirs["data"] == []


def test_a_rejection_must_say_why(api: TestClient, api_db) -> None:
    """"No" on its own is a wall, not a review."""
    headers = _staff(api, api_db, "ops@staff.example.com", AdminRole.OPS)
    owner = _athlete(api, "owner@example.com")
    course = _submitted_course(api_db, owner["id"], slug="my-race", name="My Race")

    blank = api.post(
        f"/api/v1/admin/courses/{course.id}/reject", headers=headers, json={"note": ""}
    )
    missing = api.post(f"/api/v1/admin/courses/{course.id}/reject", headers=headers, json={})

    assert blank.status_code == 422
    assert missing.status_code == 422


def test_a_house_course_cannot_be_reviewed(api: TestClient, api_db) -> None:
    """It is in the catalogue by construction. "Publishing" one would imply it
    had been out, and "rejecting" one would be a deletion in review's clothes.
    """
    headers = _staff(api, api_db, "ops@staff.example.com", AdminRole.OPS)
    house = _submitted_course(api_db, None, slug="house-race", name="House Race")

    response = api.post(f"/api/v1/admin/courses/{house.id}/publish", headers=headers, json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_the_submitter_is_told_what_was_decided(api: TestClient, api_db) -> None:
    """A decision the only person affected by it never hears is not an outcome."""
    headers = _staff(api, api_db, "ops@staff.example.com", AdminRole.OPS)
    owner = _athlete(api, "owner@example.com")
    course = _submitted_course(api_db, owner["id"], slug="my-race", name="My Race")

    api.post(
        f"/api/v1/admin/courses/{course.id}/reject",
        headers=headers,
        json={"note": "The bike leg follows a closed road."},
    )

    note = api_db.scalar(
        select(Notification).where(
            Notification.user_id == owner["id"],
            Notification.type_key == NotificationType.COURSE_REVIEWED,
        )
    )
    assert note is not None
    assert "closed road" in note.body


def test_the_queue_defaults_to_what_still_needs_deciding(api: TestClient, api_db) -> None:
    headers = _staff(api, api_db, "ops@staff.example.com", AdminRole.OPS)
    owner = _athlete(api, "owner@example.com")
    waiting = _submitted_course(api_db, owner["id"], slug="waiting", name="Waiting Race")
    done = _submitted_course(api_db, owner["id"], slug="done", name="Done Race")
    api.post(f"/api/v1/admin/courses/{done.id}/publish", headers=headers, json={})

    queue = api.get("/api/v1/admin/courses/submitted", headers=headers).json()
    published = api.get(
        "/api/v1/admin/courses/submitted?curation_status=published", headers=headers
    ).json()

    assert [row["course_id"] for row in queue["results"]] == [str(waiting.id)]
    assert [row["course_id"] for row in published["results"]] == [str(done.id)]


def test_the_queue_names_who_submitted_it(api: TestClient, api_db) -> None:
    """A reviewer deciding whether to trust a trace needs to be able to ask
    the person who supplied it."""
    headers = _staff(api, api_db, "ops@staff.example.com", AdminRole.OPS)
    owner = _athlete(api, "owner@example.com")
    _submitted_course(api_db, owner["id"], slug="my-race", name="My Race")

    row = api.get("/api/v1/admin/courses/submitted", headers=headers).json()["results"][0]

    assert row["submitted_by_email"] == "owner@example.com"
    assert row["submitted_by"] == owner["id"]


def test_a_decision_is_audited(api: TestClient, api_db) -> None:
    from raceos.db.models import AuditLog

    headers = _staff(api, api_db, "ops@staff.example.com", AdminRole.OPS)
    owner = _athlete(api, "owner@example.com")
    course = _submitted_course(api_db, owner["id"], slug="my-race", name="My Race")

    api.post(f"/api/v1/admin/courses/{course.id}/publish", headers=headers, json={})

    entry = api_db.scalar(select(AuditLog).where(AuditLog.entity_id == course.id))
    assert entry is not None
    assert entry.action == "course.published"


def test_publication_reaches_the_signed_out_directory_too(api: TestClient, api_db) -> None:
    """The SQL half of the visibility rule and the Python half must agree, or
    the page count disagrees with the page."""
    headers = _staff(api, api_db, "ops@staff.example.com", AdminRole.OPS)
    owner = _athlete(api, "owner@example.com")
    course = _submitted_course(api_db, owner["id"], slug="my-race", name="My Race")

    before = api.get("/api/v1/courses?q=My Race").json()
    api.post(f"/api/v1/admin/courses/{course.id}/publish", headers=headers, json={})
    after = api.get("/api/v1/courses?q=My Race").json()

    assert before["data"] == []
    assert before["meta"]["total"] == 0
    assert [row["slug"] for row in after["data"]] == ["my-race"]
    assert after["meta"]["total"] == 1


def test_a_past_due_subscription_still_counts_as_something_to_lose(
    api: TestClient, api_db
) -> None:
    """It has not been cancelled. Leaving it out of the denominator would
    report a worse churn rate than the month actually had."""
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    struggling = _athlete(api, "pastdue@example.com")
    _subscription(api_db, struggling["id"], SubscriptionStatus.PAST_DUE, started_days_ago=200)
    left = _athlete(api, "left@example.com")
    _subscription(api_db, left["id"], SubscriptionStatus.CANCELLED, started_days_ago=200)

    churn = api.get("/api/v1/admin/revenue?days=30", headers=headers).json()["churn"]

    assert churn["subscriptions_at_risk"] == 2
    assert churn["subscriptions_lost"] == 1
    assert churn["churn_pct"] == 50.0


def _age_updated_at(api_db, subscription_id, when: datetime) -> None:
    """Backdate a subscription's ``updated_at``.

    It is maintained by a BEFORE UPDATE trigger, so writing to it does not
    work — the trigger stamps it back to now(). Suspending the trigger for one
    statement is the only way to build a row that was cancelled before the
    window, and the `finally` matters: leaving it disabled would silently stop
    every later test in the session from stamping the column.
    """
    api_db.execute(
        text("ALTER TABLE subscriptions DISABLE TRIGGER trg_subscriptions_set_updated_at")
    )
    try:
        api_db.execute(
            text("UPDATE subscriptions SET updated_at = :when WHERE id = :id"),
            {"when": when, "id": subscription_id},
        )
    finally:
        api_db.execute(
            text("ALTER TABLE subscriptions ENABLE TRIGGER trg_subscriptions_set_updated_at")
        )
        api_db.commit()


def test_a_subscription_cancelled_before_the_window_is_not_this_months_loss(
    api: TestClient, api_db
) -> None:
    headers = _staff(api, api_db, "admin@staff.example.com", AdminRole.ADMIN)
    athlete = _athlete(api, "longgone@example.com")
    row = _subscription(api_db, athlete["id"], SubscriptionStatus.CANCELLED, started_days_ago=400)
    _age_updated_at(api_db, row.id, datetime.now(UTC) - timedelta(days=200))

    churn = api.get("/api/v1/admin/revenue?days=30", headers=headers).json()["churn"]

    assert churn["subscriptions_lost"] == 0
    assert churn["subscriptions_at_risk"] == 0
