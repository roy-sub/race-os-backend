"""Assembling exports from a solved plan.

Everything here is derived, nothing is stored: an export is regenerated from
the plan on every request. That is deliberate. A cached PDF is a copy of a
plan that can fall out of date silently, and Law 3 says a stale artefact must
never masquerade as a current one. Regenerating costs a few hundred
milliseconds and removes the entire class of problem.

The route geometry, waypoint positions and elevations all come from the same
course bundle the solver read, so a `.fit` file and the plan it accompanies
cannot disagree about where the course goes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Any
from uuid import UUID

from geoalchemy2.shape import to_shape
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from raceos.api.errors import Conflict, NotFound
from raceos.api.schemas.plan import PlanDetail, format_hm
from raceos.db.models import Course, CourseBundle, CourseBundleLeg, Plan, Race, User
from raceos.domain.enums import Leg, PlanStatus
from raceos.exports.files import CalendarEvent, RoutePoint, Waypoint, race_week_events
from raceos.exports.pdf import PlanRenderData

#: A course file is per-leg. A head unit follows one route; concatenating a
#: swim, a bike and a run into a single course would produce a track that
#: teleports at every transition.
EXPORTABLE_LEGS: tuple[Leg, ...] = (Leg.BIKE, Leg.RUN)


@dataclass(frozen=True)
class ExportContext:
    """The plan plus everything around it an export needs."""

    plan: Plan
    race: Race
    course: Course
    bundle: CourseBundle
    athlete: User


def load_context(session: Session, *, plan: Plan) -> ExportContext:
    """Resolve the race, course and **the bundle this plan was solved against**.

    Not the course's currently active bundle: a plan exported after a bundle
    republish must still describe the course the athlete's numbers came from.
    Law 3 again — the drift path is what tells them the course moved.
    """
    race = session.get(Race, plan.race_id)
    if race is None:  # pragma: no cover - FK RESTRICT makes this unreachable
        raise NotFound("The race for this plan no longer exists.")
    bundle = session.scalar(
        select(CourseBundle)
        .options(selectinload(CourseBundle.legs))
        .where(CourseBundle.id == race.course_bundle_id)
    )
    course = session.get(Course, race.course_id)
    athlete = session.get(User, plan.user_id)
    if bundle is None or course is None or athlete is None:  # pragma: no cover
        raise NotFound("The course for this plan no longer exists.")
    return ExportContext(plan=plan, race=race, course=course, bundle=bundle, athlete=athlete)


def require_exportable(plan: Plan) -> None:
    """A draft has no numbers, so there is nothing to export.

    A 409 rather than a 404: the plan exists and the athlete may read it — it
    is the plan's *state* that makes the request wrong, and telling them so
    lets the UI say "solve first" instead of "not found".
    """
    if plan.status is PlanStatus.DRAFT or plan.solved_at is None:
        raise Conflict(
            "This plan has not been solved yet, so there is nothing to export. " "Solve it first."
        )


# ---------------------------------------------------------------------------
# Print artefacts
# ---------------------------------------------------------------------------


def build_render_data(context: ExportContext, detail: PlanDetail) -> PlanRenderData:
    """Take the numbers from the serialised plan, not from the ORM rows.

    The printed card is then the same object the app displays, formatted
    once. A second formatting path would be a second chance to be wrong.
    """
    plan, race, course, bundle = context.plan, context.race, context.course, context.bundle
    fuelling = detail.fuelling.model_dump(mode="json") if detail.fuelling else {}
    return PlanRenderData(
        athlete_name=context.athlete.name or context.athlete.email,
        course_name=course.name,
        course_place=course.place,
        event_date=race.event_date.isoformat(),
        start_time=race.start_time_local.strftime("%H:%M"),
        bundle_version=bundle.version,
        bundle_provenance=bundle.provenance.value,
        attribution=bundle.attribution,
        projected_label=detail.projected_label or format_hm(plan.projected_minutes) or "—",
        feasibility=plan.feasibility.value,
        splits=[split.model_dump(mode="json") for split in detail.splits],
        gates=[gate.model_dump(mode="json") for gate in detail.gates],
        segments=[segment.model_dump(mode="json") for segment in detail.segments],
        fuelling=fuelling,
        aid_actions=[action.model_dump(mode="json") for action in detail.aid_actions],
        bags=[bag.model_dump(mode="json") for bag in detail.bags],
        constraint_refs=[ref.model_dump(mode="json") for ref in detail.constraint_refs],
        assumed_fields=list(plan.assumed_fields or []),
    )


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def _leg_row(bundle: CourseBundle, leg: Leg) -> CourseBundleLeg:
    for row in bundle.legs:
        if row.leg is leg:
            return row
    raise NotFound(f"This course bundle has no {leg.value.lower()} leg.")


def route_points(bundle: CourseBundle, leg: Leg) -> list[RoutePoint]:
    """Vertices with cumulative distance, straight from the bundle geometry.

    Distance is apportioned across the vertices and scaled to the leg's
    declared ``distance_m`` — the same treatment ``solver.adapters`` gives the
    elevation series, so a waypoint at km 42 lands at the same place in the
    exported file as it does in the solve.
    """
    row = _leg_row(bundle, leg)
    shape = to_shape(row.geometry)  # type: ignore[arg-type]
    coords = list(shape.coords)
    if len(coords) < 2:  # pragma: no cover - the loader rejects such a bundle
        raise NotFound(f"The {leg.value.lower()} leg has no usable geometry.")

    distance_m = float(row.distance_m)
    last = len(coords) - 1
    points: list[RoutePoint] = []
    for index, coordinate in enumerate(coords):
        lng, lat = float(coordinate[0]), float(coordinate[1])
        elevation = float(coordinate[2]) if len(coordinate) > 2 else 0.0
        points.append(
            RoutePoint(
                lat=lat,
                lng=lng,
                elevation_m=elevation,
                distance_m=distance_m * index / last,
            )
        )
    return points


def _at_distance(points: list[RoutePoint], distance_m: float) -> tuple[float, float]:
    """Linear interpolation between the two bracketing vertices.

    The pipeline resamples to roughly 10 m spacing, so the segment a waypoint
    falls in is short and a straight line across it is well inside the
    accuracy the coordinates themselves carry.
    """
    if distance_m <= points[0].distance_m:
        return points[0].lat, points[0].lng
    if distance_m >= points[-1].distance_m:
        return points[-1].lat, points[-1].lng
    for before, after in pairwise(points):
        if before.distance_m <= distance_m <= after.distance_m:
            span = after.distance_m - before.distance_m
            if span <= 0.0:  # pragma: no cover - vertices are strictly ordered
                return before.lat, before.lng
            fraction = (distance_m - before.distance_m) / span
            return (
                before.lat + (after.lat - before.lat) * fraction,
                before.lng + (after.lng - before.lng) * fraction,
            )
    return points[-1].lat, points[-1].lng  # pragma: no cover - covered above


def leg_waypoints(bundle: CourseBundle, leg: Leg, points: list[RoutePoint]) -> list[Waypoint]:
    """Aid stations, cut-offs and course furniture on this leg, in order.

    Waypoints are what make the file worth having: a bare route is already in
    the athlete's head unit from the organiser, but "aid station at 42.1 km,
    take a bottle" is the plan.
    """
    collected: list[Waypoint] = []
    sources: tuple[tuple[str, list[Any]], ...] = (
        ("aid_station", list(bundle.aid_stations or [])),
        ("cutoff", list(bundle.barriers or [])),
        ("waypoint", list(bundle.waypoints or [])),
    )
    for kind, items in sources:
        for item in items:
            if item.get("leg") != leg.value:
                continue
            km = item.get("km")
            if not isinstance(km, int | float):
                continue
            distance_m = float(km) * 1000.0
            lat, lng = _at_distance(points, distance_m)
            collected.append(
                Waypoint(
                    name=str(item.get("name") or kind.replace("_", " ").title()),
                    lat=lat,
                    lng=lng,
                    distance_m=distance_m,
                    kind=str(item.get("type") or kind),
                )
            )
    collected.sort(key=lambda point: point.distance_m)
    return collected


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def calendar_events(context: ExportContext) -> list[CalendarEvent]:
    """Race week, anchored to the event date rather than to weekday names."""
    has_special_needs = any(str(bag.get("key", "")).endswith("_sn") for bag in _bag_keys(context))
    return race_week_events(
        event_date=context.race.event_date,
        course_name=context.course.name,
        has_special_needs=has_special_needs,
    )


def _bag_keys(context: ExportContext) -> list[dict[str, str]]:
    return [{"key": bag.key.value} for bag in context.plan.bags]


def export_filename(*, course_slug: str, event_date: date, suffix: str) -> str:
    """A name that sorts and survives a downloads folder.

    Slug plus date, because an athlete with three races has three of these and
    ``race-card.pdf`` three times over tells them nothing.
    """
    return f"{course_slug}-{event_date.isoformat()}-{suffix}"


def available_exports(plan_id: UUID) -> list[dict[str, str]]:
    """What the UI offers, with the paths it should link to."""
    base = f"/api/v1/plans/{plan_id}/export"
    return [
        {
            "key": "race_card_pdf",
            "label": "Race card (PDF)",
            "media_type": "application/pdf",
            "url": f"{base}/race-card.pdf",
            "description": "One page: pacing, cut-off margins, fuelling, aid plan.",
        },
        {
            "key": "bag_manifests_pdf",
            "label": "Bag manifests (PDF)",
            "media_type": "application/pdf",
            "url": f"{base}/bags.pdf",
            "description": "One page per bag, every item annotated with why it is there.",
        },
        {
            "key": "course_fit",
            "label": "Bike course (.fit)",
            "media_type": "application/vnd.ant.fit",
            "url": f"{base}/course.fit?leg=BIKE",
            "description": "Course with a waypoint at every aid station and cut-off.",
        },
        {
            "key": "course_gpx",
            "label": "Bike route (.gpx)",
            "media_type": "application/gpx+xml",
            "url": f"{base}/course.gpx?leg=BIKE",
            "description": "Route only, for anything that will not take a .fit course.",
        },
        {
            "key": "race_week_ics",
            "label": "Race week (.ics)",
            "media_type": "text/calendar",
            "url": f"{base}/race-week.ics",
            "description": "Registration, pack, check-in and race day.",
        },
    ]
