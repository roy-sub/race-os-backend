"""The season view, and how an athlete's constraints moved across it.

Season Pass promises two things nothing served: a season at a glance, and
constraint drift *over time*. Constraint history existed but only per key
(``GET /constraints/{key}/history``), which answers "how has my FTP moved?" and
cannot answer "what changed about me last year?" — the question the tier is
sold on.

**Grouped by season, not by calendar year.** A race in January belongs to the
season it was trained for, and for most athletes that is the one that started
the previous autumn. The split point is configurable rather than assumed; see
:data:`SEASON_START_MONTH`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.db.models import (
    AnalysisCompareRow,
    Constraint,
    ConstraintHistory,
    Course,
    Plan,
    PostRaceAnalysis,
    Race,
    User,
)
from raceos.domain.enums import PlanStatus, RaceStatus

#: A season runs from this month to the month before it, so a January race
#: sits with the autumn that prepared it rather than opening a season of its
#: own. October is the conventional break in long-course racing: the
#: northern-hemisphere season has ended and the next year's entries open.
SEASON_START_MONTH = 10


def season_of(day: date) -> int:
    """The season a date belongs to, named by the year it starts in."""
    return day.year if day.month >= SEASON_START_MONTH else day.year - 1


def season_label(season: int) -> str:
    """``2025`` -> ``"2025/26"``. A season spans two calendar years."""
    return f"{season}/{(season + 1) % 100:02d}"


@dataclass(frozen=True)
class SeasonRace:
    """One race in the season, with what was planned and what happened."""

    race_id: UUID
    course_name: str
    course_place: str
    course_slug: str
    distance_type: str
    event_date: date
    race_status: str
    plan_id: UUID | None
    plan_version: int | None
    goal_minutes: float | None
    projected_minutes: float | None
    #: From the post-race analysis of the version that was live at race time,
    #: when there is one. Absent is the ordinary case: most races have not been
    #: analysed, and a season view that invented a finish time for them would
    #: be worse than one that says nothing.
    actual_minutes: float | None
    has_analysis: bool


@dataclass(frozen=True)
class ConstraintPoint:
    """One value, at one moment, with where it came from."""

    key: str
    value: float
    unit: str
    source: str
    at: datetime
    change_reason: str | None


@dataclass(frozen=True)
class ConstraintTrack:
    """One constraint's whole series, oldest first.

    Returned as a series rather than as a start-and-end pair because the shape
    is the point: a value that rose, fell back and rose again is a different
    story from one that climbed steadily to the same place.
    """

    key: str
    unit: str
    points: tuple[ConstraintPoint, ...]
    #: Signed change across the window. `None` when there is only one point,
    #: because a single reading has not moved — it has only been taken.
    change: float | None


@dataclass(frozen=True)
class SeasonSummary:
    season: int
    label: str
    races: tuple[SeasonRace, ...]
    planned_count: int
    raced_count: int


def _analysis_finish_minutes(session: Session, plan_id: UUID, version: int) -> float | None:
    """The finish time the analysis measured, if one was run.

    Read from the compare row the analysis wrote for the whole race rather
    than recomputed, so the season view and the post-race screen can never
    disagree about what the athlete's day cost them.
    """
    analysis = session.scalar(
        select(PostRaceAnalysis)
        .where(PostRaceAnalysis.plan_id == plan_id, PostRaceAnalysis.plan_version == version)
        .order_by(PostRaceAnalysis.generated_at.desc())
    )
    if analysis is None:
        return None
    row = session.scalar(
        select(AnalysisCompareRow)
        .where(AnalysisCompareRow.analysis_id == analysis.id)
        .order_by(AnalysisCompareRow.ordinal.desc())
    )
    if row is None:
        return None
    return _minutes_from_clock(row.actual)


def _minutes_from_clock(text: str) -> float | None:
    """``"10:41"`` or ``"10:41:30"`` to minutes. ``None`` for anything else.

    Compare rows store display strings — they are what the post-race screen
    prints — so reading one back means parsing it. Anything that does not
    parse returns ``None`` rather than a guess: a wrong finish time on a
    season view is worse than a missing one.
    """
    parts = text.strip().split(":")
    if not all(part.isdigit() for part in parts):
        return None
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 60 + int(parts[1]) + int(parts[2]) / 60
    return None


def seasons(session: Session, *, user: User, today: date | None = None) -> list[SeasonSummary]:
    """Every season this athlete has raced or entered, newest first."""
    day = today or datetime.now(UTC).date()
    rows = session.execute(
        select(Race, Course)
        .join(Course, Course.id == Race.course_id)
        .where(Race.user_id == user.id)
        .order_by(Race.event_date.desc())
    ).all()

    grouped: dict[int, list[SeasonRace]] = {}
    for race, course in rows:
        plan = session.scalar(
            select(Plan)
            .where(
                Plan.race_id == race.id,
                Plan.status.in_(
                    (
                        PlanStatus.ACTIVE,
                        PlanStatus.PAST,
                        PlanStatus.PENDING_ATHLETE_APPROVAL,
                        PlanStatus.DRAFT,
                    )
                ),
            )
            .order_by(Plan.version.desc())
            .limit(1)
        )
        actual = (
            _analysis_finish_minutes(session, plan.id, plan.version)
            if plan is not None and plan.solved_at is not None
            else None
        )
        grouped.setdefault(season_of(race.event_date), []).append(
            SeasonRace(
                race_id=race.id,
                course_name=course.name,
                course_place=course.place,
                course_slug=course.slug,
                distance_type=course.distance_type.value,
                event_date=race.event_date,
                race_status=race.status.value,
                plan_id=plan.id if plan else None,
                plan_version=plan.version if plan else None,
                goal_minutes=plan.goal_minutes if plan else None,
                projected_minutes=plan.projected_minutes if plan else None,
                actual_minutes=actual,
                has_analysis=actual is not None,
            )
        )

    out: list[SeasonSummary] = []
    for season in sorted(grouped, reverse=True):
        races = tuple(grouped[season])
        out.append(
            SeasonSummary(
                season=season,
                label=season_label(season),
                races=races,
                planned_count=sum(1 for r in races if r.plan_id is not None),
                raced_count=sum(
                    1
                    for r in races
                    if r.race_status == RaceStatus.COMPLETED.value or r.event_date < day
                ),
            )
        )
    return out


def constraint_tracks(
    session: Session, *, user: User, since: date | None = None
) -> list[ConstraintTrack]:
    """Every constraint's whole series, in one call.

    `GET /constraints/{key}/history` answers "how has my FTP moved?". This
    answers "what changed about me?", which is the one the season view is for
    and which eight separate requests could only assemble by hand.

    The athlete's *current* value is appended as the last point. History is an
    append-only mirror written when a value is superseded, so without it the
    series would stop at the second-to-last reading and a constraint set once
    and never changed would have no series at all.
    """
    history_statement = select(ConstraintHistory).where(ConstraintHistory.user_id == user.id)
    if since is not None:
        history_statement = history_statement.where(
            ConstraintHistory.created_at >= datetime.combine(since, datetime.min.time(), tzinfo=UTC)
        )

    points: dict[str, list[ConstraintPoint]] = {}
    units: dict[str, str] = {}
    for row in session.scalars(history_statement.order_by(ConstraintHistory.created_at)):
        units[row.key] = row.unit
        points.setdefault(row.key, []).append(
            ConstraintPoint(
                key=row.key,
                value=float(row.value),
                unit=row.unit,
                source=row.source.value,
                at=row.created_at,
                change_reason=row.change_reason,
            )
        )

    for current in session.scalars(select(Constraint).where(Constraint.user_id == user.id)):
        units.setdefault(current.key, current.unit)
        points.setdefault(current.key, []).append(
            ConstraintPoint(
                key=current.key,
                value=float(current.value),
                unit=current.unit,
                source=current.source.value,
                at=current.updated_at,
                change_reason="current value",
            )
        )

    tracks: list[ConstraintTrack] = []
    for key in sorted(points):
        series = tuple(points[key])
        tracks.append(
            ConstraintTrack(
                key=key,
                unit=units.get(key, ""),
                points=series,
                change=(series[-1].value - series[0].value if len(series) > 1 else None),
            )
        )
    return tracks
