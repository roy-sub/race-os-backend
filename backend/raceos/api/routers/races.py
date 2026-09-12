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

from raceos.api.deps import Config, CurrentUser, DbSession
from raceos.api.errors import Conflict, InvalidInput, NotFound
from raceos.api.schemas.race import (
    ForecastOut,
    RaceCreate,
    RaceOut,
    RaceUpdate,
    RaceWeekOut,
    RaceWeekTaskCreate,
    RaceWeekTaskOut,
    RaceWeekTaskPatch,
)
from raceos.db.models import Course, CourseBundle, Plan, Race, RaceWeekTask
from raceos.domain.enums import CourseAvailability, PlanStatus, RaceStatus
from raceos.services import course_service, race_week_service, weather_service

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
    if not course_service.visible_to(course, user):
        # The showcase course and other athletes' submissions answer the same
        # way a missing slug does — see `course_service.visible_to`.
        raise NotFound(f"No course {payload.course_ref!r}.")
    if course.availability is not CourseAvailability.AVAILABLE:
        raise Conflict(
            f"{course.name} is on the calendar for "
            f"{course.next_edition_date.strftime('%-d %B %Y')} but its course "
            f"data is not published yet, so it cannot be planned for. You can "
            f"add the course yourself if you have the route files."
            if course.next_edition_date
            else (
                f"{course.name} has no published course data yet, so it cannot " f"be planned for."
            )
        )
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


@router.get("/{race_id}/forecast", summary="The live forecast for this race's start hour")
def get_race_forecast(
    race_id: UUID, session: DbSession, user: CurrentUser, settings: Config
) -> ForecastOut:
    """The forecast as it stands **now**, beside the one the plan was solved on.

    A solved plan freezes its ``forecast_snapshot`` and keeps it frozen — Law 3
    says a plan's numbers do not change under the athlete. That is the right
    behaviour and it leaves a gap: nothing showed what the weather is actually
    doing, so an athlete could not see that the plan they are holding was
    solved against a forecast that has since moved eight degrees. This closes
    it without touching the plan.

    Never 4xx for an absent forecast. "No forecast" is an ordinary state with
    three ordinary causes, each of which wants different words on the screen,
    and a 404 here would say the race does not exist.

    The response commits because ``fetch_forecast`` writes the provider's reply
    into the TTL cache. That is a read-through cache doing its job on a GET,
    not a mutation: dropping the write would re-fetch on every render.
    """
    race = _owned(session, race_id, user)
    days_away = (race.event_date - datetime.now(UTC).date()).days
    base = ForecastOut(
        available=False,
        days_away=days_away,
        horizon_hours=settings.weather_forecast_horizon_hours,
    )

    course = session.get(Course, race.course_id)
    if course is None:  # pragma: no cover - FK RESTRICT
        return base.model_copy(update={"unavailable_reason": "course_unlocatable"})

    snapshot = weather_service.fetch_for_race(session, race=race, settings=settings)
    if snapshot is None:
        session.commit()
        reason = (
            "beyond_horizon"
            if days_away * 24 > settings.weather_forecast_horizon_hours
            else "provider_unavailable"
        )
        return base.model_copy(update={"unavailable_reason": reason})

    session.commit()
    return ForecastOut(
        available=True,
        days_away=days_away,
        horizon_hours=settings.weather_forecast_horizon_hours,
        for_local_time=(f"{race.event_date.isoformat()} {race.start_time_local.strftime('%H:%M')}"),
        **snapshot,
    )


# ---------------------------------------------------------------------------
# Race week
# ---------------------------------------------------------------------------


def _task_out(task: RaceWeekTask, *, today: date) -> RaceWeekTaskOut:
    out = RaceWeekTaskOut.model_validate(task)
    out.days_away = (task.due_date - today).days
    return out


@router.get("/{race_id}/race-week", summary="The race-week checklist")
def get_race_week(race_id: UUID, session: DbSession, user: CurrentUser) -> RaceWeekOut:
    """Dated, checkable tasks. **Generated on read, idempotently.**

    The ICS export has always derived the right dates from the event date — a
    Tuesday race gets a Saturday check-in — but a calendar event cannot be
    ticked off and nothing was stored, so "have I handed in the special-needs
    bag?" had no answer. Both now come from one derivation, so the calendar
    and the checklist cannot disagree about the same week.

    Generating here rather than in a job means a race entered five minutes ago
    has a checklist, instead of waiting for a cron that may not run before race
    week.
    """
    today = datetime.now(UTC).date()
    race, tasks, visible = race_week_service.for_race(
        session, race_id=race_id, user=user, today=today
    )
    session.commit()
    return RaceWeekOut(
        race_id=race.id,
        event_date=race.event_date,
        visible=visible,
        tasks=[_task_out(task, today=today) for task in tasks],
        remaining=sum(1 for task in tasks if task.completed_at is None),
    )


@router.post(
    "/{race_id}/race-week/tasks",
    status_code=status.HTTP_201_CREATED,
    summary="Add your own race-week task",
)
def add_race_week_task(
    race_id: UUID, payload: RaceWeekTaskCreate, session: DbSession, user: CurrentUser
) -> RaceWeekTaskOut:
    """Sits beside the derived ones, in date order. To the athlete it is one
    list, so it is one list."""
    task = race_week_service.add_task(
        session,
        race_id=race_id,
        user=user,
        title=payload.title,
        due_date=payload.due_date,
        description=payload.description,
    )
    session.commit()
    return _task_out(task, today=datetime.now(UTC).date())


@router.patch("/race-week/tasks/{task_id}", summary="Tick a task off, or un-tick it")
def set_race_week_task(
    task_id: UUID, payload: RaceWeekTaskPatch, session: DbSession, user: CurrentUser
) -> RaceWeekTaskOut:
    task = race_week_service.set_completed(
        session, task_id=task_id, user=user, completed=payload.completed
    )
    session.commit()
    return _task_out(task, today=datetime.now(UTC).date())


@router.delete(
    "/race-week/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Remove a task you added",
)
def delete_race_week_task(task_id: UUID, session: DbSession, user: CurrentUser) -> None:
    """Yours only. A derived task is part of the race rather than a note, and
    removing "bike check-in" because it is inconvenient is not something the
    checklist should help with — ticking it off is."""
    race_week_service.delete_task(session, task_id=task_id, user=user)
    session.commit()


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
