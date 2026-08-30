"""Drift detection by shadow recompute.

**Law 3: drift, never silent recalculation.** A plan's stored numbers are
never rewritten behind the athlete's back. When the world moves — a forecast,
a constraint, a republished course bundle — the solver is run again *against a
throwaway copy*, the two results are compared, and the difference is recorded
as a pending event. The plan itself is untouched until the athlete applies it,
and applying it produces a **new version**, so the plan they raced on stays
readable forever.

The recompute is a real solve, not a heuristic. Estimating what a 3.4 °C
forecast change would do to a bike target is exactly the kind of shortcut that
tells an athlete their power moved by the wrong amount.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.errors import Conflict, NotFound
from raceos.config import Settings
from raceos.db.models import (
    NotificationPreference,
    Plan,
    PlanDriftEvent,
    PlanGate,
    PlanSplit,
    Race,
    User,
)
from raceos.domain.enums import (
    DriftCause,
    DriftSensitivity,
    DriftSeverity,
    DriftStatus,
    NotificationSeverity,
    NotificationType,
    PlanStatus,
)
from raceos.logging import get_logger
from raceos.services import notification_service, plan_service
from raceos.solver.models import SolveOutput
from raceos.solver.pipeline import SolveInfeasible
from raceos.solver.pipeline import solve as run_solver
from raceos.solver.serialisation import solve_input_hash

logger = get_logger(__name__)

#: Fuelling deltas worth telling somebody about. Below these the number moves
#: but the athlete's actions do not: nobody re-packs a bag over 3 g/hr.
FUEL_THRESHOLDS: dict[str, float] = {
    "carb_g_per_hr": 5.0,
    "fluid_ml_per_hr": 50.0,
    "sodium_mg_per_hr": 100.0,
    "caffeine_mg_total": 50.0,
}

_LEG_LABEL = {"SWIM": "Swim", "BIKE": "Bike", "RUN": "Run"}


@dataclass(frozen=True)
class FieldDelta:
    key: str
    label: str
    from_value: str
    to_value: str
    #: Signed change in minutes where the field is a duration; ``None``
    #: otherwise. Kept numeric so severity is decided on arithmetic rather
    #: than on parsing the display strings back.
    delta_minutes: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "from": self.from_value,
            "to": self.to_value,
        }
        if self.delta_minutes is not None:
            out["delta_minutes"] = round(self.delta_minutes, 1)
        return out


@dataclass(frozen=True)
class DriftAssessment:
    """What a shadow recompute found. Nothing here has been written yet."""

    plan_id: UUID
    cause: DriftCause
    severity: DriftSeverity
    deltas: tuple[FieldDelta, ...]
    #: The recomputed worst margin, so the caller can say how close it now is.
    projected_minutes: float | None
    worst_margin_minutes: float | None
    #: True when the recompute could not produce a plan at all.
    now_infeasible: bool = False
    infeasible_message: str = ""

    @property
    def material(self) -> bool:
        return bool(self.deltas) or self.now_infeasible


def _minutes(value: object) -> float:
    return float(value)  # type: ignore[arg-type]


def _clock(minutes: float) -> str:
    """``h:mm`` — the same rendering the plan header uses."""
    total = int(round(minutes))
    sign = "-" if total < 0 else ""
    total = abs(total)
    return f"{sign}{total // 60}:{total % 60:02d}"


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def compare(
    session: Session, *, plan: Plan, recomputed: SolveOutput, settings: Settings
) -> tuple[list[FieldDelta], DriftSeverity]:
    """Diff the stored plan against a fresh solve.

    Only *material* changes are reported. A split that moved by twelve seconds
    is real but not actionable, and an alert an athlete cannot act on trains
    them to ignore the ones they can.
    """
    deltas: list[FieldDelta] = []
    threshold = settings.drift_split_threshold_minutes

    stored_splits = {
        row.leg.value: row
        for row in session.scalars(select(PlanSplit).where(PlanSplit.plan_id == plan.id))
    }
    for split in recomputed.splits:
        stored = stored_splits.get(split.leg.value)
        if stored is None:
            continue
        change = split.split_minutes - _minutes(stored.split_minutes)
        if abs(change) >= threshold:
            deltas.append(
                FieldDelta(
                    key=f"split.{split.leg.value.lower()}",
                    label=_LEG_LABEL.get(split.leg.value, split.leg.value),
                    from_value=_clock(_minutes(stored.split_minutes)),
                    to_value=_clock(split.split_minutes),
                    delta_minutes=change,
                )
            )
        if str(stored.target_pace_or_power) != split.target_pace_or_power:
            deltas.append(
                FieldDelta(
                    key=f"target.{split.leg.value.lower()}",
                    label=f"{_LEG_LABEL.get(split.leg.value, split.leg.value)} target",
                    from_value=f"{stored.target_pace_or_power}{stored.unit}",
                    to_value=f"{split.target_pace_or_power}{split.unit}",
                )
            )

    stored_gates = {
        row.name: row
        for row in session.scalars(select(PlanGate).where(PlanGate.plan_id == plan.id))
    }
    severity = DriftSeverity.NORMAL
    for gate in recomputed.gates:
        stored_gate = stored_gates.get(gate.name)
        if stored_gate is None:
            continue
        change = gate.margin_minutes - _minutes(stored_gate.margin_minutes)
        if abs(change) >= threshold:
            deltas.append(
                FieldDelta(
                    key=f"margin.{gate.name}",
                    label=gate.name.replace("_", " ").capitalize() + " margin",
                    from_value=_clock(_minutes(stored_gate.margin_minutes)),
                    to_value=_clock(gate.margin_minutes),
                    delta_minutes=change,
                )
            )
        # Severity is decided on the *recomputed* margin, not on how far it
        # moved: a margin that was always thin and stayed thin is still the
        # thing worth shouting about.
        if gate.margin_minutes < settings.drift_margin_risk_minutes:
            severity = DriftSeverity.CUTOFF_RISK

    fuelling = plan_service_fuelling(session, plan)
    if fuelling is not None:
        for key, limit in FUEL_THRESHOLDS.items():
            before = float(getattr(fuelling, key))
            after = float(getattr(recomputed.fuelling, key))
            if abs(after - before) >= limit:
                deltas.append(
                    FieldDelta(
                        key=f"fuelling.{key}",
                        label=key.replace("_", " ").replace(" per hr", "/hr"),
                        from_value=f"{before:.0f}",
                        to_value=f"{after:.0f}",
                    )
                )

    return deltas, severity


def plan_service_fuelling(session: Session, plan: Plan) -> Any:
    from raceos.db.models import PlanFuelling

    return session.scalar(select(PlanFuelling).where(PlanFuelling.plan_id == plan.id))


# ---------------------------------------------------------------------------
# The shadow recompute
# ---------------------------------------------------------------------------


def shadow_recompute(
    session: Session,
    *,
    plan: Plan,
    user: User,
    settings: Settings,
    cause: DriftCause,
    forecast_snapshot: dict[str, Any] | None = None,
) -> DriftAssessment:
    """Solve again with today's inputs. **Writes nothing.**

    ``forecast_snapshot`` overrides the plan's frozen forecast without
    touching it — that is what makes this a shadow: the plan keeps the
    forecast it was solved against until the athlete applies the drift.
    """
    request = plan_service.build_solve_input(session, plan, user)
    if forecast_snapshot is not None:
        request = plan_service.replace_forecast(request, forecast_snapshot)

    if plan.solve_input_hash and solve_input_hash(request) == plan.solve_input_hash:
        # Byte-identical input implies byte-identical output. Nothing moved.
        return DriftAssessment(
            plan_id=plan.id,
            cause=cause,
            severity=DriftSeverity.NORMAL,
            deltas=(),
            projected_minutes=None,
            worst_margin_minutes=None,
        )

    try:
        recomputed = run_solver(request)
    except SolveInfeasible as verdict:
        detail = verdict.infeasibility
        return DriftAssessment(
            plan_id=plan.id,
            cause=cause,
            severity=DriftSeverity.CUTOFF_RISK,
            deltas=(
                FieldDelta(
                    key=f"barrier.{detail.barrier}",
                    label=detail.barrier.replace("_", " ").capitalize(),
                    from_value="made",
                    to_value=f"missed by {detail.miss_minutes:.0f} min",
                    delta_minutes=-detail.miss_minutes,
                ),
            ),
            projected_minutes=None,
            worst_margin_minutes=None,
            now_infeasible=True,
            infeasible_message=(
                f"Re-solving against today's conditions misses the "
                f"{detail.barrier.replace('_', ' ')} by "
                f"{detail.miss_minutes:.0f} minutes."
            ),
        )

    deltas, severity = compare(session, plan=plan, recomputed=recomputed, settings=settings)
    return DriftAssessment(
        plan_id=plan.id,
        cause=cause,
        severity=severity,
        deltas=tuple(deltas),
        projected_minutes=recomputed.projected_minutes,
        worst_margin_minutes=recomputed.worst_margin_minutes,
    )


# ---------------------------------------------------------------------------
# Sensitivity
# ---------------------------------------------------------------------------


def _sensitivity(session: Session, user: User) -> DriftSensitivity:
    row = session.scalar(
        select(NotificationPreference).where(
            NotificationPreference.user_id == user.id,
            NotificationPreference.type_key == NotificationType.DRIFT,
        )
    )
    return row.drift_sensitivity if row else DriftSensitivity.BALANCED


def passes_sensitivity(assessment: DriftAssessment, sensitivity: DriftSensitivity) -> bool:
    """Whether this athlete asked to hear about a change of this size.

    Sensitivity governs *notification*, never detection: the event is recorded
    either way, so the plan page can always show what moved even when the
    athlete asked not to be told about it.
    """
    if sensitivity is DriftSensitivity.EVERYTHING:
        return assessment.material
    if sensitivity is DriftSensitivity.CRITICAL:
        return assessment.severity is DriftSeverity.CUTOFF_RISK or assessment.now_infeasible
    # Balanced: anything that moves a split or a margin, or risks a cut-off.
    return assessment.material


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def record(
    session: Session,
    *,
    plan: Plan,
    user: User,
    settings: Settings,
    assessment: DriftAssessment,
    notify: bool = True,
) -> PlanDriftEvent | None:
    """Persist a pending event and, if the athlete wants it, tell them.

    A second pending event for the same plan and cause replaces the first:
    two alerts saying the forecast moved are one alert with the newer numbers.
    """
    if not assessment.material:
        return None

    existing = session.scalar(
        select(PlanDriftEvent).where(
            PlanDriftEvent.plan_id == plan.id,
            PlanDriftEvent.cause == assessment.cause,
            PlanDriftEvent.status == DriftStatus.PENDING,
        )
    )
    if existing is not None:
        existing.detected_at = datetime.now(UTC)
        existing.severity = assessment.severity
        existing.field_deltas = [delta.to_dict() for delta in assessment.deltas]
        event = existing
    else:
        event = PlanDriftEvent(
            plan_id=plan.id,
            detected_at=datetime.now(UTC),
            cause=assessment.cause,
            severity=assessment.severity,
            field_deltas=[delta.to_dict() for delta in assessment.deltas],
            status=DriftStatus.PENDING,
        )
        session.add(event)
    session.flush()

    if notify and passes_sensitivity(assessment, _sensitivity(session, user)):
        notification_service.notify(
            session,
            user=user,
            settings=settings,
            type_key=(
                NotificationType.CUTOFF
                if assessment.severity is DriftSeverity.CUTOFF_RISK
                else NotificationType.DRIFT
            ),
            severity=(
                NotificationSeverity.BAD
                if assessment.severity is DriftSeverity.CUTOFF_RISK
                else NotificationSeverity.WARN
            ),
            title=_title(assessment),
            body=_body(assessment),
            tag="CUT-OFF RISK"
            if assessment.severity is DriftSeverity.CUTOFF_RISK
            else "PLAN DRIFT",
            race_id=plan.race_id,
            plan_id=plan.id,
            deltas=[delta.to_dict() for delta in assessment.deltas],
            cta_label="Review what changed",
            cta_href=f"/plan/{plan.id}?drift={event.id}",
        )

    logger.info(
        "drift.recorded",
        extra={
            "plan_id": str(plan.id),
            "cause": assessment.cause.value,
            "severity": assessment.severity.value,
            "delta_count": len(assessment.deltas),
        },
    )
    return event


def _title(assessment: DriftAssessment) -> str:
    """Built from the deltas, so the headline cannot contradict the detail."""
    if assessment.now_infeasible:
        return assessment.infeasible_message
    if not assessment.deltas:  # pragma: no cover - guarded by `material`
        return "Your plan changed."
    first = assessment.deltas[0]
    return f"{first.label} moves from {first.from_value} to {first.to_value}."


def _body(assessment: DriftAssessment) -> str:
    cause = {
        DriftCause.FORECAST: "The forecast for this race has moved",
        DriftCause.CONSTRAINT_CHANGE: "One of your constraints changed",
        DriftCause.COURSE_BUNDLE_CHANGE: "The course bundle for this race was republished",
    }[assessment.cause]
    count = len(assessment.deltas)
    noun = "number" if count == 1 else "numbers"
    tail = (
        " Re-solving is free and your current plan stays exactly as it is " "until you apply this."
    )
    return f"{cause} since this plan was solved. {count} {noun} would change.{tail}"


# ---------------------------------------------------------------------------
# Acting on an event
# ---------------------------------------------------------------------------


def get_event(session: Session, *, event_id: UUID, user: User) -> PlanDriftEvent:
    event = session.get(PlanDriftEvent, event_id)
    if event is None:
        raise NotFound("Drift event not found.")
    plan = session.get(Plan, event.plan_id)
    if plan is None or plan.user_id != user.id:
        raise NotFound("Drift event not found.")
    return event


def list_pending(session: Session, *, plan: Plan) -> list[PlanDriftEvent]:
    return list(
        session.scalars(
            select(PlanDriftEvent)
            .where(
                PlanDriftEvent.plan_id == plan.id,
                PlanDriftEvent.status == DriftStatus.PENDING,
            )
            .order_by(PlanDriftEvent.detected_at.desc())
        )
    )


def apply(
    session: Session,
    *,
    event: PlanDriftEvent,
    user: User,
    settings: Settings,
) -> Plan:
    """Re-solve and supersede. **Always free, always a new version.**

    The athlete is not charged for a change they did not ask for, and the
    version they raced on stays readable: post-race comparison references the
    version that was live at race time, not the current one.
    """
    if event.status is not DriftStatus.PENDING:
        raise Conflict(f"This drift event was already {event.status.value}.")

    plan = session.get(Plan, event.plan_id)
    if plan is None or plan.user_id != user.id:  # pragma: no cover - checked above
        raise NotFound("Plan not found.")
    if plan.status is PlanStatus.DRAFT:
        raise Conflict("A draft has nothing to drift from. Solve it first.")

    result = plan_service.solve_plan(session, plan=plan, user=user, settings=settings, force=True)
    event.status = DriftStatus.APPLIED
    event.applied_at = datetime.now(UTC)
    event.resulting_plan_id = result.plan.id
    session.flush()
    logger.info(
        "drift.applied",
        extra={"event_id": str(event.id), "resulting_plan_id": str(result.plan.id)},
    )
    return result.plan


def dismiss(session: Session, *, event: PlanDriftEvent) -> PlanDriftEvent:
    """Keep the plan as it is. The event stays, marked dismissed.

    Deleting it would lose the fact that the athlete was told and chose — and
    that record is the difference between an informed decision and a
    surprise on race morning.
    """
    if event.status is not DriftStatus.PENDING:
        raise Conflict(f"This drift event was already {event.status.value}.")
    event.status = DriftStatus.DISMISSED
    session.flush()
    return event


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------


def active_plans_for_race(session: Session, race_id: UUID) -> list[Plan]:
    return list(
        session.scalars(
            select(Plan).where(Plan.race_id == race_id, Plan.status == PlanStatus.ACTIVE)
        )
    )


def sweep_forecasts(
    session: Session, *, settings: Settings, horizon_days: int = 10
) -> dict[str, int]:
    """Re-check every plan whose race is inside the forecast horizon.

    Run by the scheduled job rather than on read: a drift the athlete has not
    opened the app to see still needs to reach them.
    """
    from datetime import timedelta

    from raceos.services import weather_service

    today = datetime.now(UTC).date()
    races = session.scalars(
        select(Race).where(
            Race.event_date >= today,
            Race.event_date <= today + timedelta(days=horizon_days),
        )
    )

    checked = 0
    raised = 0
    for race in races:
        for plan in active_plans_for_race(session, race.id):
            user = session.get(User, plan.user_id)
            if user is None:  # pragma: no cover - FK RESTRICT
                continue
            forecast = weather_service.fetch_for_race(session, race=race, settings=settings)
            if forecast is None:
                # A forecast is an improvement to a plan, not a precondition
                # for one. No forecast means nothing new to compare against.
                continue
            checked += 1
            assessment = shadow_recompute(
                session,
                plan=plan,
                user=user,
                settings=settings,
                cause=DriftCause.FORECAST,
                forecast_snapshot=forecast,
            )
            if record(session, plan=plan, user=user, settings=settings, assessment=assessment):
                raised += 1
    return {"checked": checked, "raised": raised}
