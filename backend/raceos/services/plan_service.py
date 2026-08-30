"""Plans: draft, solve, version, re-solve, override, approve.

**A solve never mutates an existing plan.** It inserts a new ``plans`` row with
``version = max(version) + 1`` and all its child rows in one transaction, then
flips the previous row's status. Previous versions stay readable forever,
because post-race comparison must reference *the version that was live at race
time*, not the current one — and because Law 3 says plans never change under a
user.

**Solves run synchronously.** The measured cost is well inside the 6 s SLA
(1.35 s on the largest real course), so the request returns the solved plan.
The ``solve_jobs`` table and the 202 path exist as an escape hatch and are
fully implemented, because the frontend already handles them and because a
solve that ever ran long needs somewhere to go.

**Idempotent by hash.** If the plan's current ``solve_input_hash`` already has
a solved version, that version is returned rather than recomputed. Identical
input implies byte-identical output, so recomputing would burn the SLA to
produce the same bytes.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as clock_time
from uuid import UUID
from zoneinfo import ZoneInfo

from shapely.geometry import LineString
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raceos.api.errors import Conflict, Forbidden, Infeasible, InfeasibleDetails, NotFound
from raceos.config import Settings
from raceos.db.models import (
    Constraint,
    Course,
    CourseBundle,
    CourseBundleLeg,
    OverrideEvent,
    Plan,
    PlanAidAction,
    PlanBag,
    PlanBagItem,
    PlanConstraintRef,
    PlanFuelling,
    PlanGate,
    PlanSegment,
    PlanSplit,
    Race,
    SolveTiming,
    User,
)
from raceos.domain.enums import (
    CONSTRAINT_KEYS,
    Feasibility,
    PlanStatus,
    RiskLevel,
)
from raceos.logging import get_logger
from raceos.solver.adapters import from_pipeline_bundle
from raceos.solver.errors import MissingConstraint
from raceos.solver.models import (
    SCHEMA_VERSION,
    AthleteSnapshot,
    BikeSetup,
    ConstraintValue,
    CourseBundleSnapshot,
    EventSpec,
    ForecastSnapshot,
    GoalSpec,
    SolveInput,
    SolveOptions,
    SolveOutput,
)
from raceos.solver.models import (
    Infeasibility as SolverInfeasibility,
)
from raceos.solver.pipeline import SolveInfeasible
from raceos.solver.pipeline import solve as run_solver
from raceos.solver.serialisation import solve_input_hash

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Building the solver's input
# ---------------------------------------------------------------------------


def _utc_offset_hours(timezone: str, on_date: date) -> float:
    """The offset **in effect on the event date**, including summer time.

    Not the current offset: a race in September solved in January must use
    September's offset, or the head-torch calculation is an hour out — which
    is exactly the size of the decision it feeds.
    """
    try:
        zone = ZoneInfo(timezone)
    except Exception:
        return 0.0
    reference = datetime.combine(on_date, clock_time(12, 0), tzinfo=zone)
    offset = reference.utcoffset()
    return offset.total_seconds() / 3600.0 if offset else 0.0


def build_course_snapshot(session: Session, bundle: CourseBundle) -> CourseBundleSnapshot:
    """Adapt a stored bundle into the solver's frozen snapshot.

    Rebuilds the payload shape the pipeline emits and reuses that adapter,
    rather than writing a third reader of the same data. The Z ordinates of
    each leg's geometry **are** the elevation series; nothing else is read.
    """
    legs = list(
        session.scalars(select(CourseBundleLeg).where(CourseBundleLeg.bundle_id == bundle.id))
    )
    course = session.get(Course, bundle.course_id)
    assert course is not None

    from geoalchemy2.shape import to_shape

    payload = {
        "course": {
            "slug": course.slug,
            "distance_type": course.distance_type.value,
        },
        "course_bundle": {
            "segments": bundle.segments,
            "barriers": bundle.barriers,
            "aid_stations": bundle.aid_stations,
            "elevation_source": bundle.elevation_source,
        },
        "course_bundle_legs": [
            {
                "leg": leg.leg.value,
                "distance_m": float(leg.distance_m),
                "surface_quality": leg.surface_quality.value,
                "geometry": _ewkt(to_shape(leg.geometry)),  # type: ignore[arg-type]
            }
            for leg in legs
        ],
    }
    return from_pipeline_bundle(payload)


def _ewkt(geometry: LineString) -> str:
    points = ", ".join(f"{x} {y} {z}" for x, y, z in geometry.coords)
    return f"SRID=4326;LINESTRING Z ({points})"


def build_athlete_snapshot(session: Session, user: User) -> AthleteSnapshot:
    """Freeze the athlete's constraints as they are right now.

    A missing constraint raises :class:`MissingConstraint` naming the key —
    never a silent default, because a defaulted constraint produces a plan for
    an athlete who does not exist and nothing downstream can tell them apart.
    """
    rows = {
        row.key: row
        for row in session.scalars(select(Constraint).where(Constraint.user_id == user.id))
    }
    missing = [key for key in CONSTRAINT_KEYS if key not in rows]
    if missing:
        raise MissingConstraint(missing[0])

    constraints = tuple(
        ConstraintValue(
            key=key,
            value=float(rows[key].value),
            unit=rows[key].unit,
            source=rows[key].source,
            measured_at_temp_c=_optional_float(rows[key].measured_at_temp_c),
        )
        # A fixed order, never a dict's, so the input hash is stable.
        for key in CONSTRAINT_KEYS
    )
    setup = (
        BikeSetup(position=user.bike_position, helmet=user.bike_helmet)
        if user.bike_position is not None and user.bike_helmet is not None
        else None
    )
    return AthleteSnapshot(level=user.level, constraints=constraints, bike_setup=setup)


def build_forecast_snapshot(stored: dict[str, object] | None) -> ForecastSnapshot:
    """From the plan's frozen forecast blob, or a documented neutral default.

    A plan solved before any forecast exists still needs numbers. The default
    is mild and explicit rather than hidden: `pressure_hpa` and
    `cloud_cover_pct` are left absent so they land in ``assumed_fields`` and
    the UI can mark what rested on an assumption.
    """
    data = stored or {}

    def number(key: str, fallback: float) -> float:
        raw = data.get(key, fallback)
        return float(raw) if isinstance(raw, int | float | str) else fallback

    return ForecastSnapshot(
        temp_c=number("temp_c", 18.0),
        humidity=number("humidity", 60.0),
        wind_speed_ms=number("wind_speed_ms", 3.0),
        conditions=str(data.get("conditions", "partly_cloudy")),
        water_temp_c=number("water_temp_c", 19.0),
        wind_dir_deg=_optional_float(data.get("wind_dir_deg")),
        pressure_hpa=_optional_float(data.get("pressure_hpa")),
        cloud_cover_pct=_optional_float(data.get("cloud_cover_pct")),
    )


def _optional_float(raw: object) -> float | None:
    """`None` stays `None`; anything numeric becomes a float.

    Written once rather than inline at each site: these values arrive from a
    JSONB blob, so their static type is `object` and every call site would
    otherwise need the same narrowing.
    """
    if raw is None:
        return None
    if isinstance(raw, int | float | str):
        return float(raw)
    return None


def build_solve_input(
    session: Session, plan: Plan, user: User, *, carb_override: float | None = None
) -> SolveInput:
    race = session.get(Race, plan.race_id)
    assert race is not None
    bundle = session.get(CourseBundle, race.course_bundle_id)
    if bundle is None:
        raise NotFound("This race is not pinned to a course bundle.")
    course = session.get(Course, race.course_id)
    assert course is not None

    return SolveInput(
        schema_version=SCHEMA_VERSION,
        athlete=build_athlete_snapshot(session, user),
        course=build_course_snapshot(session, bundle),
        goal=GoalSpec(
            goal_minutes=float(plan.goal_minutes) if plan.goal_minutes else None,
            risk=_risk_from(plan),
            first_timer=user.level.value == "first",
        ),
        forecast=build_forecast_snapshot(plan.forecast_snapshot),
        event=EventSpec(
            event_date=race.event_date,
            start_time_local=race.start_time_local,
            timezone=course.timezone,
            lat=float(course.lat),
            lng=float(course.lng),
            utc_offset_hours=_utc_offset_hours(course.timezone, race.event_date),
        ),
        options=SolveOptions(
            carb_override=carb_override,
            night_flag=bool((plan.constraints_snapshot or {}).get("night_flag", False)),
            preview_only=False,
        ),
    )


def replace_forecast(request: SolveInput, snapshot: dict[str, object]) -> SolveInput:
    """A copy of *request* with a different forecast. The original is untouched.

    Used by the drift shadow recompute: the plan keeps the forecast it was
    solved against until the athlete applies the change, so the newer one can
    only ever exist on a throwaway copy of the input.
    """
    return dataclasses.replace(request, forecast=build_forecast_snapshot(snapshot))


def _risk_from(plan: Plan) -> RiskLevel:
    raw = (plan.constraints_snapshot or {}).get("risk")
    try:
        return RiskLevel(raw) if raw else RiskLevel.BALANCED
    except ValueError:
        return RiskLevel.BALANCED


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


def create_draft(session: Session, *, user: User, race_id: UUID) -> Plan:
    race = session.get(Race, race_id)
    if race is None or race.user_id != user.id:
        raise NotFound("Race not found.")

    existing = session.scalar(
        select(Plan).where(Plan.race_id == race_id, Plan.status == PlanStatus.DRAFT)
    )
    if existing is not None:
        return existing

    plan = Plan(
        race_id=race_id,
        user_id=user.id,
        status=PlanStatus.DRAFT,
        version=_next_version(session, race_id),
        feasibility=Feasibility.NOT_SOLVED,
    )
    session.add(plan)
    session.flush()
    return plan


def _next_version(session: Session, race_id: UUID) -> int:
    highest = session.scalar(select(func.max(Plan.version)).where(Plan.race_id == race_id))
    return int(highest or 0) + 1


def patch_draft(session: Session, *, plan: Plan, user: User, changes: dict[str, object]) -> Plan:
    """Each builder step persists immediately.

    Drafts are saved continuously and independently of solve success, which is
    what lets Part 5.4's promise hold: on a solver crash the athlete's inputs
    survive exactly as entered.
    """
    require_owner(plan, user)
    if plan.status not in (PlanStatus.DRAFT, PlanStatus.PENDING_ATHLETE_APPROVAL):
        raise Conflict("Only a draft can be edited. Re-solve to make a new version.")

    if "goal_minutes" in changes:
        raw = changes["goal_minutes"]
        plan.goal_minutes = float(raw) if raw is not None else None  # type: ignore[arg-type]

    scratch = dict(plan.constraints_snapshot or {})
    for key in ("risk", "night_flag", "readiness_note"):
        if key in changes:
            scratch[key] = changes[key]
    plan.constraints_snapshot = scratch

    if "forecast" in changes and isinstance(changes["forecast"], dict):
        plan.forecast_snapshot = changes["forecast"]

    session.flush()
    return plan


def require_owner(plan: Plan, user: User) -> None:
    if plan.user_id != user.id:
        raise Forbidden("This plan belongs to another athlete.")


# ---------------------------------------------------------------------------
# Solving
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SolveResult:
    plan: Plan
    reused: bool
    duration_ms: int


def solve_plan(
    session: Session,
    *,
    plan: Plan,
    user: User,
    settings: Settings,
    carb_override: float | None = None,
    force: bool = False,
) -> SolveResult:
    """Solve, and persist the result atomically as a new version.

    Everything is written in one transaction: ``plans`` plus every child row.
    A partial write is impossible, which matters because a plan with splits
    but no gates would render as a plan with no cut-off information — the one
    thing this product exists to provide.
    """
    request = build_solve_input(session, plan, user, carb_override=carb_override)
    input_hash = solve_input_hash(request)

    if not force:
        existing = session.scalar(
            select(Plan)
            .where(
                Plan.race_id == plan.race_id,
                Plan.solve_input_hash == input_hash,
                Plan.status.in_((PlanStatus.ACTIVE, PlanStatus.PAST)),
            )
            .order_by(Plan.version.desc())
            .limit(1)
        )
        if existing is not None:
            # Identical input implies byte-identical output, so recomputing
            # would burn the SLA to produce the same bytes.
            return SolveResult(plan=existing, reused=True, duration_ms=0)

    started = time.perf_counter()
    try:
        output = run_solver(request)
    except SolveInfeasible as verdict:
        duration_ms = int((time.perf_counter() - started) * 1000)
        _record_timing(session, plan, duration_ms, settings)
        detail = verdict.infeasibility
        # A successful solve with an infeasible verdict — not a server error.
        # Nothing is persisted: there is no plan to persist.
        raise Infeasible(
            _infeasible_message(detail),
            InfeasibleDetails(
                barrier=detail.barrier,
                miss_minutes=detail.miss_minutes,
                levers=detail.levers,
                tightest_barrier=detail.tightest_barrier,
                tightest_miss_minutes=detail.tightest_miss_minutes,
            ),
        ) from verdict

    duration_ms = int((time.perf_counter() - started) * 1000)
    solved = _persist(
        session,
        plan=plan,
        user=user,
        request=request,
        output=output,
        input_hash=input_hash,
    )
    _record_timing(session, solved, duration_ms, settings)
    session.flush()
    return SolveResult(plan=solved, reused=False, duration_ms=duration_ms)


def _infeasible_message(detail: SolverInfeasibility) -> str:
    """Built from the **earliest missed** barrier, never the tightest (§F.5).

    "You miss the finish by 132 minutes" and "you miss the bike cut-off by ten"
    lead to opposite decisions, and only the second is true.
    """
    label = detail.barrier.replace("_", " ")
    return f"This plan misses the {label} by {detail.miss_minutes:.0f} minutes."


def _record_timing(session: Session, plan: Plan, duration_ms: int, settings: Settings) -> None:
    """Solver latency is a **real measured series**, never a display constant.

    The Admin dashboard's P50/P95/P99 are aggregated from these rows.
    """
    session.add(
        SolveTiming(
            plan_id=plan.id,
            total_ms=duration_ms,
            stage_timings_ms={},
            exceeded_sla=duration_ms > settings.solver_sla_ms,
        )
    )
    if duration_ms > settings.solver_sla_ms:
        logger.warning(
            "solve exceeded the SLA",
            extra={"duration_ms": duration_ms, "sla_ms": settings.solver_sla_ms},
        )


def _persist(
    session: Session,
    *,
    plan: Plan,
    user: User,
    request: SolveInput,
    output: SolveOutput,
    input_hash: str,
) -> Plan:
    """Insert a new version and supersede the previous active one."""
    previous = session.scalar(
        select(Plan).where(Plan.race_id == plan.race_id, Plan.status == PlanStatus.ACTIVE)
    )

    if plan.status is PlanStatus.DRAFT:
        solved = plan
    else:
        solved = Plan(
            race_id=plan.race_id,
            user_id=plan.user_id,
            goal_minutes=plan.goal_minutes,
            constraints_snapshot=plan.constraints_snapshot,
            forecast_snapshot=plan.forecast_snapshot,
            built_by_coach_id=plan.built_by_coach_id,
            version=_next_version(session, plan.race_id),
        )
        session.add(solved)
        session.flush()

    if previous is not None and previous.id != solved.id:
        # Demote the outgoing version and FLUSH before promoting the new one.
        # `uq_plans_one_active_per_race` is a partial unique index, so if both
        # UPDATEs reach the database in one flush the ordering is SQLAlchemy's
        # to choose and the constraint fires roughly half the time. Making the
        # order explicit is the fix; widening the constraint would not be.
        previous.status = PlanStatus.PAST
        previous.superseded_by_plan_id = solved.id
        session.flush()

    solved.status = (
        PlanStatus.PENDING_ATHLETE_APPROVAL
        if solved.built_by_coach_id and solved.approved_at is None
        else PlanStatus.ACTIVE
    )
    solved.solved_at = _now()
    solved.solve_input_hash = input_hash
    solved.projected_minutes = output.projected_minutes
    solved.feasibility = output.feasibility
    solved.worst_margin_minutes = output.worst_margin_minutes
    solved.binding_constraint_key = output.binding_constraint_key
    solved.assumed_fields = list(output.assumed_fields)
    solved.constraints_snapshot = {
        **(solved.constraints_snapshot or {}),
        "constraints": [
            {"key": c.key, "value": c.value, "unit": c.unit, "source": c.source.value}
            for c in request.athlete.constraints
        ],
        "level": request.athlete.level.value,
    }
    solved.forecast_snapshot = {
        "temp_c": request.forecast.temp_c,
        "humidity": request.forecast.humidity,
        "wind_speed_ms": request.forecast.wind_speed_ms,
        "wind_dir_deg": request.forecast.wind_dir_deg,
        "conditions": request.forecast.conditions,
        "water_temp_c": request.forecast.water_temp_c,
        "pressure_hpa": request.forecast.pressure_hpa,
        "cloud_cover_pct": request.forecast.cloud_cover_pct,
    }
    solved.readiness_fraction, solved.readiness_note = _readiness(output)

    _replace_children(session, solved, output)
    session.flush()
    return solved


def _readiness(output: SolveOutput) -> tuple[float, str]:
    """A real fraction of real steps, not a decorative percentage."""
    steps = {
        "solved": True,
        "feasible": output.feasibility is not Feasibility.NOT_SOLVED,
        "fuelling": output.fuelling.carb_g_per_hr > 0,
        "bags packed": all(bag.item_count > 0 for bag in output.bags[:3]),
        "no assumptions": not output.assumed_fields,
        "margins clear": output.margin_state.value == "clear",
        "aid plan": bool(output.aid_actions),
    }
    done = sum(1 for value in steps.values() if value)
    outstanding = [name for name, value in steps.items() if not value]
    note = "Ready to race." if not outstanding else f"Outstanding: {outstanding[0]}."
    return done / len(steps), note


def _replace_children(session: Session, plan: Plan, output: SolveOutput) -> None:
    """Child rows are rewritten wholesale, never patched.

    A solve produces a complete plan; merging it into the previous one would
    let a stale segment survive a re-solve and appear beside fresh ones.
    """
    for model in (
        PlanSegment,
        PlanSplit,
        PlanGate,
        PlanAidAction,
        PlanConstraintRef,
        PlanFuelling,
    ):
        for row in session.scalars(select(model).where(model.plan_id == plan.id)):
            session.delete(row)
    for bag in session.scalars(select(PlanBag).where(PlanBag.plan_id == plan.id)):
        session.delete(bag)
    session.flush()

    for segment in output.segments:
        session.add(
            PlanSegment(
                plan_id=plan.id,
                ordinal=segment.ordinal,
                name=segment.name,
                leg=segment.leg,
                from_km=segment.from_km,
                to_km=segment.to_km,
                terrain_desc=segment.terrain_desc,
                target_watts=segment.target_watts,
                target_pace_sec_per_km=segment.target_pace_sec_per_km,
                target_minutes=segment.target_minutes,
                note=segment.note or None,
            )
        )

    for split in output.splits:
        session.add(
            PlanSplit(
                plan_id=plan.id,
                leg=split.leg,
                distance=split.distance,
                target_pace_or_power=split.target_pace_or_power,
                unit=split.unit,
                split_minutes=split.split_minutes,
                note=split.note or None,
            )
        )

    for gate in output.gates:
        session.add(
            PlanGate(
                plan_id=plan.id,
                name=gate.name,
                leg=gate.leg,
                limit_minutes=gate.limit_minutes,
                eta_minutes=gate.eta_minutes,
                margin_minutes=gate.margin_minutes,
                load_pct=gate.load_pct,
                state=gate.state,
            )
        )

    fuelling = output.fuelling
    session.add(
        PlanFuelling(
            plan_id=plan.id,
            carb_g_per_hr=fuelling.carb_g_per_hr,
            fluid_ml_per_hr=fuelling.fluid_ml_per_hr,
            sodium_mg_per_hr=fuelling.sodium_mg_per_hr,
            caffeine_mg_total=fuelling.caffeine_mg_total,
            total_carb_g=fuelling.total_carb_g,
            overridden=fuelling.overridden,
            requires_multiple_transportable=fuelling.requires_multiple_transportable,
            binding_carb_key=fuelling.binding_carb_key,
            binding_fluid_key=fuelling.binding_fluid_key,
            binding_sodium_key=fuelling.binding_sodium_key,
            binding_caffeine_key=fuelling.binding_caffeine_key,
        )
    )

    for action in output.aid_actions:
        session.add(
            PlanAidAction(
                plan_id=plan.id,
                ordinal=action.ordinal,
                at_clock_minutes=action.at_clock_minutes,
                at_km=action.at_km,
                leg=action.leg,
                station_name=action.station_name,
                action_text=action.action_text,
                cumulative_carb_g=action.cumulative_carb_g,
            )
        )

    for solved_bag in output.bags:
        bag_row = PlanBag(
            plan_id=plan.id,
            key=solved_bag.key,
            name=solved_bag.name,
            when_label=solved_bag.when_label,
            item_count=solved_bag.item_count,
        )
        session.add(bag_row)
        session.flush()
        for ordinal, item in enumerate(solved_bag.items, start=1):
            session.add(
                PlanBagItem(
                    bag_id=bag_row.id,
                    ordinal=ordinal,
                    name=item.name,
                    qty=item.qty,
                    note=item.note,
                    reason_constraint_key=item.reason_constraint_key,
                    reason_text=item.reason_text,
                )
            )

    for ref in output.constraint_refs:
        session.add(
            PlanConstraintRef(
                plan_id=plan.id,
                key=ref.key,
                name=ref.name,
                value=ref.value,
                unit=ref.unit,
                source_label=ref.source_label,
                binding=ref.binding,
                description=ref.description or None,
                affects_text=ref.affects_text or None,
                override_text=ref.override_text or None,
            )
        )
    session.flush()


# ---------------------------------------------------------------------------
# Overrides and approval
# ---------------------------------------------------------------------------


def record_override(
    session: Session,
    *,
    plan: Plan,
    user: User,
    constraint_key: str,
    new_value: float,
    reason: str | None,
) -> OverrideEvent:
    """Log the override **before** the solve that consumes it (§5.1).

    The solver refuses a carb override that has no logged event behind it, so
    the order is a requirement rather than bookkeeping: an override is a
    decision the athlete made, and the record of it must outlive the plan.
    """
    require_owner(plan, user)
    current = session.scalar(
        select(Constraint).where(Constraint.user_id == user.id, Constraint.key == constraint_key)
    )
    event = OverrideEvent(
        user_id=user.id,
        plan_id=plan.id,
        constraint_key=constraint_key,
        overridden_from=float(current.value) if current else 0.0,
        overridden_to=new_value,
        reason=reason,
    )
    session.add(event)
    session.flush()
    return event


def mark_built_by_coach(session: Session, *, plan: Plan, coach: User) -> Plan:
    """Stamp the plan before it is solved.

    Before, not after: :func:`_persist` reads ``built_by_coach_id`` to decide
    whether the new version lands ``pending_athlete_approval`` or ``active``.
    Stamping afterwards would produce a plan that went live in the athlete's
    account without them ever seeing it.
    """
    plan.built_by_coach_id = coach.id
    plan.approved_at = None
    session.flush()
    return plan


def approve_plan(session: Session, *, plan: Plan, user: User) -> Plan:
    """A coach-built plan is not the athlete's plan until they approve it."""
    require_owner(plan, user)
    if plan.status is not PlanStatus.PENDING_ATHLETE_APPROVAL:
        raise Conflict("This plan does not need approval.")

    previous = session.scalar(
        select(Plan).where(
            Plan.race_id == plan.race_id,
            Plan.status == PlanStatus.ACTIVE,
            Plan.id != plan.id,
        )
    )
    if previous is not None:
        previous.status = PlanStatus.PAST
        previous.superseded_by_plan_id = plan.id

    plan.approved_at = _now()
    plan.status = PlanStatus.ACTIVE
    session.flush()
    return plan


def get_plan(session: Session, *, plan_id: UUID, user: User) -> Plan:
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise NotFound("Plan not found.")
    require_owner(plan, user)
    return plan


def list_plans(session: Session, *, user: User) -> list[Plan]:
    return list(
        session.scalars(
            select(Plan)
            .where(Plan.user_id == user.id)
            .order_by(Plan.created_at.desc(), Plan.version.desc())
        )
    )


def list_versions(session: Session, *, plan: Plan, user: User) -> list[Plan]:
    require_owner(plan, user)
    return list(
        session.scalars(
            select(Plan).where(Plan.race_id == plan.race_id).order_by(Plan.version.desc())
        )
    )


def delete_draft(session: Session, *, plan: Plan, user: User) -> None:
    """Drafts only. **Solved plans are never hard-deleted.**"""
    require_owner(plan, user)
    if plan.status is not PlanStatus.DRAFT:
        raise Conflict(
            "Only a draft can be deleted. Solved plans stay readable forever, "
            "because post-race comparison references the version live at race time."
        )
    session.delete(plan)
    session.flush()
