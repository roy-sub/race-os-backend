"""Drift: check, review, apply, dismiss.

**Law 3 lives here.** A check runs the solver again against a throwaway copy
of the plan's inputs and reports what *would* change. The plan is untouched
until the athlete applies it, and applying produces a new version — so the
plan they raced on stays readable forever.

Applying is always free. The athlete did not ask for the world to move.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from raceos.api.deps import Config, CurrentUser, DbSession
from raceos.api.schemas.drift import DriftCheckOut, DriftEventOut
from raceos.api.schemas.plan import PlanDetail
from raceos.api.serialise import plan_detail
from raceos.db.models import Race
from raceos.domain.enums import DriftCause
from raceos.services import drift_service, plan_service, weather_service

router = APIRouter(prefix="/api/v1/plans", tags=["drift"])


@router.get("/{plan_id}/drift", summary="Pending drift events for this plan")
def list_drift(plan_id: UUID, session: DbSession, user: CurrentUser) -> list[DriftEventOut]:
    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)
    return [
        DriftEventOut.model_validate(event)
        for event in drift_service.list_pending(session, plan=plan)
    ]


@router.post("/{plan_id}/drift/check", summary="Shadow-recompute against today")
def check_drift(
    plan_id: UUID, session: DbSession, user: CurrentUser, settings: Config
) -> DriftCheckOut:
    """Runs the real solver on a throwaway input. **Writes no plan changes.**

    Estimating what a 3.4 °C forecast move would do to a bike target is
    exactly the shortcut that tells an athlete the wrong number, so this pays
    for a full solve rather than approximating one.
    """
    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)
    race = session.get(Race, plan.race_id)
    forecast = (
        weather_service.fetch_for_race(session, race=race, settings=settings)
        if race is not None
        else None
    )

    assessment = drift_service.shadow_recompute(
        session,
        plan=plan,
        user=user,
        settings=settings,
        cause=DriftCause.FORECAST,
        forecast_snapshot=forecast,
    )
    event = drift_service.record(
        session, plan=plan, user=user, settings=settings, assessment=assessment
    )
    session.commit()

    return DriftCheckOut(
        plan_id=plan.id,
        cause=assessment.cause,
        severity=assessment.severity,
        material=assessment.material,
        now_infeasible=assessment.now_infeasible,
        infeasible_message=assessment.infeasible_message,
        projected_minutes=assessment.projected_minutes,
        worst_margin_minutes=assessment.worst_margin_minutes,
        field_deltas=[delta.to_dict() for delta in assessment.deltas],
        event=DriftEventOut.model_validate(event) if event else None,
    )


@router.post("/drift/{event_id}/apply", summary="Accept the change and re-solve")
def apply_drift(
    event_id: UUID, session: DbSession, user: CurrentUser, settings: Config
) -> PlanDetail:
    """Free, and always a new version."""
    event = drift_service.get_event(session, event_id=event_id, user=user)
    plan = drift_service.apply(session, event=event, user=user, settings=settings)
    session.commit()
    return plan_detail(session, plan)


@router.post("/drift/{event_id}/dismiss", summary="Keep the plan as it is")
def dismiss_drift(event_id: UUID, session: DbSession, user: CurrentUser) -> DriftEventOut:
    """The event stays, marked dismissed.

    Deleting it would lose the fact that the athlete was told and chose, and
    that record is the difference between an informed decision and a surprise
    on race morning.
    """
    event = drift_service.get_event(session, event_id=event_id, user=user)
    drift_service.dismiss(session, event=event)
    session.commit()
    return DriftEventOut.model_validate(event)
