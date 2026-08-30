"""Admin, ops, support access and Race Mode.

Every KPI here is asserted against rows the test itself created, so a display
constant could not pass.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import (
    AdminRoleAssignment,
    AuditLog,
    Course,
    CourseBundle,
    CrowdReport,
    CrowdReportUpload,
    Race,
    SolveTiming,
    SupportAccessGrant,
    User,
)
from raceos.domain.enums import (
    AdminRole,
    CrowdCategory,
    CrowdStatus,
    IncidentSeverity,
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


def _staff(api: TestClient, api_db, email: str, role: AdminRole) -> dict[str, str]:
    created = api.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "correct-horse-battery", "name": email},
    ).json()
    user = api_db.scalar(select(User).where(User.email == email))
    api_db.add(AdminRoleAssignment(user_id=user.id, role=role))
    api_db.commit()
    return {"Authorization": f"Bearer {created['access_token']}"}


@pytest.fixture
def ops(api: TestClient, api_db):
    return _staff(api, api_db, "ops@example.com", AdminRole.OPS)


@pytest.fixture
def support(api: TestClient, api_db):
    return _staff(api, api_db, "support@example.com", AdminRole.SUPPORT)


@pytest.fixture
def solved(api: TestClient, signed_up, migrated_engine, api_db, paywall):
    """An athlete with a solved plan on a race five days out."""
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

    athlete = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    race = Race(
        user_id=athlete.id,
        course_id=course_id,
        course_bundle_id=bundle_id,
        event_date=datetime.now(UTC).date() + timedelta(days=5),
        start_time_local=time(7, 0),
    )
    api_db.add(race)
    api_db.commit()

    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": str(race.id)})
    buy_plan(api, headers, draft.json()["id"])
    plan = api.post(f"/api/v1/plans/{draft.json()['id']}/solve", headers=headers, json={}).json()
    return {
        "headers": headers,
        "athlete_id": athlete.id,
        "course_id": course_id,
        "race_id": race.id,
        "plan": plan,
        "plan_id": plan["id"],
    }


# ---------------------------------------------------------------------------
# KPIs from the real series
# ---------------------------------------------------------------------------


@needs_bundle
def test_solver_percentiles_come_from_measured_rows(solved, api_db) -> None:
    from raceos.services import admin_service

    timings = api_db.scalars(select(SolveTiming)).all()
    assert timings, "the solve recorded no timing"

    percentiles = admin_service.solver_percentiles(api_db)
    assert percentiles["samples"] == len(timings)
    measured = sorted(row.total_ms for row in timings)
    assert percentiles["p50_ms"] in measured, "P50 is not a latency that happened"
    assert percentiles["p95_ms"] in measured


def test_no_measurements_reports_none_not_zero(api_db) -> None:
    """An operator must be able to tell "nobody solved anything" from "every
    solve was instant"."""
    from raceos.services import admin_service

    percentiles = admin_service.solver_percentiles(api_db)
    assert percentiles == {
        "p50_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "samples": 0,
    }


@needs_bundle
def test_a_kpi_snapshot_counts_what_actually_happened(solved, api_db) -> None:
    from raceos.services import admin_service

    row = admin_service.snapshot_kpis(api_db)
    api_db.commit()

    assert row.plans_solved == 1
    assert row.total_accounts >= 1
    assert row.solver_p50_ms is not None


@needs_bundle
def test_snapshotting_the_same_day_twice_replaces_rather_than_duplicates(solved, api_db) -> None:
    """A cron that fires twice must not produce two truths for one date."""
    from raceos.db.models import KpiSnapshot
    from raceos.services import admin_service

    admin_service.snapshot_kpis(api_db, on_date=date(2026, 6, 1))
    admin_service.snapshot_kpis(api_db, on_date=date(2026, 6, 1))
    api_db.commit()

    rows = api_db.scalars(select(KpiSnapshot).where(KpiSnapshot.date == date(2026, 6, 1))).all()
    assert len(rows) == 1


@needs_bundle
def test_the_overview_serves_the_phrasing_boundary(solved, api: TestClient, ops) -> None:
    """The claim "the model cannot touch a number" is checkable by an operator
    without reading the source."""
    body = api.get("/api/v1/admin/overview", headers=ops).json()
    assert "solver decides" in body["phrasing_boundary"]["law"].lower()
    assert body["solver"]["samples"] >= 1


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


