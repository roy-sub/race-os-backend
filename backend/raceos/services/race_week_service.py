"""The race-week checklist: dated, checkable, and derived from one place.

The ICS export already knew the right dates — it derives them from the event
date rather than from weekday names, so a Tuesday race gets a Saturday
check-in. What it could not do is be ticked off, so "have I handed in the
special-needs bag?" had no answer anywhere.

**Generated and personal tasks are one list.** The four or five items every
race has come from :data:`~raceos.exports.files.RACE_WEEK_ITEMS`; anything the
athlete adds sits beside them, in date order, indistinguishable except for a
flag. To the athlete it is one checklist, and building it out of two would
mean two orderings and two ways to complete something.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.errors import Conflict, InvalidInput, NotFound
from raceos.db.models import Plan, Race, RaceWeekTask, User
from raceos.domain.enums import PlanStatus
from raceos.exports.files import race_week_items
from raceos.logging import get_logger

logger = get_logger(__name__)

#: How far out the checklist starts being worth showing. Beyond this the
#: answer to every item is "not yet", and a list of things nobody can do is a
#: list people stop opening.
VISIBLE_DAYS_BEFORE = 21

#: What an athlete's own task may be titled. Long enough for a real reminder,
#: short enough to read in a strip.
MAX_TITLE_LENGTH = 120


def _owned(session: Session, *, race_id: UUID, user: User) -> Race:
    race = session.get(Race, race_id)
    if race is None or race.user_id != user.id:
        raise NotFound("Race not found.")
    return race


def _has_special_needs(session: Session, race: Race) -> bool:
    """Whether the athlete's plan actually carries special-needs bags.

    Read from the solved plan rather than assumed from the distance: a
    deadline for something this race does not offer is a deadline to ignore,
    and one unactionable line is enough to make a checklist feel like
    decoration.
    """
    plan = session.scalar(
        select(Plan)
        .where(Plan.race_id == race.id, Plan.status == PlanStatus.ACTIVE)
        .order_by(Plan.version.desc())
    )
    if plan is None:
        return False
    from raceos.db.models import PlanBag

    return bool(
        session.scalar(
            select(PlanBag).where(
                PlanBag.plan_id == plan.id,
                PlanBag.key.in_(("bike_sn", "run_sn")),
            )
        )
    )


def ensure_generated(session: Session, *, race: Race, user: User) -> list[RaceWeekTask]:
    """Create the derived tasks for this race, or bring them up to date.

    Idempotent, and safe to call on every read — which is how the checklist
    exists at all without a job. ``(race_id, key)`` is unique, so a second call
    updates rather than duplicates.

    **A completed task is never un-completed by a rebuild.** Dates and titles
    are refreshed because a race can be re-dated and copy can be edited; the
    tick is the athlete's and the derivation has no business touching it.
    """
    existing = {
        row.key: row
        for row in session.scalars(select(RaceWeekTask).where(RaceWeekTask.race_id == race.id))
    }
    wanted = race_week_items(has_special_needs=_has_special_needs(session, race))

    for item in wanted:
        due = race.event_date - timedelta(days=item.days_before)
        row = existing.get(item.key)
        if row is None:
            session.add(
                RaceWeekTask(
                    race_id=race.id,
                    user_id=user.id,
                    key=item.key,
                    title=item.title,
                    description=item.description,
                    due_date=due,
                    generated=True,
                )
            )
        elif row.generated:
            row.title = item.title
            row.description = item.description
            row.due_date = due

    # An item that no longer applies — the athlete re-solved and the plan lost
    # its special-needs bags — is removed, but only if untouched. Deleting
    # something somebody has ticked off would look like the app forgetting.
    wanted_keys = {item.key for item in wanted}
    for key, row in existing.items():
        if row.generated and key not in wanted_keys and row.completed_at is None:
            session.delete(row)

    session.flush()
    return list_tasks(session, race=race)


def list_tasks(session: Session, *, race: Race) -> list[RaceWeekTask]:
    """Soonest first, then in a stable order within a day."""
    return list(
        session.scalars(
            select(RaceWeekTask)
            .where(RaceWeekTask.race_id == race.id)
            .order_by(RaceWeekTask.due_date, RaceWeekTask.generated.desc(), RaceWeekTask.key)
        )
    )


def is_visible(race: Race, *, today: date | None = None) -> bool:
    """Whether the strip is worth showing at all.

    Beyond three weeks out the answer to every item is "not yet", and a list
    of things nobody can act on is a list people stop opening. After race day
    it is history rather than a checklist.
    """
    day = today or datetime.now(UTC).date()
    days_away = (race.event_date - day).days
    return 0 <= days_away <= VISIBLE_DAYS_BEFORE


def for_race(
    session: Session, *, race_id: UUID, user: User, today: date | None = None
) -> tuple[Race, list[RaceWeekTask], bool]:
    """The race, its checklist, and whether the strip should be shown.

    Generation happens on read rather than in a job. It is idempotent, it is
    one indexed query plus at most five inserts, and doing it here means a
    race entered five minutes ago has a checklist rather than waiting for a
    cron that may not run before race week.
    """
    race = _owned(session, race_id=race_id, user=user)
    tasks = ensure_generated(session, race=race, user=user)
    return race, tasks, is_visible(race, today=today)


def add_task(
    session: Session,
    *,
    race_id: UUID,
    user: User,
    title: str,
    due_date: date,
    description: str | None = None,
) -> RaceWeekTask:
    """An athlete's own reminder, beside the derived ones."""
    race = _owned(session, race_id=race_id, user=user)
    cleaned = title.strip()
    if not cleaned:
        raise InvalidInput("Give the task a name.", field="title")
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise InvalidInput(f"Keep it under {MAX_TITLE_LENGTH} characters.", field="title")
    if due_date > race.event_date:
        raise InvalidInput(
            "Race week ends on race day. A task due after it would never be shown.",
            field="due_date",
        )

    task = RaceWeekTask(
        race_id=race.id,
        user_id=user.id,
        # Prefixed and random: an athlete's task must not collide with a
        # derived key, now or after a new derived item is added.
        key=f"own_{uuid.uuid4().hex[:12]}",
        title=cleaned,
        description=(description or "").strip() or None,
        due_date=due_date,
        generated=False,
    )
    session.add(task)
    session.flush()
    return task


def set_completed(session: Session, *, task_id: UUID, user: User, completed: bool) -> RaceWeekTask:
    task = session.get(RaceWeekTask, task_id)
    if task is None or task.user_id != user.id:
        raise NotFound("Task not found.")
    task.completed_at = datetime.now(UTC) if completed else None
    session.flush()
    return task


def delete_task(session: Session, *, task_id: UUID, user: User) -> None:
    """Only an athlete's own. A derived task is part of the race, not a note.

    Removing "bike check-in" because it is inconvenient is not a thing the
    checklist should help with; ticking it off is.
    """
    task = session.get(RaceWeekTask, task_id)
    if task is None or task.user_id != user.id:
        raise NotFound("Task not found.")
    if task.generated:
        raise Conflict(
            "This one comes from the race itself and cannot be removed. Tick it " "off instead."
        )
    session.delete(task)
    session.flush()
