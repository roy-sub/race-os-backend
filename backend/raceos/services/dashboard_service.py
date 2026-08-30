"""The dashboard and My Plans, assembled from real rows.

Every number here is read, never synthesised: the projected time comes from
the plan that was solved, the days-away from the event date, the readiness
fraction from what the athlete has actually done. A dashboard that invents a
plausible figure is worse than one that says "not solved yet", because the
athlete cannot tell the two apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.errors import NotFound
from raceos.config import Settings
from raceos.db.models import (
    Course,
    CourseBundle,
    Plan,
    PlanDriftEvent,
    Race,
    User,
)
from raceos.domain.enums import (
    DriftStatus,
    Feasibility,
    PlanStatus,
    RaceStatus,
)
from raceos.services import notification_service

#: How close a race has to be before the product calls it race week.
RACE_WEEK_DAYS = 7


@dataclass(frozen=True)
class RaceCard:
    """One race as the dashboard shows it."""

    race_id: UUID
    course_id: UUID
    course_name: str
    course_place: str
    course_slug: str
    distance_type: str
    event_date: date
    start_time_local: str
    days_away: int
    race_status: str
    is_race_week: bool

    plan_id: UUID | None
    plan_version: int | None
    plan_status: str
    feasibility: str
    goal_minutes: float | None
    projected_minutes: float | None
    worst_margin_minutes: float | None
    readiness_fraction: float | None
    readiness_note: str | None
    shared: bool
    solved_at: datetime | None

    bundle_version: str | None
    has_pending_drift: bool
    drift_severity: str | None
    drift_summary: str | None
    #: The one thing this race is waiting on. Written for a person.
    next_action: str
    next_action_href: str


def _days_away(event_date: date, today: date) -> int:
    return (event_date - today).days


def _active_plan(session: Session, race_id: UUID) -> Plan | None:
    """The live version, or the newest draft when nothing is solved yet."""
    active = session.scalar(
        select(Plan)
        .where(Plan.race_id == race_id, Plan.status == PlanStatus.ACTIVE)
        .order_by(Plan.version.desc())
    )
    if active is not None:
        return active
    return session.scalar(
        select(Plan).where(Plan.race_id == race_id).order_by(Plan.version.desc()).limit(1)
    )


def _pending_drift(session: Session, plan_id: UUID) -> PlanDriftEvent | None:
    return session.scalar(
        select(PlanDriftEvent)
        .where(
            PlanDriftEvent.plan_id == plan_id,
            PlanDriftEvent.status == DriftStatus.PENDING,
        )
        .order_by(PlanDriftEvent.detected_at.desc())
    )


def _drift_summary(event: PlanDriftEvent) -> str:
    """One line naming what moved, built from the stored deltas.

    Derived from the numbers rather than written alongside them, so a summary
    that disagrees with the deltas is not expressible.
    """
    deltas = [d for d in (event.field_deltas or []) if isinstance(d, dict)]
    if not deltas:
        return f"{event.cause.value.replace('_', ' ').capitalize()} changed this plan."
    named = ", ".join(
        f"{delta.get('label') or delta.get('key') or 'a value'!s} "
        f"{delta.get('from')} → {delta.get('to')}"
        for delta in deltas[:3]
    )
    more = len(deltas) - 3
    return named + (f", and {more} more" if more > 0 else "")


def _next_action(
    *,
    plan: Plan | None,
    race_id: UUID,
    drift: PlanDriftEvent | None,
    days_away: int,
) -> tuple[str, str]:
    """What this race is waiting on, and where to go to do it."""
    if plan is None:
        return "Start a plan", f"/plan-builder?race={race_id}"
    if plan.status is PlanStatus.DRAFT:
        return "Finish and solve this plan", f"/plan-builder?race={race_id}"
    if plan.status is PlanStatus.PENDING_ATHLETE_APPROVAL:
        return "Approve your coach's plan", f"/plan/{plan.id}"
    if drift is not None:
        return "Review what changed", f"/plan/{plan.id}?drift={drift.id}"
    if 0 <= days_away <= RACE_WEEK_DAYS:
        return "Print the race card and pack", f"/plan/{plan.id}"
    return "Open plan", f"/plan/{plan.id}"


def build_race_card(session: Session, *, race: Race, today: date | None = None) -> RaceCard:
    day = today or datetime.now(UTC).date()
    course = session.get(Course, race.course_id)
    if course is None:  # pragma: no cover - FK RESTRICT
        raise NotFound("The course for this race no longer exists.")
    bundle = session.get(CourseBundle, race.course_bundle_id)
    plan = _active_plan(session, race.id)
    drift = _pending_drift(session, plan.id) if plan is not None else None
    days_away = _days_away(race.event_date, day)
    action, href = _next_action(plan=plan, race_id=race.id, drift=drift, days_away=days_away)

    return RaceCard(
        race_id=race.id,
        course_id=course.id,
        course_name=course.name,
        course_place=course.place,
        course_slug=course.slug,
        distance_type=course.distance_type.value,
        event_date=race.event_date,
        start_time_local=race.start_time_local.strftime("%H:%M"),
        days_away=days_away,
        race_status=race.status.value,
        is_race_week=0 <= days_away <= RACE_WEEK_DAYS,
        plan_id=plan.id if plan else None,
        plan_version=plan.version if plan else None,
        plan_status=plan.status.value if plan else "none",
        feasibility=(plan.feasibility if plan else Feasibility.NOT_SOLVED).value,
        goal_minutes=_number(plan.goal_minutes) if plan else None,
        projected_minutes=_number(plan.projected_minutes) if plan else None,
        worst_margin_minutes=_number(plan.worst_margin_minutes) if plan else None,
        readiness_fraction=_number(plan.readiness_fraction) if plan else None,
        readiness_note=plan.readiness_note if plan else None,
        shared=bool(plan.shared) if plan else False,
        solved_at=plan.solved_at if plan else None,
        bundle_version=bundle.version if bundle else None,
        has_pending_drift=drift is not None,
        drift_severity=drift.severity.value if drift else None,
        drift_summary=_drift_summary(drift) if drift else None,
        next_action=action,
        next_action_href=href,
    )


def _number(value: object) -> float | None:
    """``Numeric`` columns come back as ``Decimal``; JSON wants a float."""
    return None if value is None else float(value)  # type: ignore[arg-type]


def season(session: Session, *, user: User, today: date | None = None) -> list[RaceCard]:
    """Upcoming races, soonest first. The dashboard's spine."""
    day = today or datetime.now(UTC).date()
    races = session.scalars(
        select(Race)
        .where(
            Race.user_id == user.id,
            Race.status.in_((RaceStatus.UPCOMING, RaceStatus.DEFERRED)),
            Race.event_date >= day,
        )
        .order_by(Race.event_date)
    )
    return [build_race_card(session, race=race, today=day) for race in races]