@needs_bundle
def test_support_cannot_reach_ops_surfaces(solved, api: TestClient, support) -> None:
    """Expressed by not holding the role, never by a hidden button."""
    for path in ("/api/v1/admin/overview", "/api/v1/admin/kpis", "/api/v1/admin/crowd-reports"):
        assert api.get(path, headers=support).status_code == 403


@needs_bundle
def test_ops_cannot_grant_roles(solved, api: TestClient, ops, api_db) -> None:
    """Role changes are admin-only, and audited."""
    athlete = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    response = api.post(
        "/api/v1/admin/roles",
        headers=ops,
        json={"user_id": str(athlete.id), "role": "ops", "granted": True},
    )
    assert response.status_code == 403


@needs_bundle
def test_an_admin_role_change_is_audited(solved, api: TestClient, api_db) -> None:
    admin = _staff(api, api_db, "admin@example.com", AdminRole.ADMIN)
    athlete = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))

    response = api.post(
        "/api/v1/admin/roles",
        headers=admin,
        json={"user_id": str(athlete.id), "role": "support", "granted": True},
    )
    assert response.status_code == 200

    from raceos.db.models import AdminRoleAudit

    entry = api_db.scalar(select(AdminRoleAudit))
    assert entry is not None
    assert entry.granted is True
    assert entry.role is AdminRole.SUPPORT


@needs_bundle
def test_an_admin_reaches_the_ops_screens(solved, api: TestClient, api_db) -> None:
    """ADMIN implies the others: an admin is not locked out of an ops screen."""
    admin = _staff(api, api_db, "admin@example.com", AdminRole.ADMIN)
    assert api.get("/api/v1/admin/overview", headers=admin).status_code == 200


# ---------------------------------------------------------------------------
# Support access — the athlete decides, and sees everything
# ---------------------------------------------------------------------------


@needs_bundle
def test_support_sees_nothing_before_the_athlete_approves(solved, api: TestClient, support) -> None:
    athlete_id = solved["athlete_id"]

    # Asking is not access.
    requested = api.post(
        "/api/v1/admin/support/access-requests",
        headers=support,
        json={"athlete_id": str(athlete_id), "reason": "Refund query on invoice."},
    )
    assert requested.status_code == 201
    assert requested.json()["granted_at"] is None

    refused = api.get(f"/api/v1/admin/support/athletes/{athlete_id}/summary", headers=support)
    assert refused.status_code == 403


@needs_bundle
def test_an_approved_grant_lasts_one_hour_and_logs_every_access(
    solved, api: TestClient, support, api_db, api_settings
) -> None:
    athlete_id = solved["athlete_id"]
    grant_id = api.post(
        "/api/v1/admin/support/access-requests",
        headers=support,
        json={"athlete_id": str(athlete_id), "reason": "Refund query."},
    ).json()["id"]

    approved = api.post(f"/api/v1/support-access/{grant_id}/approve", headers=solved["headers"])
    assert approved.status_code == 200
    granted_at = datetime.fromisoformat(approved.json()["granted_at"])
    expires_at = datetime.fromisoformat(approved.json()["expires_at"])
    assert (expires_at - granted_at) == timedelta(minutes=api_settings.support_grant_ttl_minutes)

    summary = api.get(f"/api/v1/admin/support/athletes/{athlete_id}/summary", headers=support)
    assert summary.status_code == 200
    assert summary.json()["plans"] == 1
    # Not visible at any permission level.
    assert "not visible to support" in summary.json()["constraints"]

    # The athlete sees exactly what was opened.
    mine = api.get("/api/v1/support-access", headers=solved["headers"]).json()
    assert len(mine[0]["accessed_log"]) == 1
    assert mine[0]["accessed_log"][0]["what"] == "account summary"


@needs_bundle
def test_a_lapsed_grant_stops_working_at_the_next_request(
    solved, api: TestClient, support, api_db
) -> None:
    """Enforced on read, not trusted from a flag set at approval time."""
    athlete_id = solved["athlete_id"]
    grant_id = api.post(
        "/api/v1/admin/support/access-requests",
        headers=support,
        json={"athlete_id": str(athlete_id), "reason": "Refund query."},
    ).json()["id"]
    api.post(f"/api/v1/support-access/{grant_id}/approve", headers=solved["headers"])
    assert (
        api.get(f"/api/v1/admin/support/athletes/{athlete_id}/summary", headers=support).status_code
        == 200
    )

    grant = api_db.get(SupportAccessGrant, UUID(grant_id))
    grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    api_db.commit()

    assert (
        api.get(f"/api/v1/admin/support/athletes/{athlete_id}/summary", headers=support).status_code
        == 403
    )


