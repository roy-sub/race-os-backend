"""Plans end to end: draft, solve, version, override, approve.

The journey a real athlete takes, against the real seeded course, through the
real solver. Nothing here is stubbed — a solve in this file runs all six
stages over the actual Tramuntana geometry.
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import (
    Course,
    CourseBundle,
    Plan,
    Purchase,
    Race,
    SolveTiming,
    User,
)
from raceos.domain.enums import PlanStatus, PurchaseStatus
from raceos.ingest.bundle_loader import load_bundle_file
from tests.integration.conftest import buy_plan

pytestmark = pytest.mark.integration

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"
TRAMUNTANA = BUNDLE_DIR / "tramuntana-full.bundle.json"

needs_bundle = pytest.mark.skipif(
    not TRAMUNTANA.is_file(), reason="generated bundles are git-ignored build artefacts"
)

#: Athlete M from SOLVER_MODEL.md §B.2, so the numbers are recognisable.
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
def seeded_course(migrated_engine):
    """Load the real Tramuntana bundle and commit it."""
    from sqlalchemy.orm import sessionmaker

    if not TRAMUNTANA.is_file():
        pytest.skip("generated bundles are git-ignored build artefacts")

    with sessionmaker(bind=migrated_engine)() as session:
        load_bundle_file(session, TRAMUNTANA)
        session.commit()
        course = session.scalar(select(Course).where(Course.slug == "tramuntana-full"))
        bundle = session.scalar(select(CourseBundle))
        return {"course_id": course.id, "bundle_id": bundle.id}


@pytest.fixture
def ready_athlete(api: TestClient, signed_up, seeded_course, api_db, paywall):
    """An athlete with all eight constraints, a race, and a draft plan."""
    headers = signed_up["headers"]
    for key, value in ATHLETE_M.items():
        response = api.put(f"/api/v1/constraints/{key}", headers=headers, json={"value": value})
        assert response.status_code == 200, response.text

    user = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    race = Race(
        user_id=user.id,
        course_id=seeded_course["course_id"],
        course_bundle_id=seeded_course["bundle_id"],
        event_date=date(2026, 9, 19),
        start_time_local=time(7, 0),
    )
    api_db.add(race)
    api_db.commit()

    created = api.post("/api/v1/plans", headers=headers, json={"race_id": str(race.id)})
    assert created.status_code == 201, created.text
    # A solve is a paid action, so the hold exists before it — the same
    # sequence a real athlete goes through.
    buy_plan(api, headers, created.json()["id"])
    return {
        "headers": headers,
        "plan_id": created.json()["id"],
        "race_id": str(race.id),
        "user_id": user.id,
    }


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


@needs_bundle
def test_a_new_plan_is_a_draft_that_is_not_solved(ready_athlete, api: TestClient) -> None:
    plan = api.get(
        f"/api/v1/plans/{ready_athlete['plan_id']}", headers=ready_athlete["headers"]
    ).json()
    assert plan["status"] == "draft"
    assert plan["feasibility"] == "NOT_SOLVED"
    assert plan["projected_minutes"] is None


@needs_bundle
def test_each_builder_step_persists_immediately(ready_athlete, api: TestClient) -> None:
    """Drafts are saved continuously and independently of solve success.

    That is what lets Part 5.4's promise hold: on a solver crash the athlete's
    inputs survive exactly as entered.
    """
    headers = ready_athlete["headers"]
    plan_id = ready_athlete["plan_id"]

    api.patch(f"/api/v1/plans/{plan_id}/draft", headers=headers, json={"goal_minutes": 720})
    assert api.get(f"/api/v1/plans/{plan_id}", headers=headers).json()["goal_minutes"] == 720

    api.patch(f"/api/v1/plans/{plan_id}/draft", headers=headers, json={"risk": "aggressive"})
    assert api.get(f"/api/v1/plans/{plan_id}", headers=headers).json()["goal_minutes"] == 720


# ---------------------------------------------------------------------------
# Solving
# ---------------------------------------------------------------------------


@needs_bundle
def test_solving_produces_a_complete_plan(ready_athlete, api: TestClient) -> None:
    """Every child collection is populated, read back from the database."""
    response = api.post(
        f"/api/v1/plans/{ready_athlete['plan_id']}/solve",
        headers=ready_athlete["headers"],
        json={},
    )
    assert response.status_code == 200, response.text
    plan = response.json()

    assert plan["status"] == "active"
    assert plan["projected_minutes"] > 0
    assert plan["projected_label"] is not None
    assert ":" in plan["projected_label"]
    assert plan["binding_constraint_key"]

    assert len(plan["splits"]) == 3
    assert [s["leg"] for s in plan["splits"]] == ["SWIM", "BIKE", "RUN"]
    assert plan["segments"], "named segments are the solver's unit of work"
    assert plan["gates"], "cut-off margins are why this product exists"
    assert plan["fuelling"]["carb_g_per_hr"] > 0
    assert plan["aid_actions"], "one action per aid station from the bundle"
    assert len(plan["bags"]) == 5, "exactly five bags, always"
    assert len(plan["constraint_refs"]) == 8, "one per canonical key"


@needs_bundle
def test_every_generated_bag_item_carries_a_reason(ready_athlete, api: TestClient) -> None:
    """§6.1, at the API boundary rather than only inside the solver."""
    plan = api.post(
        f"/api/v1/plans/{ready_athlete['plan_id']}/solve",
        headers=ready_athlete["headers"],
        json={},
    ).json()
    for bag in plan["bags"]:
        for item in bag["items"]:
            assert item["reason_constraint_key"], f"{item['name']} has no reason"
            assert item["reason_text"]


@needs_bundle
def test_read_your_own_writes(ready_athlete, api: TestClient) -> None:
    """After a successful solve, GET must reflect the new version immediately.

    No eventual-consistency window on the plan read path (Part 7.3).
    """
    headers = ready_athlete["headers"]
    solved = api.post(
        f"/api/v1/plans/{ready_athlete['plan_id']}/solve", headers=headers, json={}
    ).json()
    fetched = api.get(f"/api/v1/plans/{solved['id']}", headers=headers).json()
    assert fetched["projected_minutes"] == solved["projected_minutes"]
    assert fetched["version"] == solved["version"]


@needs_bundle
def test_an_identical_solve_is_reused_not_recomputed(
    ready_athlete, api: TestClient, api_db
) -> None:
    """Identical input implies byte-identical output.

    Recomputing would burn the SLA to produce the same bytes.
    """
    headers = ready_athlete["headers"]
    plan_id = ready_athlete["plan_id"]
    first = api.post(f"/api/v1/plans/{plan_id}/solve", headers=headers, json={}).json()
    second = api.post(f"/api/v1/plans/{first['id']}/solve", headers=headers, json={}).json()

    assert first["id"] == second["id"]
    assert first["version"] == second["version"]
    timings = list(api_db.scalars(select(SolveTiming)))
    assert len(timings) == 1, "the second call did not run the solver"


@needs_bundle
def test_a_forced_resolve_creates_a_new_version_and_supersedes(
    ready_athlete, api: TestClient, api_db
) -> None:
    """A solve never mutates a plan; it inserts a new version."""
    headers = ready_athlete["headers"]
    first = api.post(
        f"/api/v1/plans/{ready_athlete['plan_id']}/solve", headers=headers, json={}
    ).json()
    second = api.post(f"/api/v1/plans/{first['id']}/resolve", headers=headers).json()

    assert second["version"] == first["version"] + 1
    assert second["status"] == "active"

    versions = api.get(f"/api/v1/plans/{second['id']}/versions", headers=headers).json()
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[1]["status"] == "past", "the previous version is superseded, not deleted"


@needs_bundle
def test_only_one_active_version_survives_a_resolve(ready_athlete, api: TestClient, api_db) -> None:
    headers = ready_athlete["headers"]
    first = api.post(
        f"/api/v1/plans/{ready_athlete['plan_id']}/solve", headers=headers, json={}
    ).json()
    api.post(f"/api/v1/plans/{first['id']}/resolve", headers=headers)
    api.post(f"/api/v1/plans/{first['id']}/resolve", headers=headers)

    active = list(api_db.scalars(select(Plan).where(Plan.status == PlanStatus.ACTIVE)))
    assert len(active) == 1


@needs_bundle
def test_solve_latency_is_recorded_as_a_real_measurement(
    ready_athlete, api: TestClient, api_db
) -> None:
    """The Admin dashboard's percentiles come from these rows, never from
    display constants (Part 5.4)."""
    api.post(
        f"/api/v1/plans/{ready_athlete['plan_id']}/solve",
        headers=ready_athlete["headers"],
        json={},
    )
    timing = api_db.scalar(select(SolveTiming))
    assert timing is not None
    assert timing.total_ms > 0
    assert timing.exceeded_sla is False


@needs_bundle
def test_solving_without_constraints_names_the_missing_key(
    api: TestClient, signed_up, seeded_course, api_db, paywall
) -> None:
    """A missing constraint is never silently defaulted."""
    user = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    race = Race(
        user_id=user.id,
        course_id=seeded_course["course_id"],
        course_bundle_id=seeded_course["bundle_id"],
        event_date=date(2026, 9, 19),
        start_time_local=time(7, 0),
    )
    api_db.add(race)
    api_db.commit()

    plan = api.post(
        "/api/v1/plans", headers=signed_up["headers"], json={"race_id": str(race.id)}
    ).json()
    buy_plan(api, signed_up["headers"], plan["id"])
    response = api.post(f"/api/v1/plans/{plan['id']}/solve", headers=signed_up["headers"], json={})
    assert response.status_code == 500, "a missing constraint is a caller error to fix"

    # And the hold is released: the athlete got nothing, so nothing is owed.
    purchase = api_db.scalar(select(Purchase).where(Purchase.plan_id == UUID(plan["id"])))
    api_db.refresh(purchase)
    assert purchase.status is PurchaseStatus.VOIDED
    assert purchase.captured_at is None


# ---------------------------------------------------------------------------
# Carb override
# ---------------------------------------------------------------------------


@needs_bundle
def test_a_carb_override_is_logged_before_it_is_used(
    ready_athlete, api: TestClient, api_db
) -> None:
    """§5.1: the override event must exist before the solve consumes it.

    An override is a decision the athlete made, and the record of it must
    outlive the plan.
    """
    from raceos.db.models import OverrideEvent

    plan = api.post(
        f"/api/v1/plans/{ready_athlete['plan_id']}/solve",
        headers=ready_athlete["headers"],
        json={"carb_override": 95},
    ).json()

    assert plan["fuelling"]["overridden"] is True
    assert plan["fuelling"]["carb_g_per_hr"] == 95
    assert plan["fuelling"]["binding_carb_key"] == "options:carb_override"

    event = api_db.scalar(select(OverrideEvent))
    assert event is not None
    assert float(event.overridden_from) == 75.0
    assert float(event.overridden_to) == 95.0


@needs_bundle
def test_an_override_above_the_hard_maximum_is_refused_by_validation(
    ready_athlete, api: TestClient
) -> None:
    """An override says the athlete knows their gut. It does not repeal
    intestinal transport."""
    response = api.post(
        f"/api/v1/plans/{ready_athlete['plan_id']}/solve",
        headers=ready_athlete["headers"],
        json={"carb_override": 200},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@needs_bundle
def test_every_plan_endpoint_rejects_an_absent_token(ready_athlete, api: TestClient) -> None:
    plan_id = ready_athlete["plan_id"]
    assert api.get("/api/v1/plans").status_code == 401
    assert api.get(f"/api/v1/plans/{plan_id}").status_code == 401
    assert api.post(f"/api/v1/plans/{plan_id}/solve", json={}).status_code == 401
    assert api.post(f"/api/v1/plans/{plan_id}/resolve").status_code == 401
    assert api.patch(f"/api/v1/plans/{plan_id}/draft", json={}).status_code == 401
    assert api.delete(f"/api/v1/plans/{plan_id}").status_code == 401
    assert api.get(f"/api/v1/plans/{plan_id}/versions").status_code == 401


@needs_bundle
def test_another_athlete_cannot_read_or_solve_this_plan(ready_athlete, api: TestClient) -> None:
    """The wrong user is a 403, not a 404: the plan exists, they may not have it."""
    intruder = api.post(
        "/api/v1/auth/signup",
        json={"email": "intruder@example.com", "password": "correct-horse-battery"},
    ).json()
    headers = {"Authorization": f"Bearer {intruder['access_token']}"}
    plan_id = ready_athlete["plan_id"]

    assert api.get(f"/api/v1/plans/{plan_id}", headers=headers).status_code == 403
    assert api.post(f"/api/v1/plans/{plan_id}/solve", headers=headers, json={}).status_code == 403
    assert api.patch(f"/api/v1/plans/{plan_id}/draft", headers=headers, json={}).status_code == 403
    assert api.delete(f"/api/v1/plans/{plan_id}", headers=headers).status_code == 403


@needs_bundle
def test_a_solved_plan_cannot_be_deleted(ready_athlete, api: TestClient) -> None:
    """Solved plans are never hard-deleted: post-race comparison needs them."""
    headers = ready_athlete["headers"]
    solved = api.post(
        f"/api/v1/plans/{ready_athlete['plan_id']}/solve", headers=headers, json={}
    ).json()
    response = api.delete(f"/api/v1/plans/{solved['id']}", headers=headers)
    assert response.status_code == 409
    assert "post-race comparison" in response.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@needs_bundle
def test_an_idempotency_key_replays_the_first_response(
    ready_athlete, api: TestClient, api_db
) -> None:
    headers = {**ready_athlete["headers"], "Idempotency-Key": "solve-once-please"}
    first = api.post(f"/api/v1/plans/{ready_athlete['plan_id']}/solve", headers=headers, json={})
    second = api.post(f"/api/v1/plans/{ready_athlete['plan_id']}/solve", headers=headers, json={})
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


@needs_bundle
def test_reusing_a_key_for_a_different_body_is_a_conflict(ready_athlete, api: TestClient) -> None:
    """Returning the first response would silently discard the second request."""
    headers = {**ready_athlete["headers"], "Idempotency-Key": "shared-key"}
    api.post(f"/api/v1/plans/{ready_athlete['plan_id']}/solve", headers=headers, json={})
    second = api.post(
        f"/api/v1/plans/{ready_athlete['plan_id']}/solve",
        headers=headers,
        json={"carb_override": 90},
    )
    assert second.status_code == 409
