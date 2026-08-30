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

from sqlalchemy import select
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
from raceos.db.models import Course, CourseBundle
from raceos.domain.enums import BundleStatus, DistanceType

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


def _summarise(session: Session, course: Course) -> CourseSummary:
    summary = CourseSummary.model_validate(course)
    bundle = _active_bundle(session, course.id)
    if bundle is not None:
        summary.provenance = PROVENANCE_DISPLAY[bundle.provenance]
        summary.bundle_version = bundle.version
        minutes, name = cutoff_summary(bundle.barriers)
        summary.cutoff_minutes = minutes
        summary.cutoff_barrier_name = name
    return summary


def list_courses(
    session: Session,
    *,
    distance_type: DistanceType | None = None,
    query: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[CourseSummary], int]:
    """The race directory. Public; no athlete data is involved."""
    statement = select(Course)
    count_statement = select(Course)

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
    courses = session.scalars(statement.order_by(Course.name).limit(limit).offset(offset)).all()
    return [_summarise(session, course) for course in courses], total


def _load_course(session: Session, course_ref: str) -> Course:
    """Resolve by uuid or by slug, so a URL can carry either."""
    try:
        course = session.get(Course, UUID(course_ref))
    except ValueError:
        course = session.scalar(select(Course).where(Course.slug == course_ref))
    if course is None:
        raise NotFound(f"No course {course_ref!r}.")
    return course


def _bundle_summary(bundle: CourseBundle) -> BundleSummary:
    summary = BundleSummary.model_validate(bundle)
    summary.provenance = PROVENANCE_DISPLAY[bundle.provenance]
    return summary


def get_course(session: Session, course_ref: str) -> CourseDetail:
    course = _load_course(session, course_ref)
    detail = CourseDetail.model_validate(_summarise(session, course).model_dump())
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


def course_recon(session: Session, course_ref: str, settings: Settings) -> dict[str, Any]:
    """Everything the free recon page shows for one course.

    Free for everyone, deliberately: the course library is the front door, and
    a cut-off calculator behind a paywall makes the product impossible to
    evaluate. **No athlete data is involved**, so this needs no session and
    leaks nothing — the numbers below describe the course, not anyone racing
    it.
    """
    course = _load_course(session, course_ref)
    bundle = _active_bundle(session, course.id)
    if bundle is None:
        raise NotFound(f"Course {course.slug!r} has no bundle yet.")

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
            }
            for leg in legs
        ],
        "totals": {
            "distance_m": total_distance_m,
            "elevation_gain_m": total_gain_m,
            "final_cutoff_minutes": cutoff_minutes,
            "final_cutoff_name": cutoff_name,
        },
        "barriers": barriers,
        "aid_stations": list(bundle.aid_stations or []),
        "waypoints": list(bundle.waypoints or []),
        "segments": list(bundle.segments or []),
        "elevation_profile": bundle.elevation_profile or {},
        "terrain_pmtiles_key": bundle.terrain_pmtiles_key,
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
