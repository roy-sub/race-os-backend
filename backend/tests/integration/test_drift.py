"""Drift and the bundle publish cascade.

Law 3 in practice: the plan's stored numbers are never rewritten behind the
athlete's back. Every assertion here is about what did *not* change.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
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
    CourseBundleDiff,
    Notification,
    Plan,
    PlanDriftEvent,
    PlanSplit,
    Race,
    User,
)
from raceos.domain.enums import (
    AdminRole,
    BundleStatus,
    DriftCause,
    DriftStatus,
    PlanStatus,
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
def solved(api: TestClient, signed_up, migrated_engine, api_db, paywall):
    """A solved plan on a race 30 days out, on the real Tramuntana geometry."""
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
        event_date=datetime.now(UTC).date() + timedelta(days=30),
        start_time_local=time(7, 0),
    )
    api_db.add(race)
    api_db.commit()

    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": str(race.id)})
    buy_plan(api, headers, draft.json()["id"])
    plan = api.post(f"/api/v1/plans/{draft.json()['id']}/solve", headers=headers, json={})
    assert plan.status_code == 200, plan.text
    return {
        "headers": headers,
        "user_id": user.id,
        "race_id": race.id,
        "course_id": course_id,
        "bundle_id": bundle_id,
        "plan": plan.json(),
        "plan_id": plan.json()["id"],
    }


def _splits(api_db, plan_id) -> dict[str, float]:
    return {
        row.leg.value: float(row.split_minutes)
        for row in api_db.scalars(select(PlanSplit).where(PlanSplit.plan_id == UUID(str(plan_id))))
    }


# ---------------------------------------------------------------------------
# The shadow recompute
# ---------------------------------------------------------------------------


@needs_bundle
def test_a_check_with_nothing_changed_finds_nothing(solved, api: TestClient) -> None:
    """Identical input implies identical output, so there is no drift."""
    body = api.post(
        f"/api/v1/plans/{solved['plan_id']}/drift/check", headers=solved["headers"]
    ).json()

    assert body["material"] is False
    assert body["field_deltas"] == []
    assert body["event"] is None


@needs_bundle
def test_a_constraint_change_produces_deltas_without_touching_the_plan(
    solved, api: TestClient, api_db
) -> None:
    """The heart of Law 3: detection writes an event, never a plan."""
    before_splits = _splits(api_db, solved["plan_id"])
    headers = solved["headers"]

    # A materially weaker cyclist: this must move the bike split.
    api.put("/api/v1/constraints/bike_threshold_power", headers=headers, json={"value": 180})

    body = api.post(f"/api/v1/plans/{solved['plan_id']}/drift/check", headers=headers).json()

    assert body["material"] is True
    assert body["field_deltas"], "a 44 w drop must move something"
    assert body["event"] is not None
    assert body["event"]["status"] == "pending"

    # And the plan itself is byte-for-byte what it was.
    assert _splits(api_db, solved["plan_id"]) == before_splits
    plan = api.get(f"/api/v1/plans/{solved['plan_id']}", headers=headers).json()
    assert plan["projected_minutes"] == solved["plan"]["projected_minutes"]
    assert plan["version"] == solved["plan"]["version"]


@needs_bundle
def test_a_sub_threshold_change_is_not_reported(solved, api: TestClient) -> None:
    """An alert an athlete cannot act on trains them to ignore the ones they
    can, so a split that moved twelve seconds is not drift."""
    headers = solved["headers"]
    # One watt. Real, but nobody re-solves for it.
    api.put("/api/v1/constraints/bike_threshold_power", headers=headers, json={"value": 225})

    body = api.post(f"/api/v1/plans/{solved['plan_id']}/drift/check", headers=headers).json()
    splits = [d for d in body["field_deltas"] if d["key"].startswith("split.")]
    assert splits == [], f"a one-watt change reported a split move: {splits}"


@needs_bundle
def test_a_second_check_updates_the_pending_event_rather_than_stacking(
    solved, api: TestClient, api_db
) -> None:
    """Two alerts saying the forecast moved are one alert with newer numbers."""
    headers = solved["headers"]
    api.put("/api/v1/constraints/bike_threshold_power", headers=headers, json={"value": 180})
    api.post(f"/api/v1/plans/{solved['plan_id']}/drift/check", headers=headers)
    api.put("/api/v1/constraints/bike_threshold_power", headers=headers, json={"value": 170})
    api.post(f"/api/v1/plans/{solved['plan_id']}/drift/check", headers=headers)

    events = api_db.scalars(
        select(PlanDriftEvent).where(PlanDriftEvent.plan_id == UUID(solved["plan_id"]))
    ).all()
    assert len(events) == 1
    assert events[0].status is DriftStatus.PENDING


# ---------------------------------------------------------------------------
# Applying and dismissing
# ---------------------------------------------------------------------------


@needs_bundle
def test_applying_drift_creates_a_new_version_and_charges_nothing(
    solved, api: TestClient, api_db
) -> None:
    """The athlete did not ask for the world to move."""
    from raceos.db.models import Purchase

    headers = solved["headers"]
    api.put("/api/v1/constraints/bike_threshold_power", headers=headers, json={"value": 180})
    event_id = api.post(f"/api/v1/plans/{solved['plan_id']}/drift/check", headers=headers).json()[
        "event"
    ]["id"]

    purchases_before = len(api_db.scalars(select(Purchase)).all())
    applied = api.post(f"/api/v1/plans/drift/{event_id}/apply", headers=headers)
    assert applied.status_code == 200, applied.text
    new_plan = applied.json()

    assert new_plan["version"] == solved["plan"]["version"] + 1
    assert new_plan["id"] != solved["plan_id"]
    assert len(api_db.scalars(select(Purchase)).all()) == purchases_before

    event = api_db.get(PlanDriftEvent, UUID(event_id))
    api_db.refresh(event)
    assert event.status is DriftStatus.APPLIED
    assert str(event.resulting_plan_id) == new_plan["id"]

    # The version the athlete had is still readable, exactly as solved.
    previous = api_db.get(Plan, UUID(solved["plan_id"]))
    api_db.refresh(previous)
    assert previous.status is PlanStatus.PAST
    assert float(previous.projected_minutes) == solved["plan"]["projected_minutes"]


@needs_bundle
def test_dismissing_keeps_the_plan_and_keeps_the_record(solved, api: TestClient, api_db) -> None:
    """The record is the difference between an informed decision and a
    surprise on race morning."""
    headers = solved["headers"]
    api.put("/api/v1/constraints/bike_threshold_power", headers=headers, json={"value": 180})
    event_id = api.post(f"/api/v1/plans/{solved['plan_id']}/drift/check", headers=headers).json()[
        "event"
    ]["id"]

    body = api.post(f"/api/v1/plans/drift/{event_id}/dismiss", headers=headers).json()
    assert body["status"] == "dismissed"
    assert api_db.get(PlanDriftEvent, UUID(event_id)) is not None

    plan = api.get(f"/api/v1/plans/{solved['plan_id']}", headers=headers).json()
    assert plan["version"] == solved["plan"]["version"]


@needs_bundle
def test_an_event_cannot_be_applied_twice(solved, api: TestClient) -> None:
    headers = solved["headers"]
    api.put("/api/v1/constraints/bike_threshold_power", headers=headers, json={"value": 180})
    event_id = api.post(f"/api/v1/plans/{solved['plan_id']}/drift/check", headers=headers).json()[
        "event"
    ]["id"]

    assert api.post(f"/api/v1/plans/drift/{event_id}/apply", headers=headers).status_code == 200
    again = api.post(f"/api/v1/plans/drift/{event_id}/apply", headers=headers)
    assert again.status_code == 409


@needs_bundle
def test_nobody_acts_on_another_athletes_drift(solved, api: TestClient) -> None:
    headers = solved["headers"]
    api.put("/api/v1/constraints/bike_threshold_power", headers=headers, json={"value": 180})
    event_id = api.post(f"/api/v1/plans/{solved['plan_id']}/drift/check", headers=headers).json()[
        "event"
    ]["id"]

    other = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "jonas.feldt@example.com",
            "password": "correct-horse-battery",
            "name": "Jonas Feldt",
        },
    ).json()
    intruder = {"Authorization": f"Bearer {other['access_token']}"}
    assert api.post(f"/api/v1/plans/drift/{event_id}/apply", headers=intruder).status_code == 404
    assert api.post(f"/api/v1/plans/drift/{event_id}/dismiss", headers=intruder).status_code == 404


# ---------------------------------------------------------------------------
# Sensitivity
# ---------------------------------------------------------------------------


def test_sensitivity_governs_notification_never_detection() -> None:
    """The event is recorded either way, so the plan page can always show what
    moved even when the athlete asked not to be told."""
    from raceos.domain.enums import DriftSensitivity, DriftSeverity
    from raceos.services.drift_service import (
        DriftAssessment,
        FieldDelta,
        passes_sensitivity,
    )

    minor = DriftAssessment(
        plan_id=UUID(int=0),
        cause=DriftCause.FORECAST,
        severity=DriftSeverity.NORMAL,
        deltas=(FieldDelta("split.bike", "Bike", "5:12", "5:16", 4.0),),
        projected_minutes=700.0,
        worst_margin_minutes=40.0,
    )
    critical = DriftAssessment(
        plan_id=UUID(int=0),
        cause=DriftCause.FORECAST,
        severity=DriftSeverity.CUTOFF_RISK,
        deltas=(FieldDelta("margin.bike_cutoff", "Bike margin", "0:40", "0:12", -28.0),),
        projected_minutes=700.0,
        worst_margin_minutes=12.0,
    )

    assert passes_sensitivity(minor, DriftSensitivity.EVERYTHING)
    assert passes_sensitivity(minor, DriftSensitivity.BALANCED)
    assert not passes_sensitivity(minor, DriftSensitivity.CRITICAL)
    assert passes_sensitivity(critical, DriftSensitivity.CRITICAL)


# ---------------------------------------------------------------------------
# The bundle publish cascade
# ---------------------------------------------------------------------------


@pytest.fixture
def ops_headers(api: TestClient, api_db):
    """An operator holding the ops role."""
    created = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "ops@example.com",
            "password": "correct-horse-battery",
            "name": "Ops",
        },
    ).json()
    user = api_db.scalar(select(User).where(User.email == "ops@example.com"))
    api_db.add(AdminRoleAssignment(user_id=user.id, role=AdminRole.OPS))
    api_db.commit()
    return {"Authorization": f"Bearer {created['access_token']}"}


def _draft_bundle(api_db, solved, *, version: str, bike_cutoff_minutes: float) -> CourseBundle:
    """A second bundle for the same course with one cut-off moved."""
    current = api_db.get(CourseBundle, solved["bundle_id"])
    barriers = []
    for barrier in current.barriers:
        copy = dict(barrier)
        if "bike" in str(copy.get("name", "")).lower():
            copy["limit_minutes_from_start"] = bike_cutoff_minutes
        barriers.append(copy)

    incoming = CourseBundle(
        course_id=current.course_id,
        version=version,
        status=BundleStatus.DRAFT,
        provenance=current.provenance,
        elevation_profile=current.elevation_profile,
        barriers=barriers,
        aid_stations=current.aid_stations,
        waypoints=current.waypoints,
        segments=current.segments,
        elevation_source=current.elevation_source,
        attribution=current.attribution,
    )
    api_db.add(incoming)
    api_db.flush()

    from raceos.db.models import CourseBundleLeg

    for leg in current.legs:
        api_db.add(
            CourseBundleLeg(
                bundle_id=incoming.id,
                leg=leg.leg,
                geometry=leg.geometry,
                distance_m=leg.distance_m,
                elevation_gain_m=leg.elevation_gain_m,
                node_count=leg.node_count,
                surface_quality=leg.surface_quality,
            )
        )
    api_db.commit()
    api_db.refresh(incoming)
    return incoming


@needs_bundle
def test_blast_radius_counts_what_a_publish_would_touch(
    solved, api: TestClient, api_db, ops_headers
) -> None:
    """ "We did not realise it affected 400 plans" is not recoverable."""
    incoming = _draft_bundle(api_db, solved, version="2026.9", bike_cutoff_minutes=300.0)

    body = api.get(f"/api/v1/admin/bundles/{incoming.id}/blast-radius", headers=ops_headers).json()

    assert body["plans"] == 1
    assert body["athletes"] == 1
    assert body["races"] == 1
    assert body["to_bundle_version"] == "2026.9"
    assert any(delta["key"].startswith("barrier.") for delta in body["field_deltas"])


@needs_bundle
def test_support_cannot_reach_the_publish_controls(solved, api: TestClient, api_db) -> None:
    """Expressed by not holding the role, not by a hidden button."""
    created = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "support@example.com",
            "password": "correct-horse-battery",
            "name": "Support",
        },
    ).json()
    user = api_db.scalar(select(User).where(User.email == "support@example.com"))
    api_db.add(AdminRoleAssignment(user_id=user.id, role=AdminRole.SUPPORT))
    api_db.commit()
    headers = {"Authorization": f"Bearer {created['access_token']}"}
    incoming = _draft_bundle(api_db, solved, version="2026.9", bike_cutoff_minutes=300.0)

    assert (
        api.get(f"/api/v1/admin/bundles/{incoming.id}/blast-radius", headers=headers).status_code
        == 403
    )
    assert (
        api.post(
            f"/api/v1/admin/bundles/{incoming.id}/publish", headers=headers, json={}
        ).status_code
        == 403
    )


@needs_bundle
def test_publishing_raises_drift_and_rewrites_no_plan(
    solved, api: TestClient, api_db, ops_headers, api_settings
) -> None:
    """The cascade tells each athlete what changed *for them*, and changes
    nothing."""
    from raceos.services import bundle_service

    before_splits = _splits(api_db, solved["plan_id"])
    incoming = _draft_bundle(api_db, solved, version="2026.9", bike_cutoff_minutes=280.0)

    # Publish outside the freeze window: the window is tested separately, and
    # this test is about the cascade.
    monday = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    actor = api_db.scalar(select(User).where(User.email == "ops@example.com"))
    result = bundle_service.publish(
        api_db,
        bundle=api_db.get(CourseBundle, incoming.id),
        actor=actor,
        settings=api_settings,
        now=monday,
    )
    api_db.commit()

    assert result.plans_affected == 1
    assert result.drift_events_raised == 1

    event = api_db.scalar(
        select(PlanDriftEvent).where(
            PlanDriftEvent.plan_id == UUID(solved["plan_id"]),
            PlanDriftEvent.cause == DriftCause.COURSE_BUNDLE_CHANGE,
        )
    )
    assert event is not None
    assert event.status is DriftStatus.PENDING

    # And the plan is untouched.
    assert _splits(api_db, solved["plan_id"]) == before_splits


@needs_bundle
def test_publishing_supersedes_the_previous_bundle_and_records_the_diff(
    solved, api_db, ops_headers, api_settings
) -> None:
    from raceos.services import bundle_service

    previous_id = solved["bundle_id"]
    incoming = _draft_bundle(api_db, solved, version="2026.9", bike_cutoff_minutes=280.0)
    actor = api_db.scalar(select(User).where(User.email == "ops@example.com"))

    bundle_service.publish(
        api_db,
        bundle=api_db.get(CourseBundle, incoming.id),
        actor=actor,
        settings=api_settings,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
    )
    api_db.commit()

    previous = api_db.get(CourseBundle, previous_id)
    api_db.refresh(previous)
    assert previous.status is BundleStatus.SUPERSEDED

    published = api_db.get(CourseBundle, incoming.id)
    assert published.status is BundleStatus.PUBLISHED
    assert published.published_at is not None
    assert published.plans_affected_count == 1

    diff = api_db.scalar(
        select(CourseBundleDiff).where(CourseBundleDiff.to_bundle_id == incoming.id)
    )
    assert diff is not None
    assert any(d["key"].startswith("barrier.") for d in diff.field_deltas)


@needs_bundle
def test_the_freeze_window_blocks_a_publish_thursday_to_sunday(
    solved, api: TestClient, api_db, ops_headers, api_settings
) -> None:
    """Athletes are travelling, packing and racing."""
    from raceos.api.errors import FreezeWindow
    from raceos.services import bundle_service

    incoming = _draft_bundle(api_db, solved, version="2026.9", bike_cutoff_minutes=280.0)
    actor = api_db.scalar(select(User).where(User.email == "ops@example.com"))
    saturday = datetime(2026, 6, 6, 10, 0, tzinfo=UTC)

    with pytest.raises(FreezeWindow):
        bundle_service.publish(
            api_db,
            bundle=api_db.get(CourseBundle, incoming.id),
            actor=actor,
            settings=api_settings,
            now=saturday,
        )
    api_db.rollback()

    still_draft = api_db.get(CourseBundle, incoming.id)
    api_db.refresh(still_draft)
    assert still_draft.status is BundleStatus.DRAFT


@needs_bundle
def test_the_freeze_can_be_overridden_but_only_with_a_reason(
    solved, api: TestClient, api_db, ops_headers
) -> None:
    """A bundle correcting a *wrong* cut-off is more dangerous to withhold
    than to publish — but the audit line has to say why."""
    incoming = _draft_bundle(api_db, solved, version="2026.9", bike_cutoff_minutes=280.0)

    refused = api.post(
        f"/api/v1/admin/bundles/{incoming.id}/publish",
        headers=ops_headers,
        json={"override_freeze": True},
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["field"] == "override_reason"

    accepted = api.post(
        f"/api/v1/admin/bundles/{incoming.id}/publish",
        headers=ops_headers,
        json={
            "override_freeze": True,
            "override_reason": "Organiser published a corrected bike cut-off.",
        },
    )
    assert accepted.status_code == 200, accepted.text

    entry = api_db.scalar(select(AuditLog).where(AuditLog.action == "bundle.publish"))
    assert entry is not None
    assert entry.after["override_freeze"] is True
    assert "corrected" in entry.after["override_reason"]


@needs_bundle
def test_blast_radius_answers_during_a_freeze_rather_than_erroring(
    solved, api: TestClient, api_db, ops_headers
) -> None:
    """The refusal belongs on the publish, not on the question."""
    incoming = _draft_bundle(api_db, solved, version="2026.9", bike_cutoff_minutes=280.0)
    body = api.get(f"/api/v1/admin/bundles/{incoming.id}/blast-radius", headers=ops_headers).json()
    assert "plans" in body
    assert isinstance(body["freeze_blocked"], bool)
    if body["freeze_blocked"]:
        assert body["freeze_reason"], "a blocked window must say why"


@needs_bundle
def test_an_already_published_bundle_cannot_be_republished(
    solved, api: TestClient, api_db, ops_headers, api_settings
) -> None:
    from raceos.api.errors import Conflict
    from raceos.services import bundle_service

    actor = api_db.scalar(select(User).where(User.email == "ops@example.com"))
    monday = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    incoming = _draft_bundle(api_db, solved, version="2026.9", bike_cutoff_minutes=280.0)

    bundle_service.publish(
        api_db,
        bundle=api_db.get(CourseBundle, incoming.id),
        actor=actor,
        settings=api_settings,
        now=monday,
    )
    api_db.commit()

    with pytest.raises(Conflict):
        bundle_service.publish(
            api_db,
            bundle=api_db.get(CourseBundle, incoming.id),
            actor=actor,
            settings=api_settings,
            now=monday,
        )


@needs_bundle
def test_a_superseded_bundle_cannot_be_brought_back(
    solved, api_db, ops_headers, api_settings
) -> None:
    """The pipeline bundle races are pinned to arrives as a draft, so the
    first real publish has to supersede *it* — not only a previously
    published one. Otherwise that draft keeps winning the reader's fallback
    and every plan stays solved against a bundle nothing superseded."""
    from raceos.api.errors import Conflict
    from raceos.services import bundle_service

    actor = api_db.scalar(select(User).where(User.email == "ops@example.com"))
    incoming = _draft_bundle(api_db, solved, version="2026.9", bike_cutoff_minutes=280.0)
    bundle_service.publish(
        api_db,
        bundle=api_db.get(CourseBundle, incoming.id),
        actor=actor,
        settings=api_settings,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
    )
    api_db.commit()

    original = api_db.get(CourseBundle, solved["bundle_id"])
    api_db.refresh(original)
    assert original.status is BundleStatus.SUPERSEDED

    with pytest.raises(Conflict):
        bundle_service.publish(
            api_db,
            bundle=original,
            actor=actor,
            settings=api_settings,
            now=datetime(2026, 6, 1, 11, 0, tzinfo=UTC),
        )


# ---------------------------------------------------------------------------
# Internal jobs
# ---------------------------------------------------------------------------


def test_the_jobs_index_needs_the_internal_secret(api: TestClient) -> None:
    """That secret is the only thing between the internet and every job."""
    assert api.get("/internal/jobs").status_code == 403
    assert api.post("/internal/jobs/drift-sweep").status_code == 403
    assert api.get("/internal/jobs", headers={"X-Internal-Job-Secret": "wrong"}).status_code == 403


def test_every_registered_job_declares_a_cadence_and_a_description(
    api: TestClient, api_settings
) -> None:
    secret = api_settings.internal_job_secret.get_secret_value()
    body = api.get("/internal/jobs", headers={"X-Internal-Job-Secret": secret}).json()

    assert body["jobs"], "the registry is empty"
    for job in body["jobs"]:
        assert job["description"], f"{job['name']} has no description"
        assert len(job["suggested_cron"].split()) == 5, f"{job['name']} cadence is not cron"


def test_running_a_job_records_the_run(api: TestClient, api_settings) -> None:
    secret = api_settings.internal_job_secret.get_secret_value()
    headers = {"X-Internal-Job-Secret": secret}

    run = api.post("/internal/jobs/purge-forecast-cache", headers=headers)
    assert run.status_code == 200, run.text
    assert run.json()["succeeded"] is True
    assert run.json()["duration_ms"] is not None

    runs = api.get("/internal/jobs/runs", headers=headers).json()["runs"]
    assert any(entry["job_name"] == "purge-forecast-cache" for entry in runs)


def test_an_unknown_job_is_a_404_naming_the_known_ones(api: TestClient, api_settings) -> None:
    secret = api_settings.internal_job_secret.get_secret_value()
    response = api.post("/internal/jobs/not-a-job", headers={"X-Internal-Job-Secret": secret})
    assert response.status_code == 404
    assert "drift-sweep" in response.json()["error"]["message"]


@needs_bundle
def test_the_drift_sweep_runs_over_real_plans(
    solved, api: TestClient, api_settings, api_db
) -> None:
    """Runs end to end. With no forecast provider reachable offline it raises
    nothing, which is the correct behaviour: a forecast is an improvement to a
    plan, not a precondition for one."""
    secret = api_settings.internal_job_secret.get_secret_value()
    run = api.post("/internal/jobs/drift-sweep", headers={"X-Internal-Job-Secret": secret})
    assert run.status_code == 200, run.text
    assert run.json()["succeeded"] is True
    assert set(run.json()["result"]) == {"checked", "raised"}
    assert api_db.scalar(select(Notification)) is None