@needs_bundle
def test_the_athlete_can_refuse_and_can_revoke(solved, api: TestClient, support) -> None:
    athlete_id = solved["athlete_id"]
    first = api.post(
        "/api/v1/admin/support/access-requests",
        headers=support,
        json={"athlete_id": str(athlete_id), "reason": "Query."},
    ).json()["id"]
    denied = api.post(f"/api/v1/support-access/{first}/deny", headers=solved["headers"])
    assert denied.status_code == 200
    assert (
        api.get(f"/api/v1/admin/support/athletes/{athlete_id}/summary", headers=support).status_code
        == 403
    )

    second = api.post(
        "/api/v1/admin/support/access-requests",
        headers=support,
        json={"athlete_id": str(athlete_id), "reason": "Query again."},
    ).json()["id"]
    api.post(f"/api/v1/support-access/{second}/approve", headers=solved["headers"])
    api.post(f"/api/v1/support-access/{second}/revoke", headers=solved["headers"])
    assert (
        api.get(f"/api/v1/admin/support/athletes/{athlete_id}/summary", headers=support).status_code
        == 403
    )


@needs_bundle
def test_a_support_request_needs_a_reason(solved, api: TestClient, support) -> None:
    """The athlete is being asked to open their account."""
    response = api.post(
        "/api/v1/admin/support/access-requests",
        headers=support,
        json={"athlete_id": str(solved["athlete_id"]), "reason": "   "},
    )
    assert response.status_code == 422


