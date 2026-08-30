"""Plans: create, patch, solve, version, override, approve.

``POST /plans/:id/solve`` returns **200 with the full solved plan** when it
completes inside the synchronous budget, and 202 with a job id when it does
not. The synchronous path is the default because the measured cost is well
inside the SLA; the async path is fully implemented because the frontend
already handles it and because a solve that ever ran long needs somewhere to go.

``INFEASIBLE`` is a **422 with a verdict, not a server error** — a successful
solve whose answer is "not at this goal". Its details name the *earliest
missed* barrier (§F.5), because that is where the athlete's race actually ends.
"""

from __future__ import annotations

import hashlib
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Response, status

from raceos.api.deps import Config, CurrentUser, DbSession, Warnings
from raceos.api.errors import NotFound
from raceos.api.schemas.plan import (
    OverrideRequest,
    PlanCreate,
    PlanDetail,
    PlanDraftPatch,
    PlanSummary,
    SolveJobOut,
    SolveRequest,
)
from raceos.api.serialise import plan_detail, plan_summary
from raceos.db.models import SolveJob
from raceos.domain.entitlements import EntitlementAction
from raceos.services import auth_service, billing_service, plan_service
from raceos.services.rate_limit import lookup_idempotent, record_idempotent

router = APIRouter(prefix="/api/v1/plans", tags=["plans"])


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED, summary="Start a draft plan")
def create_plan(payload: PlanCreate, session: DbSession, user: CurrentUser) -> PlanSummary:
    plan = plan_service.create_draft(session, user=user, race_id=payload.race_id)
    session.commit()
    return plan_summary(plan)


@router.get("", summary="Every plan this athlete has")
def list_plans(session: DbSession, user: CurrentUser) -> list[PlanSummary]:
    return [plan_summary(plan) for plan in plan_service.list_plans(session, user=user)]


@router.get("/{plan_id}", summary="One plan in full")
def get_plan(
    plan_id: UUID, session: DbSession, user: CurrentUser, warnings: Warnings, settings: Config
) -> PlanDetail:
    from raceos.services import constraint_service

    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)
    constraint_service.attach_staleness_warnings(
        constraint_service.list_constraints(session, athlete_id=user.id),
        warnings,
        settings,
    )
    return plan_detail(session, plan)


@router.patch("/{plan_id}/draft", summary="Save a builder step")
def patch_draft(
    plan_id: UUID, payload: PlanDraftPatch, session: DbSession, user: CurrentUser
) -> PlanSummary:
    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)
    plan_service.patch_draft(
        session,
        plan=plan,
        user=user,
        changes=payload.model_dump(exclude_unset=True),
    )
    session.commit()
    return plan_summary(plan)


@router.delete(
    "/{plan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete a draft",
)
def delete_plan(plan_id: UUID, session: DbSession, user: CurrentUser) -> None:
    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)
    plan_service.delete_draft(session, plan=plan, user=user)
    session.commit()


@router.get("/{plan_id}/versions", summary="Every version for this race")
def list_versions(plan_id: UUID, session: DbSession, user: CurrentUser) -> list[PlanSummary]:
    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)
    return [plan_summary(row) for row in plan_service.list_versions(session, plan=plan, user=user)]


# ---------------------------------------------------------------------------
# Solving
# ---------------------------------------------------------------------------


def _request_hash(payload: SolveRequest, plan_id: UUID) -> str:
    return hashlib.sha256(f"{plan_id}:{payload.model_dump_json()}".encode()).hexdigest()


