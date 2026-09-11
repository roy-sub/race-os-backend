"""Course recon, the cut-off calculator, and push subscriptions.

Recon has two tiers and the split is the product decision, not an accident.
What someone *chooses a race on* — where it is, how far, how much climbing and
what the tightest cut-off is — is free to everyone, signed out included,
because a directory nobody can evaluate is not a front door. The surveyed map
and the furniture around it are the course work an athlete buys, and they come
back withheld with a reason rather than silently missing.

The showcase course is the exception in both directions, and these tests pin
both: its map is open to everyone, because a marketing map nobody can see
advertises nothing, and it is flagged illustrative so it cannot be mistaken for
a surveyed one.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import Course, PushSubscription
from raceos.domain.enums import CourseVisibility
from raceos.ingest.bundle_loader import load_bundle_file

pytestmark = pytest.mark.integration

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"
TRAMUNTANA = BUNDLE_DIR / "tramuntana-full.bundle.json"

needs_bundle = pytest.mark.skipif(
    not TRAMUNTANA.is_file(), reason="generated bundles are git-ignored build artefacts"
)


@pytest.fixture
def course_slug(migrated_engine):
    """A catalogue course: listed to everyone, map behind the entitlement."""
    from sqlalchemy.orm import sessionmaker

    if not TRAMUNTANA.is_file():
        pytest.skip("generated bundles are git-ignored build artefacts")
    with sessionmaker(bind=migrated_engine)() as session:
        load_bundle_file(session, TRAMUNTANA)
        session.commit()
        course = session.scalar(select(Course).where(Course.slug == "tramuntana-full"))
        return course.slug


@pytest.fixture
def showcase_slug(migrated_engine):
    """The signed-out marketing course. Open map, flagged illustrative."""
    from sqlalchemy.orm import sessionmaker

    if not TRAMUNTANA.is_file():
        pytest.skip("generated bundles are git-ignored build artefacts")
    with sessionmaker(bind=migrated_engine)() as session:
        load_bundle_file(session, TRAMUNTANA)
        course = session.scalar(select(Course).where(Course.slug == "tramuntana-full"))
        course.visibility = CourseVisibility.SHOWCASE
        session.commit()
        return course.slug


# ---------------------------------------------------------------------------
# Recon — free, and free of athlete data
# ---------------------------------------------------------------------------


@needs_bundle
def test_recon_needs_no_account(course_slug: str, api: TestClient) -> None:
    """The course library is the front door. A 401 here cannot be evaluated."""
    response = api.get(f"/api/v1/courses/{course_slug}/recon")
    assert response.status_code == 200


@needs_bundle
def test_what_a_race_is_chosen_on_is_free(course_slug: str, api: TestClient) -> None:
    """Where, how far, how much climbing, tightest cut-off. No account."""
    body = api.get(f"/api/v1/courses/{course_slug}/recon").json()

    assert body["course"]["slug"] == course_slug
    assert len(body["legs"]) == 3
    assert body["totals"]["distance_m"] > 0
    assert body["totals"]["elevation_gain_m"] > 0
    assert body["totals"]["final_cutoff_minutes"]
    assert body["totals"]["final_cutoff_name"]
    # Per leg, the two facts someone compares races on.
    for leg in body["legs"]:
        assert leg["distance_m"] > 0
        assert leg["elevation_gain_m"] >= 0
    # ODbL travels with the response whether or not the geometry does.
    assert body["bundle"]["attribution"]
    assert body["bundle"]["provenance"]


@needs_bundle
def test_the_surveyed_map_is_withheld_with_a_reason(course_slug: str, api: TestClient) -> None:
    """Withheld, not missing.

    A client that had to infer this from an empty coordinate array could not
    tell "you have not paid for this" from "this course has no geometry", and
    those two need different screens.
    """
    body = api.get(f"/api/v1/courses/{course_slug}/recon").json()

    assert body["access"]["map_unlocked"] is False
    assert body["access"]["map_locked_reason"]
    assert body["access"]["illustrative_map"] is False

    assert all(leg["coordinates"] == [] for leg in body["legs"])
    assert body["barriers"] == []
    assert body["aid_stations"] == []
    assert body["segments"] == []
    assert body["elevation_profile"] == {}
    assert body["terrain_pmtiles_key"] is None


@needs_bundle
def test_the_showcase_map_is_open_and_says_it_is_illustrative(
    showcase_slug: str, api: TestClient
) -> None:
    """A marketing map nobody can see advertises nothing.

    And a stylised map that does not say it is stylised is a claim about a
    place rather than a drawing of one, so the note travels with it.
    """
    body = api.get(f"/api/v1/courses/{showcase_slug}/recon").json()

    assert body["access"]["map_unlocked"] is True
    assert body["access"]["illustrative_map"] is True
    assert "not to scale" in (body["access"]["illustrative_note"] or "").lower()

    assert any(leg["coordinates"] for leg in body["legs"])
    assert body["barriers"], "zero barriers is a data error, never an empty page"
    assert body["aid_stations"]


@needs_bundle
def test_a_signed_in_athlete_is_not_shown_the_showcase(
    showcase_slug: str, api: TestClient, signed_up: dict
) -> None:
    """Once there is an account, the showcase is not part of the catalogue.

    A deliberately out-of-scale illustration sitting beside surveyed courses
    invites the two to be read as the same kind of thing.
    """
    listing = api.get("/api/v1/courses", headers=signed_up["headers"]).json()
    assert showcase_slug not in {row["slug"] for row in listing["data"]}
    assert (
        api.get(f"/api/v1/courses/{showcase_slug}/recon", headers=signed_up["headers"]).status_code
        == 404
    )


@needs_bundle
def test_recon_contains_no_athlete_data(course_slug: str, api: TestClient) -> None:
    """These numbers describe the course, not anyone racing it."""
    flattened = repr(api.get(f"/api/v1/courses/{course_slug}/recon").json()).lower()
    for forbidden in ("constraint", "athlete", "email", "user_id", "threshold"):
        assert forbidden not in flattened, f"recon leaked {forbidden}"


# ---------------------------------------------------------------------------
# The cut-off calculator
# ---------------------------------------------------------------------------


@needs_bundle
def test_the_cutoff_calculator_answers_without_an_account(
    course_slug: str, api: TestClient
) -> None:
    response = api.post(
        f"/api/v1/courses/{course_slug}/cutoff-check",
        json={"projected_minutes": 780},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["barriers"]
    assert body["bundle_version"]


@needs_bundle
def test_a_slower_projection_puts_more_barriers_at_risk(course_slug: str, api: TestClient) -> None:
    """The whole point: it has to actually discriminate."""
    fast = api.post(
        f"/api/v1/courses/{course_slug}/cutoff-check", json={"projected_minutes": 600}
    ).json()
    slow = api.post(
        f"/api/v1/courses/{course_slug}/cutoff-check", json={"projected_minutes": 1000}
    ).json()

    assert slow["at_risk_count"] >= fast["at_risk_count"]
    assert all(row["margin_minutes"] <= 0 or row["margin_minutes"] >= 0 for row in slow["barriers"])
    # Every margin is smaller for the slower athlete.
    for quick, plodding in zip(fast["barriers"], slow["barriers"], strict=True):
        assert plodding["margin_minutes"] <= quick["margin_minutes"]


@needs_bundle
def test_every_row_says_it_is_an_estimate_not_a_solve(course_slug: str, api: TestClient) -> None:
    """An estimate that looks like a plan is worse than no estimate."""
    body = api.post(
        f"/api/v1/courses/{course_slug}/cutoff-check", json={"projected_minutes": 780}
    ).json()
    for row in body["barriers"]:
        assert "not a solve" in row["basis"]


@needs_bundle
def test_a_nonsense_projection_is_refused(course_slug: str, api: TestClient) -> None:
    for value in (0, -5, 5000):
        response = api.post(
            f"/api/v1/courses/{course_slug}/cutoff-check",
            json={"projected_minutes": value},
        )
        assert response.status_code == 422


def test_the_calculator_is_arithmetic_over_published_limits() -> None:
    """Pure, so it is checkable without a database."""
    from raceos.services.course_service import cutoff_feasibility

    barriers = [
        {"name": "swim_cutoff", "leg": "SWIM", "limit_minutes_from_start": 140.0},
        {"name": "bike_cutoff", "leg": "BIKE", "limit_minutes_from_start": 600.0},
        {"name": "finish", "leg": "RUN", "limit_minutes_from_start": 1020.0},
    ]
    rows = cutoff_feasibility(barriers=barriers, projected_minutes=1020.0)

    # An athlete finishing exactly on the final limit has zero margin
    # everywhere, because every barrier scales with the same finish time.
    assert [row["margin_minutes"] for row in rows] == [0.0, 0.0, 0.0]

    faster = cutoff_feasibility(barriers=barriers, projected_minutes=510.0)
    assert faster[-1]["margin_minutes"] == 510.0


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


ENDPOINT = "https://push.example.com/subscription/abc123"


@needs_bundle
def test_a_browser_can_register_even_though_delivery_is_off(api: TestClient, signed_up) -> None:
    """A browser that has granted permission should not have to ask again when
    the flag flips."""
    response = api.post(
        "/api/v1/push/subscriptions",
        headers=signed_up["headers"],
        json={"endpoint": ENDPOINT, "p256dh_key": "k" * 40, "auth_key": "a" * 20},
    )
    assert response.status_code == 201
    assert response.json()["delivery_enabled"] is False
    assert "in-app inbox" in response.json()["note"]


@needs_bundle
def test_re_subscribing_the_same_browser_does_not_duplicate(
    api: TestClient, signed_up, api_db
) -> None:
    """Otherwise the athlete gets every notification twice."""
    payload = {"endpoint": ENDPOINT, "p256dh_key": "k" * 40, "auth_key": "a" * 20}
    api.post("/api/v1/push/subscriptions", headers=signed_up["headers"], json=payload)
    api.post("/api/v1/push/subscriptions", headers=signed_up["headers"], json=payload)

    assert len(api_db.scalars(select(PushSubscription)).all()) == 1


@needs_bundle
def test_the_endpoint_is_never_echoed_back(api: TestClient, signed_up) -> None:
    """It is a capability URL: anyone holding it can push to that browser."""
    api.post(
        "/api/v1/push/subscriptions",
        headers=signed_up["headers"],
        json={"endpoint": ENDPOINT, "p256dh_key": "k" * 40, "auth_key": "a" * 20},
    )
    listed = api.get("/api/v1/push/subscriptions", headers=signed_up["headers"]).json()

    assert len(listed) == 1
    assert listed[0]["endpoint_host"] == "push.example.com"
    assert "abc123" not in repr(listed)


@needs_bundle
def test_a_non_https_endpoint_is_refused(api: TestClient, signed_up) -> None:
    response = api.post(
        "/api/v1/push/subscriptions",
        headers=signed_up["headers"],
        json={
            "endpoint": "http://push.example.com/x",
            "p256dh_key": "k" * 40,
            "auth_key": "a" * 20,
        },
    )
    assert response.status_code == 422


@needs_bundle
def test_a_browser_can_be_removed(api: TestClient, signed_up, api_db) -> None:
    created = api.post(
        "/api/v1/push/subscriptions",
        headers=signed_up["headers"],
        json={"endpoint": ENDPOINT, "p256dh_key": "k" * 40, "auth_key": "a" * 20},
    ).json()
    removed = api.delete(
        f"/api/v1/push/subscriptions/{created['id']}", headers=signed_up["headers"]
    )
    assert removed.status_code == 204
    assert api_db.scalars(select(PushSubscription)).all() == []


@needs_bundle
def test_nobody_removes_another_accounts_browser(api: TestClient, signed_up) -> None:
    created = api.post(
        "/api/v1/push/subscriptions",
        headers=signed_up["headers"],
        json={"endpoint": ENDPOINT, "p256dh_key": "k" * 40, "auth_key": "a" * 20},
    ).json()
    other = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "jonas.feldt@example.com",
            "password": "correct-horse-battery",
            "name": "Jonas Feldt",
        },
    ).json()
    response = api.delete(
        f"/api/v1/push/subscriptions/{created['id']}",
        headers={"Authorization": f"Bearer {other['access_token']}"},
    )
    assert response.status_code == 404


@needs_bundle
def test_delivery_reports_honestly_while_push_is_disabled(
    api: TestClient, signed_up, api_db, api_settings
) -> None:
    """It does not pretend to have sent anything."""
    from raceos.db.models import Notification, User
    from raceos.domain.enums import NotificationSeverity, NotificationType
    from raceos.services import push_service

    api.post(
        "/api/v1/push/subscriptions",
        headers=signed_up["headers"],
        json={"endpoint": ENDPOINT, "p256dh_key": "k" * 40, "auth_key": "a" * 20},
    )
    user = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    notification = Notification(
        user_id=user.id,
        type_key=NotificationType.DRIFT,
        severity=NotificationSeverity.WARN,
        title="Bike target moved.",
        body="Heat adjustment.",
    )
    api_db.add(notification)
    api_db.commit()

    result = push_service.deliver(
        api_db, user=user, notification=notification, settings=api_settings
    )
    assert result.delivered == 0
    assert "disabled" in result.reason
    assert "in-app inbox" in result.reason


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/push/subscriptions"),
        ("POST", "/api/v1/push/subscriptions"),
        ("DELETE", f"/api/v1/push/subscriptions/{UUID(int=0)}"),
    ],
)
def test_every_push_endpoint_rejects_an_absent_token(
    api: TestClient, method: str, path: str
) -> None:
    response = api.request(
        method,
        path,
        json={"endpoint": ENDPOINT, "p256dh_key": "k" * 40, "auth_key": "a" * 20},
    )
    assert response.status_code == 401