def past_races(session: Session, *, user: User, today: date | None = None) -> list[RaceCard]:
    """Everything already run, newest first."""
    day = today or datetime.now(UTC).date()
    races = session.scalars(
        select(Race)
        .where(
            Race.user_id == user.id,
            (Race.event_date < day) | (Race.status == RaceStatus.COMPLETED),
        )
        .order_by(Race.event_date.desc())
    )
    return [build_race_card(session, race=race, today=day) for race in races]


def dashboard(
    session: Session, *, user: User, settings: Settings, today: date | None = None
) -> dict[str, Any]:
    """One request, everything the dashboard renders.

    Assembled server-side rather than left to five client calls, because the
    cards have to agree with each other: a "next race" derived from a
    different read than the season list is how a dashboard ends up showing two
    different next races.
    """
    day = today or datetime.now(UTC).date()
    upcoming = season(session, user=user, today=day)
    windows = notification_service.suppression_windows(session, user=user, settings=settings)

    return {
        "athlete": {
            "id": str(user.id),
            "name": user.name,
            "tier": user.tier.value,
        },
        "next_race": upcoming[0] if upcoming else None,
        "races": upcoming,
        "counts": {
            "upcoming": len(upcoming),
            "unsolved": sum(
                1
                for card in upcoming
                if card.plan_id is None or card.plan_status == PlanStatus.DRAFT.value
            ),
            "needs_review": sum(1 for card in upcoming if card.has_pending_drift),
            "unread_notifications": notification_service.unread_count(session, user=user),
        },
        # Surfaced rather than merely acted on: an athlete who notices the
        # alerts have gone quiet deserves to know it is deliberate.
        "quiet_windows": [
            {
                "race_id": str(window.race_id),
                "opens_at": window.opens_at.isoformat(),
                "closes_at": window.closes_at.isoformat(),
                "reason": window.reason,
            }
            for window in windows
        ],
    }
