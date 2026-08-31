"""Races: the athlete's entry into a course edition.

**This is what the plan builder's first step produces.** A plan is solved for
a race — this person, this course, this date — so without a race there is
nothing to plan. The race also carries the date and start time every wall-clock
number on the race card is measured from.

A race is always pinned to the course bundle that is current *when it is
created*. It stays pinned until the athlete applies a drift event, which is
what stops a republished course silently changing a solved plan (Law 3).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import APIRouter, status
from sqlalchemy import select

from raceos.api.deps import CurrentUser, DbSession
from raceos.api.errors import Conflict, InvalidInput, NotFound
from raceos.api.schemas.race import RaceCreate, RaceOut, RaceUpdate
from raceos.db.models import Course, CourseBundle, Plan, Race
from raceos.domain.enums import PlanStatus, RaceStatus
from raceos.services import course_service

router = APIRouter(prefix="/api/v1/races", tags=["races"])

#: How far ahead a race may be entered. A date beyond this is almost always a
#: typo in the year, and catching it here beats an athlete discovering their
#: plan is for 2035.
MAX_YEARS_AHEAD = 5


def _out(session: DbSession, race: Race, today: date) -> RaceOut:
    out = RaceOut.model_validate(race)
    course = session.get(Course, race.course_id)
    bundle = session.get(CourseBundle, race.course_bundle_id)
    if course is not None:
        out.course_name = course.name
        out.course_place = course.place
        out.course_slug = course.slug
        out.distance_type = course.distance_type.value
        out.timezone = course.timezone
    out.bundle_version = bundle.version if bundle else None
    out.days_away = (race.event_date - today).days

    plan = session.scalar(
        select(Plan)
        .where(
            Plan.race_id == race.id,
            Plan.status.in_(
                (PlanStatus.ACTIVE, PlanStatus.DRAFT, PlanStatus.PENDING_ATHLETE_APPROVAL)
            ),
        )
        .order_by(Plan.version.desc())
        .limit(1)
    )
    if plan is not None:
        out.plan_id = plan.id
        out.plan_status = plan.status.value
    return out


@router.post("", status_code=status.HTTP_201_CREATED, summary="Enter a race")
def create_race(payload: RaceCreate, session: DbSession, user: CurrentUser) -> RaceOut:
    """Pin this athlete to a course on a date.

    The bundle is resolved *now* and stored, rather than looked up per read:
    a plan solved against version 2026.1 must keep describing that geometry
    even after 2026.2 is published.
    """
    today = datetime.now(UTC).date()
    if payload.event_date.year - today.year > MAX_YEARS_AHEAD:
        raise InvalidInput(
            f"{payload.event_date.isoformat()} is more than {MAX_YEARS_AHEAD} "
            f"years away — check the year.",
            field="event_date",
        )

    course = course_service._load_course(session, payload.course_ref)
    bundle = course_service._active_bundle(session, course.id)
    if bundle is None:
        raise Conflict(
            f"{course.name} has no published course data yet, so it cannot be " f"planned for."
        )

    existing = session.scalar(
        select(Race).where(
            Race.user_id == user.id,
            Race.course_id == course.id,
            Race.event_date == payload.event_date,
        )
    )
    if existing is not None:
        # Not an error worth blocking on: the athlete meant this race, and
        # returning it lets a double-submit land on the same row rather than
        # creating a duplicate they then have to delete.
        return _out(session, existing, today)

    race = Race(
        user_id=user.id,
        course_id=course.id,
        course_bundle_id=bundle.id,
        event_date=payload.event_date,
        start_time_local=payload.start_time_local,
        status=RaceStatus.UPCOMING if payload.event_date >= today else RaceStatus.COMPLETED,
        bib=payload.bib,
    )
    session.add(race)
    session.commit()
    return _out(session, race, today)


@router.get("", summary="Races this athlete has entered")
def list_races(session: DbSession, user: CurrentUser) -> list[RaceOut]:
    """Soonest first, past races last."""
    today = datetime.now(UTC).date()
    races = session.scalars(select(Race).where(Race.user_id == user.id).order_by(Race.event_date))
    return [_out(session, race, today) for race in races]


def _owned(session: DbSession, race_id: UUID, user: CurrentUser) -> Race:
    race = session.get(Race, race_id)
    if race is None or race.user_id != user.id:
        raise NotFound("Race not found.")
    return race


@router.get("/{race_id}", summary="One race")
def get_race(race_id: UUID, session: DbSession, user: CurrentUser) -> RaceOut:
    return _out(session, _owned(session, race_id, user), datetime.now(UTC).date())


@router.patch("/{race_id}", summary="Correct a date, time or bib")
def update_race(
    race_id: UUID, payload: RaceUpdate, session: DbSession, user: CurrentUser
) -> RaceOut:
    """Editing the date does **not** re-solve anything.

    An existing plan keeps its numbers until the athlete asks for a re-solve —
    silently recomputing behind them is the thing Law 3 forbids.
    """
    race = _owned(session, race_id, user)
    if payload.event_date is not None:
        race.event_date = payload.event_date
    if payload.start_time_local is not None:
        race.start_time_local = payload.start_time_local
    if payload.bib is not None:
        race.bib = payload.bib
    if payload.status is not None:
        race.status = payload.status
    session.commit()
    return _out(session, race, datetime.now(UTC).date())


@router.delete(
    "/{race_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Remove a race that has no solved plan",
)
def delete_race(race_id: UUID, session: DbSession, user: CurrentUser) -> None:
    """Refused once a plan has been solved against it.

    A solved plan is a record of a decision the athlete made, and the race is
    the only thing that says which event it was for. Deleting it would leave
    an orphan.
    """
    race = _owned(session, race_id, user)
    solved = session.scalar(
        select(Plan).where(Plan.race_id == race.id, Plan.solved_at.is_not(None))
    )
    if solved is not None:
        raise Conflict(
            "This race has a solved plan, so it cannot be removed. Delete the "
            "plan first if you no longer want it."
        )
    for draft in session.scalars(select(Plan).where(Plan.race_id == race.id)):
        session.delete(draft)
    session.delete(race)
    session.commit()