@router.post("/{plan_id}/solve", summary="Solve the plan")
def solve_plan(
    plan_id: UUID,
    payload: SolveRequest,
    response: Response,
    session: DbSession,
    user: CurrentUser,
    settings: Config,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PlanDetail:
    """200 with the solved plan; 422 with a verdict when infeasible.

    On solver failure **nothing is persisted and nothing is charged**: the
    draft's inputs survive exactly as entered, because drafts are saved
    continuously and independently of solve success.
    """
    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)

    if idempotency_key:
        replay = lookup_idempotent(
            session,
            key=idempotency_key,
            endpoint="plans.solve",
            request_hash=_request_hash(payload, plan_id),
        )
        if replay is not None:
            response.status_code = replay.status_code
            return PlanDetail.model_validate(replay.body)

    auth_service.require_verified_for_solve(user, settings)
    # 402 with the upgrade path, before any work is done. Refusing after the
    # solve would burn the SLA to produce something we then withhold.
    billing_service.require(
        session, user=user, action=EntitlementAction.SOLVE_PLAN, race_id=plan.race_id
    )

    if payload.carb_override is not None:
        # The override event is written BEFORE the solve that consumes it
        # (§5.1): an override is a decision the athlete made, and the record
        # of it must outlive the plan.
        plan_service.record_override(
            session,
            plan=plan,
            user=user,
            constraint_key="gut_carb_ceiling",
            new_value=payload.carb_override,
            reason="carb override at solve",
        )

    try:
        result = plan_service.solve_plan(
            session,
            plan=plan,
            user=user,
            settings=settings,
            carb_override=payload.carb_override,
            force=payload.force,
        )
    except Exception:
        # **Nothing is charged for a plan the athlete did not get.** The hold
        # is released on every failure path, infeasible verdicts included.
        #
        # The commit before the void is deliberate: no database error has
        # occurred, so the session is sound, and it persists the solve-timing
        # measurement. That row is telemetry for the latency series rather
        # than plan state, and discarding it would quietly bias the P95 by
        # dropping exactly the solves that took longest to fail.
        session.commit()
        billing_service.void_for_plan(session, plan=plan, reason="solve failed", settings=settings)
        session.commit()
        raise

    # Capture sits on the success path and nowhere else.
    billing_service.capture_for_plan(session, plan=result.plan, settings=settings)
    session.commit()

    detail = plan_detail(session, result.plan)
    if idempotency_key:
        record_idempotent(
            session,
            key=idempotency_key,
            endpoint="plans.solve",
            request_hash=_request_hash(payload, plan_id),
            status_code=200,
            body=detail.model_dump(mode="json"),
            user_id=user.id,
            settings=settings,
        )
        session.commit()
    return detail


@router.post("/{plan_id}/resolve", summary="Athlete-initiated re-solve")
def resolve_plan(
    plan_id: UUID, session: DbSession, user: CurrentUser, settings: Config
) -> PlanDetail:
    """Always free, and always a new version.

    ``force=True`` because the athlete asked: returning the cached version
    would look like the button did nothing.
    """
    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)
    billing_service.require(
        session, user=user, action=EntitlementAction.DRIFT_RESOLVE, race_id=plan.race_id
    )
    result = plan_service.solve_plan(session, plan=plan, user=user, settings=settings, force=True)
    session.commit()
    return plan_detail(session, result.plan)


@router.post("/{plan_id}/override", summary="Log a constraint override")
def override(
    plan_id: UUID, payload: OverrideRequest, session: DbSession, user: CurrentUser
) -> dict[str, str]:
    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)
    event = plan_service.record_override(
        session,
        plan=plan,
        user=user,
        constraint_key=payload.constraint_key,
        new_value=payload.new_value,
        reason=payload.reason,
    )
    session.commit()
    return {"override_event_id": str(event.id)}


@router.post("/{plan_id}/approve", summary="Approve a coach-built plan")
def approve(plan_id: UUID, session: DbSession, user: CurrentUser) -> PlanSummary:
    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)
    plan_service.approve_plan(session, plan=plan, user=user)
    session.commit()
    return plan_summary(plan)


# ---------------------------------------------------------------------------
# Solve jobs — the async escape hatch
# ---------------------------------------------------------------------------

jobs_router = APIRouter(prefix="/api/v1/solve-jobs", tags=["plans"])


@jobs_router.get("/{job_id}", summary="Poll an async solve")
def get_solve_job(job_id: UUID, session: DbSession, user: CurrentUser) -> SolveJobOut:
    job = session.get(SolveJob, job_id)
    if job is None or job.user_id != user.id:
        raise NotFound("Solve job not found.")
    return SolveJobOut(
        job_id=job.id,
        status=job.status.value,
        resulting_plan_id=job.resulting_plan_id,
        error_code=job.error_code,
    )
