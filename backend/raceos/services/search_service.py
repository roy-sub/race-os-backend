"""One query across everything a person can navigate to.

The command palette asks one question — "what does this word mean here?" — and
until now the only answer available was ``GET /courses?q=``, which searches
courses alone.

**What this returns, and what it deliberately does not.** It searches the
things the server knows about: courses, the athlete's own races and plans, and
the help library. It does not return *navigation targets* — "open settings",
"start a plan" — even though the palette shows those beside these results.
Those are frontend routes, the frontend already owns the one list of them, and
a round trip to be told that a page it renders exists would be a round trip
for nothing.

**Every result is scoped to the asker.** Races and plans are filtered by owner
in SQL, not after the fact, and courses reuse the directory's own visibility
filter so a search cannot surface a row the directory would not list.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.help_content import search_articles
from raceos.config import Settings
from raceos.db.models import Course, Plan, Race, User
from raceos.domain.enums import PlanStatus
from raceos.services.course_service import _visible_filter

#: How many of each kind to return before the caller's overall limit applies.
#: A palette showing twenty races and no help article has answered the wrong
#: question, so each kind gets a share rather than the best matches overall.
PER_KIND_LIMIT = 5


@dataclass(frozen=True)
class SearchHit:
    """One result.

    ``ref`` is whatever the client needs to build a link: a course slug, a race
    or plan id. The URL itself is not built here — the frontend owns its route
    table, and a server that hardcoded ``/plan?plan=`` would be a second copy
    of it, silently wrong the day a path changes.
    """

    kind: str
    ref: str
    title: str
    subtitle: str
    #: Lower sorts first. Set from how direct the match was, so a course whose
    #: name is the query outranks one that merely happens to be in that place.
    rank: int = 1


def _rank_for(query: str, *fields: str | None) -> int:
    """0 for a prefix match, 1 for a match anywhere, 2 for no direct match."""
    needle = query.lower()
    for field in fields:
        if field and field.lower().startswith(needle):
            return 0
    for field in fields:
        if field and needle in field.lower():
            return 1
    return 2


def _courses(session: Session, query: str, viewer: User | None) -> list[SearchHit]:
    pattern = f"%{query}%"
    rows = session.scalars(
        select(Course)
        .where(_visible_filter(viewer))
        .where(Course.name.ilike(pattern) | Course.place.ilike(pattern))
        .limit(PER_KIND_LIMIT)
    )
    return [
        SearchHit(
            kind="course",
            ref=row.slug,
            title=row.name,
            subtitle=f"{row.place} · {row.distance_type.value}",
            rank=_rank_for(query, row.name, row.place),
        )
        for row in rows
    ]


def _races(session: Session, query: str, viewer: User) -> list[SearchHit]:
    """The athlete's own entries, matched on the course they are for.

    A race has no name of its own — it is this person, this course, this date —
    so the course's name and place are what there is to match on.
    """
    pattern = f"%{query}%"
    rows = session.execute(
        select(Race, Course)
        .join(Course, Course.id == Race.course_id)
        .where(Race.user_id == viewer.id)
        .where(Course.name.ilike(pattern) | Course.place.ilike(pattern))
        .order_by(Race.event_date)
        .limit(PER_KIND_LIMIT)
    )
    return [
        SearchHit(
            kind="race",
            ref=str(race.id),
            title=course.name,
            subtitle=f"Your race · {race.event_date.isoformat()}",
            rank=_rank_for(query, course.name, course.place),
        )
        for race, course in rows
    ]


def _plans(session: Session, query: str, viewer: User) -> list[SearchHit]:
    """Solved and draft plans, matched on their race's course.

    Superseded versions are excluded. Searching for a course and getting its
    four past plan versions above the live one is not what anybody meant.
    """
    pattern = f"%{query}%"
    rows = session.execute(
        select(Plan, Course)
        .join(Race, Race.id == Plan.race_id)
        .join(Course, Course.id == Race.course_id)
        .where(Plan.user_id == viewer.id)
        .where(
            Plan.status.in_(
                (PlanStatus.ACTIVE, PlanStatus.DRAFT, PlanStatus.PENDING_ATHLETE_APPROVAL)
            )
        )
        .where(Course.name.ilike(pattern) | Course.place.ilike(pattern))
        .order_by(Plan.version.desc())
        .limit(PER_KIND_LIMIT)
    )
    return [
        SearchHit(
            kind="plan",
            ref=str(plan.id),
            title=course.name,
            subtitle=(
                f"Your plan · {plan.status.value.replace('_', ' ')}"
                f"{'' if plan.solved_at is None else f' · v{plan.version}'}"
            ),
            rank=_rank_for(query, course.name, course.place),
        )
        for plan, course in rows
    ]


def _help(query: str) -> list[SearchHit]:
    return [
        SearchHit(
            kind="help",
            ref=article.slug,
            title=article.title,
            subtitle=article.category,
            rank=_rank_for(query, article.title, article.summary),
        )
        for article in search_articles(query, limit=PER_KIND_LIMIT)
    ]


def search(
    session: Session,
    *,
    query: str,
    viewer: User | None,
    limit: int = 20,
    settings: Settings | None = None,
) -> list[SearchHit]:
    """Everything matching *query* that *viewer* is allowed to see."""
    needle = query.strip()
    if len(needle) < 2:
        # One character matches most of the library and is almost always a
        # keystroke on the way to a real query.
        return []

    hits: list[SearchHit] = [*_courses(session, needle, viewer), *_help(needle)]
    if viewer is not None:
        hits.extend(_races(session, needle, viewer))
        hits.extend(_plans(session, needle, viewer))

    # Rank first, then by kind, so a palette's ordering is stable between
    # keystrokes that did not change the ranking.
    kind_order = {"plan": 0, "race": 1, "course": 2, "help": 3}
    hits.sort(key=lambda hit: (hit.rank, kind_order.get(hit.kind, 9), hit.title.lower()))
    return hits[:limit]


__all__ = ["PER_KIND_LIMIT", "SearchHit", "search"]