@needs_bundle
def test_only_the_named_athlete_can_approve_a_grant(solved, api: TestClient, support) -> None:
    grant_id = api.post(
        "/api/v1/admin/support/access-requests",
        headers=support,
        json={"athlete_id": str(solved["athlete_id"]), "reason": "Query."},
    ).json()["id"]

    other = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "stranger@example.com",
            "password": "correct-horse-battery",
            "name": "Stranger",
        },
    ).json()
    response = api.post(
        f"/api/v1/support-access/{grant_id}/approve",
        headers={"Authorization": f"Bearer {other['access_token']}"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Crowd promotion
# ---------------------------------------------------------------------------


def _crowd(api_db, course_id, *, uploaders: int, agreement: float) -> CrowdReport:
    report = CrowdReport(
        course_id=course_id,
        category=CrowdCategory.AID_STATION,
        title="Unlisted aid station at km 92",
        body="Water and cola, not in the athlete guide.",
        agreement_weight_pct=agreement,
    )
    api_db.add(report)
    api_db.flush()
    for index in range(uploaders):
        created = api_db.scalar(select(User).where(User.email == f"crowd{index}@example.com"))
        if created is None:
            created = User(email=f"crowd{index}@example.com", name=f"Crowd {index}")
            api_db.add(created)
            api_db.flush()
        api_db.add(
            CrowdReportUpload(
                crowd_report_id=report.id,
                user_id=created.id,
                payload={"km": 92},
                submitted_at=datetime.now(UTC),
            )
        )
    api_db.commit()
    return report


@needs_bundle
def test_confidence_counts_independent_uploaders_not_uploads(solved, api_db, api_settings) -> None:
    """One athlete uploading forty times is one observation."""
    from raceos.services import admin_service

    report = _crowd(api_db, solved["course_id"], uploaders=35, agreement=80.0)
    verdict = admin_service.assess_crowd_report(api_db, report=report, settings=api_settings)
    assert verdict.upload_count == 35
    assert verdict.confidence.value == "high"
    assert verdict.eligible_for_promotion is False, "40 independent uploads required"


@needs_bundle
def test_promotion_needs_enough_independent_evidence(solved, api: TestClient, ops, api_db) -> None:
    report = _crowd(api_db, solved["course_id"], uploaders=20, agreement=80.0)
    refused = api.post(f"/api/v1/admin/crowd-reports/{report.id}/promote", headers=ops, json={})
    assert refused.status_code == 409
    assert "independent upload" in refused.json()["error"]["message"]


@needs_bundle
def test_a_well_evidenced_finding_is_promoted_and_audited(
    solved, api: TestClient, ops, api_db
) -> None:
    report = _crowd(api_db, solved["course_id"], uploaders=45, agreement=85.0)
    promoted = api.post(f"/api/v1/admin/crowd-reports/{report.id}/promote", headers=ops, json={})
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "promoted"
    assert promoted.json()["independent_uploads"] == 45

    entry = api_db.scalar(select(AuditLog).where(AuditLog.action == "crowd.promote"))
    assert entry is not None
    assert entry.after["independent_uploads"] == 45


@needs_bundle
def test_an_override_promotion_is_recorded_as_forced(solved, api: TestClient, ops, api_db) -> None:
    report = _crowd(api_db, solved["course_id"], uploaders=5, agreement=90.0)
    api.post(
        f"/api/v1/admin/crowd-reports/{report.id}/promote",
        headers=ops,
        json={"force": True},
    )
    entry = api_db.scalar(select(AuditLog).where(AuditLog.action == "crowd.promote"))
    assert entry.after["forced"] is True


@needs_bundle
def test_a_promoted_report_cannot_be_promoted_twice(solved, api: TestClient, ops, api_db) -> None:
    report = _crowd(api_db, solved["course_id"], uploaders=45, agreement=85.0)
    api.post(f"/api/v1/admin/crowd-reports/{report.id}/promote", headers=ops, json={})
    again = api.post(f"/api/v1/admin/crowd-reports/{report.id}/promote", headers=ops, json={})
    assert again.status_code == 409


@needs_bundle
def test_a_finding_can_be_held_or_rejected(solved, api: TestClient, ops, api_db) -> None:
    report = _crowd(api_db, solved["course_id"], uploaders=5, agreement=20.0)
    response = api.post(
        f"/api/v1/admin/crowd-reports/{report.id}/resolve",
        headers=ops,
        json={"status": CrowdStatus.REJECTED.value},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


# ---------------------------------------------------------------------------
# Incidents and health
# ---------------------------------------------------------------------------


@needs_bundle
def test_an_incident_is_recorded_and_listed(solved, api: TestClient, ops) -> None:
    created = api.post(
        "/api/v1/admin/incidents",
        headers=ops,
        json={
            "severity": IncidentSeverity.SEV2.value,
            "what": "Forecast provider returned 502 for 14 minutes.",
            "duration_minutes": 14,
            "service_ref": "open-meteo",
        },
    )
    assert created.status_code == 201
    listed = api.get("/api/v1/admin/incidents", headers=ops).json()
    assert listed[0]["what"].startswith("Forecast provider")


@needs_bundle
def test_service_health_is_written_by_probes(solved, api: TestClient, ops) -> None:
    """A hand-set "nominal" is worth nothing."""
    rows = api.get("/api/v1/admin/health", headers=ops).json()
    names = {row["service"] for row in rows}
    assert {"database", "storage", "payments", "solver"} <= names
    assert all(row["status"] in ("nominal", "degraded", "down") for row in rows)


# ---------------------------------------------------------------------------
# Race Mode
# ---------------------------------------------------------------------------


@needs_bundle
def test_race_mode_carries_everything_needed_on_course(solved, api: TestClient) -> None:
    """Race day makes zero network requests, so the payload has to be complete."""
    response = api.get(f"/api/v1/plans/{solved['plan_id']}/race-mode", headers=solved["headers"])
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["plan"]["splits"]
    assert body["plan"]["gates"]
    assert body["plan"]["bags"]
    assert body["plan"]["aid_actions"]
    assert body["plan"]["fuelling"]
    # The "Why this?" content travels: on course with no signal is exactly
    # when an athlete wants to know why a number is what it is.
    assert body["plan"]["constraint_refs"]
    assert body["bundle"]["barriers"]
    assert body["bundle"]["attribution"], "ODbL travels onto the phone"
    assert body["offline"]["complete"] is True
    assert body["cached_at"]


@needs_bundle
def test_race_mode_carries_an_etag_so_check_in_can_revalidate(solved, api: TestClient) -> None:
    response = api.get(f"/api/v1/plans/{solved['plan_id']}/race-mode", headers=solved["headers"])
    etag = response.headers["etag"]
    assert solved["plan_id"] in etag
    assert response.headers["cache-control"].startswith("private")


@needs_bundle
def test_race_mode_surfaces_an_outstanding_drift(solved, api: TestClient, api_db) -> None:
    """Better to know at check-in than at kilometre ninety."""
    headers = solved["headers"]
    api.put("/api/v1/constraints/bike_threshold_power", headers=headers, json={"value": 180})
    api.post(f"/api/v1/plans/{solved['plan_id']}/drift/check", headers=headers)

    body = api.get(f"/api/v1/plans/{solved['plan_id']}/race-mode", headers=headers).json()
    assert len(body["pending_drift"]) == 1
    assert body["pending_drift"][0]["field_deltas"]


@needs_bundle
def test_a_draft_has_no_race_mode(solved, api: TestClient) -> None:
    draft = api.post(
        "/api/v1/plans",
        headers=solved["headers"],
        json={"race_id": str(solved["race_id"])},
    )
    if draft.status_code == 201:
        response = api.get(
            f"/api/v1/plans/{draft.json()['id']}/race-mode", headers=solved["headers"]
        )
        assert response.status_code == 409


@needs_bundle
def test_nobody_opens_another_athletes_race_mode(solved, api: TestClient) -> None:
    other = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "jonas.feldt@example.com",
            "password": "correct-horse-battery",
            "name": "Jonas Feldt",
        },
    ).json()
    response = api.get(
        f"/api/v1/plans/{solved['plan_id']}/race-mode",
        headers={"Authorization": f"Bearer {other['access_token']}"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def test_thirteen_jobs_are_registered(api: TestClient, api_settings) -> None:
    """Every scheduled job is a service function behind one secret."""
    secret = api_settings.internal_job_secret.get_secret_value()
    body = api.get("/internal/jobs", headers={"X-Internal-Job-Secret": secret}).json()
    assert len(body["jobs"]) == 13


@needs_bundle
def test_the_kpi_job_runs_end_to_end(solved, api: TestClient, api_settings) -> None:
    secret = api_settings.internal_job_secret.get_secret_value()
    run = api.post("/internal/jobs/kpi-snapshot", headers={"X-Internal-Job-Secret": secret})
    assert run.status_code == 200, run.text
    assert run.json()["succeeded"] is True


@needs_bundle
def test_the_rollover_job_completes_a_past_race(
    solved, api: TestClient, api_db, api_settings
) -> None:
    race = api_db.get(Race, solved["race_id"])
    race.event_date = datetime.now(UTC).date() - timedelta(days=3)
    api_db.commit()

    secret = api_settings.internal_job_secret.get_secret_value()
    run = api.post("/internal/jobs/race-status-rollover", headers={"X-Internal-Job-Secret": secret})
    assert run.json()["result"]["items_processed"] == 1

    api_db.refresh(race)
    assert race.status.value == "completed"


def test_a_failing_job_records_the_failure(api: TestClient, api_settings) -> None:
    """A job that failed silently is indistinguishable from one the cron never
    called."""
    from raceos.services import job_service

    @job_service.register(
        "deliberately-failing", description="test only", suggested_cron="0 0 * * *"
    )
    def _fail(session, settings):  # type: ignore[no-untyped-def]
        raise RuntimeError("expected")

    secret = api_settings.internal_job_secret.get_secret_value()
    try:
        response = api.post(
            "/internal/jobs/deliberately-failing",
            headers={"X-Internal-Job-Secret": secret},
        )
        assert response.status_code == 500

        runs = api.get(
            "/internal/jobs/runs?name=deliberately-failing",
            headers={"X-Internal-Job-Secret": secret},
        ).json()["runs"]
        assert runs[0]["succeeded"] is False
        assert "RuntimeError" in runs[0]["error"]
    finally:
        job_service._REGISTRY.pop("deliberately-failing", None)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/admin/overview"),
        ("GET", "/api/v1/admin/kpis"),
        ("GET", "/api/v1/admin/health"),
        ("GET", "/api/v1/admin/incidents"),
        ("POST", "/api/v1/admin/incidents"),
        ("GET", "/api/v1/admin/crowd-reports"),
        ("POST", "/api/v1/admin/roles"),
        ("POST", "/api/v1/admin/support/access-requests"),
        ("GET", "/api/v1/support-access"),
        ("POST", f"/api/v1/support-access/{UUID(int=0)}/approve"),
        ("GET", f"/api/v1/plans/{UUID(int=0)}/race-mode"),
    ],
)
def test_every_admin_endpoint_rejects_an_absent_token(
    api: TestClient, method: str, path: str
) -> None:
    assert api.request(method, path, json={}).status_code == 401


@needs_bundle
def test_an_ordinary_athlete_reaches_no_admin_surface(solved, api: TestClient) -> None:
    for path in (
        "/api/v1/admin/overview",
        "/api/v1/admin/kpis",
        "/api/v1/admin/crowd-reports",
        "/api/v1/admin/incidents",
    ):
        assert api.get(path, headers=solved["headers"]).status_code == 403
