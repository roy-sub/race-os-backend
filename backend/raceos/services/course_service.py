"""Reading courses and their bundles.

Read-only in this milestone. The publish workflow, blast-radius preview and
the Thursday-to-Sunday freeze arrive with milestone 9; nothing here writes.

The one piece of derivation is :func:`cutoff_summary`, which turns a bundle's
real barriers into the single cut-off the directory card shows. The frontend
mock carries `cutoff: "10:30 bike"` as a string; that string is rendered from
these minutes rather than stored, so a bundle whose cut-off moves cannot leave
a stale label behind.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.orm import Session, selectinload

from raceos.api.errors import InvalidInput, NotFound
from raceos.api.schemas.course import (
    PROVENANCE_DISPLAY,
    BundleDetail,
    BundleHistoryEntry,
    BundleSummary,
    CourseDetail,
    CourseSummary,
    LegSummary,
)
from raceos.config import Settings
from raceos.db.models import Course, CourseBundle, User
from raceos.domain.enums import (
    BundleStatus,
    CourseAvailability,
    CourseVisibility,
    DistanceType,
)

#: Barrier names that represent the headline cut-off, most significant first.
#: A course without a bike cut-off (Olympic, Sprint) falls through to the
#: finish, which is the only limit those distances have.
CUTOFF_PREFERENCE: tuple[str, ...] = ("bike_cutoff", "finish")


def cutoff_summary(barriers: list[dict[str, Any]]) -> tuple[float | None, str | None]:
    """The headline cut-off: its minutes from start, and its name."""
    by_name = {b.get("name"): b for b in barriers if isinstance(b, dict)}
    for name in CUTOFF_PREFERENCE:
        barrier = by_name.get(name)
        if barrier and barrier.get("limit_minutes_from_start") is not None:
            return float(barrier["limit_minutes_from_start"]), name
    return None, None


def _active_bundle(session: Session, course_id: UUID) -> CourseBundle | None:
    """The bundle a client should read.

    Prefers `published`; falls back to the newest `draft`. The fallback is
    what makes the seeded courses visible before an admin has published
    anything — they arrive from the pipeline as drafts, honestly, and hiding
    them would leave the directory empty for no good reason.
    """
    published = session.scalar(
        select(CourseBundle)
        .where(
            CourseBundle.course_id == course_id,
            CourseBundle.status == BundleStatus.PUBLISHED,
        )
        .order_by(CourseBundle.published_at.desc().nullslast(), CourseBundle.version.desc())
        .limit(1)
    )
    if published is not None:
        return published
    return session.scalar(
        select(CourseBundle)
        .where(CourseBundle.course_id == course_id, CourseBundle.status == BundleStatus.DRAFT)
        .order_by(CourseBundle.version.desc())
        .limit(1)
    )


#: What a signed-out visitor is told when they open a catalogue course.
SIGNED_OUT_MAP_REASON = (
    "Course maps, elevation and cut-off detail are part of a race plan. "
    "Create an account to open them."
)
#: What a signed-in athlete without an entitlement is told.
LOCKED_MAP_REASON = (
    "This race's surveyed map, elevation profile and cut-off ladder open with " "the race plan."
)


def is_showcase(course: Course) -> bool:
    """The one course the signed-out marketing pages draw their map from."""
    return course.visibility is CourseVisibility.SHOWCASE


def visible_to(course: Course, viewer: User | None) -> bool:
    """Whether *viewer* should be shown *course* at all.

    Three rules, and each exists for a reason that is about the product rather
    than about security — nothing here is a secret:

    * A retired course is listed to nobody. It survives only so a plan solved
      against it still has a name.
    * The showcase is listed to signed-out visitors only. Once someone has an
      account, a stylised illustration sitting next to surveyed courses would
      invite the two to be compared as though they were the same kind of
      thing.
    * A course an athlete submitted themselves belongs to that athlete.
    """
    if course.visibility is CourseVisibility.RETIRED:
        return False
    if course.visibility is CourseVisibility.SHOWCASE:
        return viewer is None
    if course.submitted_by_user_id is not None:
        return viewer is not None and course.submitted_by_user_id == viewer.id
    return True


def map_access(
    session: Session,
    course: Course,
    viewer: User | None,
    settings: Settings,
) -> tuple[bool, str | None]:
    """Whether *viewer* may see this course's geometry, and why not if not.

    The showcase is open to everyone: it is the marketing map, and a locked
    marketing map advertises nothing. Everything else is part of the race plan
    an athlete buys, which is what makes the directory a free evaluation of
    *which* race rather than a free copy of the product.
    """
    if is_showcase(course):
        return True, None
    if viewer is None:
        return False, SIGNED_OUT_MAP_REASON
    if course.submitted_by_user_id == viewer.id:
        # An athlete who supplied the course file is not paywalled out of it.
        return True, None

    from raceos.db.models import Race
    from raceos.domain.entitlements import EntitlementAction
    from raceos.services import billing_service

    # Scoped to this athlete's own entry for this course, when they have one:
    # a per-race purchase is what unlocks the map for the race they bought,
    # and an unscoped check could only ever answer for a subscription.
    race = session.scalar(
        select(Race)
        .where(Race.user_id == viewer.id, Race.course_id == course.id)
        .order_by(Race.event_date.desc())
        .limit(1)
    )
    decision = billing_service.check(
        session,
        user=viewer,
        action=EntitlementAction.COURSE_MAP,
        race_id=race.id if race is not None else None,
        settings=settings,
    )
    if decision.allowed:
        return True, None
    return False, decision.reason or LOCKED_MAP_REASON


def _summarise(
    session: Session,
    course: Course,
    viewer: User | None = None,
    settings: Settings | None = None,
) -> CourseSummary:
    summary = CourseSummary.model_validate(course)
    summary.is_user_submitted = course.submitted_by_user_id is not None
    summary.illustrative_map = is_showcase(course)
    if settings is not None:
        unlocked, reason = map_access(session, course, viewer, settings)
        summary.map_unlocked = unlocked
        summary.map_locked_reason = reason
    bundle = _active_bundle(session, course.id)
    if bundle is not None:
        summary.provenance = PROVENANCE_DISPLAY[bundle.provenance]
        summary.bundle_version = bundle.version
        minutes, name = cutoff_summary(bundle.barriers)
        summary.cutoff_minutes = minutes
        summary.cutoff_barrier_name = name
    else:
        # An announced course has no surveyed climb, and the column's zero is
        # the absence of a measurement rather than a flat course. Sending it as
        # a number made the directory print "0 m" against fourteen races whose
        # maps have not been built — a measurement we have never taken, stated
        # as fact. `None` is what we actually know.
        summary.elevation_gain_m = None
    return summary


def _visible_filter(viewer: User | None) -> Any:
    """The SQL half of :func:`visible_to`, so paging counts agree with it."""
    from sqlalchemy import and_, or_

    clauses = [Course.visibility != CourseVisibility.RETIRED]
    if viewer is None:
        clauses.append(Course.submitted_by_user_id.is_(None))
    else:
        clauses.append(Course.visibility != CourseVisibility.SHOWCASE)
        clauses.append(
            or_(
                Course.submitted_by_user_id.is_(None),
                Course.submitted_by_user_id == viewer.id,
            )
        )
    return and_(*clauses)


#: Orders the directory by what a visitor can do with a row, before date.
#:
#: The showcase leads because it is the only map a signed-out visitor may open,
#: and it is absent entirely once they sign in — so for an athlete the list
#: opens on the races they can actually enter. Announced-but-unbuilt rows come
#: last: they belong in the directory (hiding fourteen fifteenths of a season
#: misrepresents the season) but not above the rows that work.
_DIRECTORY_BAND = case(
    (Course.visibility == CourseVisibility.SHOWCASE, 0),
    (Course.availability == CourseAvailability.AVAILABLE, 1),
    else_=2,
)


def list_courses(
    session: Session,
    *,
    distance_type: DistanceType | None = None,
    query: str | None = None,
    limit: int = 25,
    offset: int = 0,
    viewer: User | None = None,
    settings: Settings | None = None,
) -> tuple[list[CourseSummary], int]:
    """The race directory.

    Public, and richer when signed in — but *narrower*, not wider: a signed-in
    athlete sees the real season and not the marketing showcase.

    Ordered by **what a visitor can act on**, then by date:

    1. the showcase, which is the one map a signed-out visitor may open and so
       is the page's own best argument for itself;
    2. everything raceable today, because a directory whose first rows cannot
       be entered reads as a directory of dead ends;
    3. the announced season, by date — the rows that answer "which race next?"

    Within each band the announced date orders the rows, because a directory of
    dated events is read to answer "which race next?" and alphabetical order
    answers nothing.
    """
    visible = _visible_filter(viewer)
    statement = select(Course).where(visible)
    count_statement = select(Course).where(visible)

    if distance_type is not None:
        statement = statement.where(Course.distance_type == distance_type)
        count_statement = count_statement.where(Course.distance_type == distance_type)
    if query:
        pattern = f"%{query.strip()}%"
        statement = statement.where(Course.name.ilike(pattern) | Course.place.ilike(pattern))
        count_statement = count_statement.where(
            Course.name.ilike(pattern) | Course.place.ilike(pattern)
        )

    total = len(session.scalars(count_statement).all())
    courses = session.scalars(
        statement.order_by(
            _DIRECTORY_BAND,
            Course.next_edition_date.asc().nullslast(),
            Course.name,
        )
        .limit(limit)
        .offset(offset)
    ).all()
    return [_summarise(session, course, viewer, settings) for course in courses], total


def _load_course(session: Session, course_ref: str) -> Course:
    """Resolve by uuid or by slug, so a URL can carry either."""
    try:
        course = session.get(Course, UUID(course_ref))
    except ValueError:
        course = session.scalar(select(Course).where(Course.slug == course_ref))
    if course is None:
        raise NotFound(f"No course {course_ref!r}.")
    return course


def load_visible_course(session: Session, course_ref: str, viewer: User | None) -> Course:
    """Resolve a course, honouring the directory's own visibility rules.

    The 404 for "not visible to you" is the same as for "does not exist",
    deliberately: nothing here is secret, and two different answers would still
    let a signed-out visitor enumerate which courses exist.
    """
    course = _load_course(session, course_ref)
    if not visible_to(course, viewer):
        raise NotFound(f"No course {course_ref!r}.")
    return course


def _bundle_summary(bundle: CourseBundle) -> BundleSummary:
    summary = BundleSummary.model_validate(bundle)
    summary.provenance = PROVENANCE_DISPLAY[bundle.provenance]
    return summary


def get_course(
    session: Session,
    course_ref: str,
    viewer: User | None = None,
    settings: Settings | None = None,
) -> CourseDetail:
    course = _load_course(session, course_ref)
    if not visible_to(course, viewer):
        # Deliberately the same answer as a slug that does not exist. There is
        # nothing secret here, but a directory that says "this exists, you may
        # not see it" reads as a bug to the athlete it happens to.
        raise NotFound(f"No course {course_ref!r}.")
    detail = CourseDetail.model_validate(_summarise(session, course, viewer, settings).model_dump())
    bundle = _active_bundle(session, course.id)
    if bundle is not None:
        detail.active_bundle = _bundle_summary(bundle)
        detail.legs = [LegSummary.model_validate(leg) for leg in _ordered_legs(bundle)]
    return detail


def _ordered_legs(bundle: CourseBundle) -> list[Any]:
    """Legs in the fixed order the solver accumulates them: SWIM, BIKE, RUN."""
    from raceos.domain.enums import LEG_ORDER

    position = {leg: index for index, leg in enumerate(LEG_ORDER)}
    return sorted(bundle.legs, key=lambda leg: position[leg.leg])


def get_active_bundle(session: Session, course_ref: str) -> BundleDetail:
    course = _load_course(session, course_ref)
    active = _active_bundle(session, course.id)
    if active is None:
        raise NotFound(f"Course {course.slug!r} has no bundle yet.")
    bundle = session.scalar(
        select(CourseBundle)
        .options(selectinload(CourseBundle.legs))
        .where(CourseBundle.id == active.id)
    )
    if bundle is None:  # pragma: no cover - the row was just read
        raise NotFound(f"Course {course.slug!r} has no bundle yet.")

    detail = BundleDetail.model_validate(
        {
            **_bundle_summary(bundle).model_dump(),
            "course_id": bundle.course_id,
            "legs": [LegSummary.model_validate(leg) for leg in _ordered_legs(bundle)],
            "barriers": bundle.barriers,
            "aid_stations": bundle.aid_stations,
            "waypoints": bundle.waypoints,
            "segments": bundle.segments,
            "elevation_profile": bundle.elevation_profile,
            "bundle_asset_key": bundle.bundle_asset_key,
            "terrain_pmtiles_key": bundle.terrain_pmtiles_key,
            "provenance_detail": bundle.provenance_detail,
        }
    )
    return detail


def get_bundle_history(session: Session, course_ref: str) -> list[BundleHistoryEntry]:
    course = _load_course(session, course_ref)
    bundles = session.scalars(
        select(CourseBundle)
        .where(CourseBundle.course_id == course.id)
        .order_by(CourseBundle.version.desc())
    ).all()
    entries: list[BundleHistoryEntry] = []
    for bundle in bundles:
        entry = BundleHistoryEntry.model_validate(bundle)
        entry.provenance = PROVENANCE_DISPLAY[bundle.provenance]
        entries.append(entry)
    return entries


#: Points per leg handed to a map. Enough that a hairpin still reads as a
#: hairpin at full zoom, few enough that the payload stays small.
MAP_MAX_POINTS = 600


def _leg_coordinates(leg: Any) -> list[list[float]]:
    """``[[lng, lat, elevation], ...]`` from the stored PostGIS geometry.

    GeoJSON order — longitude first. Getting this backwards puts Mallorca in
    Somalia, and it is the single most common mistake with coordinate pairs.
    """
    from geoalchemy2.shape import to_shape

    shape = to_shape(leg.geometry)
    return [
        [round(float(c[0]), 6), round(float(c[1]), 6), round(float(c[2]), 1) if len(c) > 2 else 0.0]
        for c in shape.coords
    ]


def _downsample(points: list[list[float]], limit: int) -> list[list[float]]:
    """Keep every nth point, always keeping the first and last.

    Dropping the last point would leave a route that stops short of the
    finish, which looks like a data error rather than a rendering choice.
    """
    if len(points) <= limit:
        return points
    step = len(points) / limit
    kept = [points[int(i * step)] for i in range(limit)]
    if kept[-1] != points[-1]:
        kept.append(points[-1])
    return kept


def course_recon(
    session: Session,
    course_ref: str,
    settings: Settings,
    viewer: User | None = None,
) -> dict[str, Any]:
    """Everything the recon page shows for one course.

    **Two tiers, and the split is deliberate.** The part that helps someone
    choose a race — where it is, how far, how much climbing, what the tightest
    cut-off is, and the cut-off calculator that runs off it — stays free for
    everyone, signed out included. That is the evaluation the directory
    exists to support.

    The surveyed map itself does not: leg geometry, the elevation series, the
    named segments, the aid stations and the barrier ladder are the course
    work an athlete buys, and they are withheld behind ``map_unlocked`` with a
    reason attached rather than silently omitted.

    The showcase course is the exception in both directions: its map is open
    to everyone, because a marketing map nobody can see advertises nothing,
    and it is flagged ``illustrative`` so it can never be mistaken for a
    surveyed one.
    """
    course = _load_course(session, course_ref)
    if not visible_to(course, viewer):
        raise NotFound(f"No course {course_ref!r}.")
    bundle = _active_bundle(session, course.id)
    if bundle is None:
        raise NotFound(
            f"{course.name} has no course data yet. It is on the calendar and "
            f"its map is being built."
        )
    unlocked, locked_reason = map_access(session, course, viewer, settings)

    legs = _ordered_legs(bundle)
    barriers = list(bundle.barriers or [])
    cutoff_minutes, cutoff_name = cutoff_summary(barriers)

    total_distance_m = sum(float(leg.distance_m) for leg in legs)
    total_gain_m = sum(float(leg.elevation_gain_m) for leg in legs)

    return {
        "course": {
            "id": str(course.id),
            "slug": course.slug,
            "name": course.name,
            "place": course.place,
            "distance_type": course.distance_type.value,
            "difficulty": course.difficulty.value,
            "timezone": course.timezone,
            "lat": float(course.lat),
            "lng": float(course.lng),
            "is_fictional": course.is_fictional,
            "availability": course.availability.value,
            "next_edition_date": (
                course.next_edition_date.isoformat() if course.next_edition_date else None
            ),
            "is_user_submitted": course.submitted_by_user_id is not None,
        },
        "access": {
            "map_unlocked": unlocked,
            "map_locked_reason": locked_reason,
            # The showcase map is a tuned graphic whose three legs disagree on
            # scale by 14.5x. Saying so next to it is the difference between a
            # stylisation and a false claim.
            "illustrative_map": is_showcase(course),
            # Honest and appealing at once: it has to say "this one is a
            # drawing" without reading as an apology for the page it sits on.
            # The two facts are the same as before, reordered so the surveyed
            # maps are the point rather than the caveat.
            "illustrative_note": (
                "A taste of it — hand-drawn, and not to scale. Your race gets "
                "the real thing: every metre surveyed from the course itself."
                if is_showcase(course)
                else None
            ),
        },
        "bundle": {
            "version": bundle.version,
            "provenance": bundle.provenance.value,
            "verified_at": bundle.verified_at.isoformat() if bundle.verified_at else None,
            "elevation_source": bundle.elevation_source,
            # ODbL obliges attribution wherever the derived data is displayed,
            # so it ships with the geometry rather than beside it.
            "attribution": bundle.attribution,
        },
        "legs": [
            {
                "leg": leg.leg.value,
                "distance_m": float(leg.distance_m),
                "elevation_gain_m": float(leg.elevation_gain_m),
                "node_count": leg.node_count,
                "surface_quality": leg.surface_quality.value,
                # The actual route, so a map can draw the real course rather
                # than a placeholder. Downsampled: a browser drawing a
                # polyline gains nothing from 10 m spacing, and the full
                # series is a megabyte per leg. Empty when the map is locked —
                # the leg's distance and climb still ship, because those are
                # the facts someone chooses a race on.
                "coordinates": (
                    _downsample(_leg_coordinates(leg), MAP_MAX_POINTS) if unlocked else []
                ),
            }
            for leg in legs
        ],
        "totals": {
            "distance_m": total_distance_m,
            "elevation_gain_m": total_gain_m,
            "final_cutoff_minutes": cutoff_minutes,
            "final_cutoff_name": cutoff_name,
        },
        # The headline cut-off travels in `totals` either way, so the free
        # cut-off calculator still works on a locked course.
        "barriers": barriers if unlocked else [],
        "aid_stations": list(bundle.aid_stations or []) if unlocked else [],
        "waypoints": list(bundle.waypoints or []) if unlocked else [],
        "segments": list(bundle.segments or []) if unlocked else [],
        "elevation_profile": (bundle.elevation_profile or {}) if unlocked else {},
        "terrain_pmtiles_key": bundle.terrain_pmtiles_key if unlocked else None,
    }


def cutoff_feasibility(
    *, barriers: list[dict[str, Any]], projected_minutes: float
) -> list[dict[str, Any]]:
    """The free cut-off calculator: a projected time against every barrier.

    Pure arithmetic over the *published* limits — not a solve. It answers "if
    I finish in this time, which cut-offs am I near?", which is the question
    someone deciding whether to enter is actually asking, and it needs no
    account and no athlete data.

    The share of the finish each barrier sits at is taken from the barriers
    themselves, so a course whose bike cut-off is unusually early reports that
    honestly rather than against a generic assumption.
    """
    if projected_minutes <= 0:
        raise InvalidInput(
            "Enter a projected finish time greater than zero.",
            field="projected_minutes",
        )
    ordered = sorted(
        (b for b in barriers if isinstance(b.get("limit_minutes_from_start"), int | float)),
        key=lambda b: float(b["limit_minutes_from_start"]),
    )
    if not ordered:
        return []

    final_limit = float(ordered[-1]["limit_minutes_from_start"])
    rows: list[dict[str, Any]] = []
    for barrier in ordered:
        limit = float(barrier["limit_minutes_from_start"])
        # Where this barrier falls in the race, as a fraction of the overall
        # time limit — then applied to the athlete's own projected pace.
        share = limit / final_limit if final_limit > 0 else 0.0
        eta = projected_minutes * share
        margin = limit - eta
        rows.append(
            {
                "name": barrier.get("name"),
                "leg": barrier.get("leg"),
                "limit_minutes": limit,
                "estimated_eta_minutes": round(eta, 1),
                "margin_minutes": round(margin, 1),
                "at_risk": margin < 20.0,
                "basis": (
                    "A straight-line estimate from your finish time, not a "
                    "solve. A solved plan accounts for terrain, heat and "
                    "fuelling."
                ),
            }
        )
    return rows
