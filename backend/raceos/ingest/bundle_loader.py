"""Load a generated course bundle into the database.

The bundles in ``pipelines/course-ingest/out/bundles/`` are the seed fixtures.
They are shaped as the rows expect — one ``courses`` row, one
``course_bundles`` row, three ``course_bundle_legs`` rows with geometry as
EWKT ``LINESTRING Z`` — so loading is a transcription, not a transformation.
**No geometry is fabricated and none is recomputed here.**

Loading is idempotent by ``(course.slug, bundle.version)``: re-running the
seed updates in place rather than duplicating, which is what lets ``make seed``
be re-run safely.

Validation happens *before* any row is written. A bundle that would violate one
of ``SOLVER_MODEL.md`` §1.2's invariants is rejected with a message naming the
problem, because a bundle rejected at load time costs an admin a minute and the
same bundle rejected at solve time costs an athlete their race plan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.db.models import Course, CourseBundle, CourseBundleLeg
from raceos.domain.enums import (
    BundleStatus,
    Difficulty,
    DistanceType,
    Leg,
    Provenance,
    SurfaceQuality,
)
from raceos.logging import get_logger

logger = get_logger(__name__)

#: Every leg the solver costs must be present. §1.2: a bundle missing one is
#: not a bundle.
REQUIRED_LEGS: frozenset[str] = frozenset({"SWIM", "BIKE", "RUN"})


class BundleValidationError(ValueError):
    """A bundle is not fit to load. Carries every problem found, not just the first."""

    def __init__(self, source: str, problems: list[str]) -> None:
        self.source = source
        self.problems = problems
        super().__init__(
            f"{source} failed validation with {len(problems)} problem(s):\n  - "
            + "\n  - ".join(problems)
        )


@dataclass
class LoadResult:
    slug: str
    version: str
    created: bool
    course_id: str
    bundle_id: str
    legs: int
    segments: int
    barriers: int
    aid_stations: int
    waypoints: int
    warnings: list[str] = field(default_factory=list)


def validate_bundle(payload: dict[str, Any], source: str) -> list[str]:
    """Return every problem with *payload*. Empty means it is loadable.

    Deliberately re-checks the things the pipeline already asserts. The
    pipeline is trusted, but a fixture can be hand-edited between generation
    and load, and this is the last point at which a bad bundle is cheap.
    """
    problems: list[str] = []

    course = payload.get("course")
    bundle = payload.get("course_bundle")
    legs = payload.get("course_bundle_legs")

    if not isinstance(course, dict):
        problems.append("missing `course` object")
    if not isinstance(bundle, dict):
        problems.append("missing `course_bundle` object")
    if not isinstance(legs, list):
        problems.append("missing `course_bundle_legs` array")
    if problems:
        return problems

    # Narrowing for the type checker; the guards above already returned.
    assert isinstance(course, dict)
    assert isinstance(bundle, dict)
    assert isinstance(legs, list)

    # --- course ------------------------------------------------------
    for key in ("name", "place", "slug", "distance_type", "difficulty", "timezone", "lat", "lng"):
        if course.get(key) in (None, ""):
            problems.append(f"course.{key} is missing")

    if course.get("distance_type") not in {d.value for d in DistanceType}:
        problems.append(
            f"course.distance_type {course.get('distance_type')!r} is not one of "
            f"{sorted(d.value for d in DistanceType)}"
        )
    if course.get("difficulty") not in {d.value for d in Difficulty}:
        problems.append(f"course.difficulty {course.get('difficulty')!r} is not a known difficulty")

    # --- bundle invariants (SOLVER_MODEL.md §1.2) --------------------
    if bundle.get("elevation_source") != "terrain":
        problems.append(
            f"course_bundle.elevation_source is {bundle.get('elevation_source')!r}; "
            f"§1.2 requires terrain-sampled elevation, never barometric or GPS"
        )
    if not str(bundle.get("attribution") or "").strip():
        problems.append("course_bundle.attribution is empty; ODbL obliges attribution")

    barriers = bundle.get("barriers") or []
    if not barriers:
        problems.append("course_bundle.barriers is empty; §1.2 calls zero barriers a data error")
    else:
        limits = [b.get("limit_minutes_from_start") for b in barriers]
        if any(limit is None for limit in limits):
            problems.append("a barrier is missing limit_minutes_from_start")
        elif list(limits) != sorted(limits):
            problems.append(
                f"barrier limits are not chronologically ordered: {limits}. "
                f"§1.2: a bike cut-off before the swim exit is a corrupt bundle"
            )

    # --- legs --------------------------------------------------------
    present = {leg.get("leg") for leg in legs}
    missing = REQUIRED_LEGS - present
    if missing:
        problems.append(f"missing leg(s) {sorted(missing)}")

    leg_distances: dict[str, float] = {}
    for leg in legs:
        name = leg.get("leg", "?")
        geometry = leg.get("geometry") or ""
        if not geometry.startswith("SRID=4326;LINESTRING Z"):
            problems.append(
                f"leg {name} geometry is not EWKT LINESTRING Z; the Z ordinate is the "
                f"elevation series the solver reads"
            )
        distance = leg.get("distance_m")
        if not isinstance(distance, int | float) or distance <= 0:
            problems.append(f"leg {name} has a non-positive distance_m")
        else:
            leg_distances[name] = float(distance)
        node_count = leg.get("node_count")
        if not isinstance(node_count, int) or node_count < 2:
            problems.append(f"leg {name} has node_count {node_count!r}; at least two are required")
        else:
            # The classic way an elevation series desynchronises from its
            # geometry is a node count that does not match the vertices.
            vertices = geometry.count(",") + 1 if geometry else 0
            if vertices and vertices != node_count:
                problems.append(
                    f"leg {name} declares node_count {node_count} but its geometry has "
                    f"{vertices} vertices"
                )
        if leg.get("surface_quality") not in {s.value for s in SurfaceQuality}:
            problems.append(
                f"leg {name} surface_quality {leg.get('surface_quality')!r} is unmapped; "
                f"it becomes Crr and must not be guessed"
            )

    # --- furniture within its leg ------------------------------------
    for label, items in (
        ("aid_stations", bundle.get("aid_stations") or []),
        ("waypoints", bundle.get("waypoints") or []),
        ("barriers", barriers),
    ):
        for item in items:
            leg_name = item.get("leg")
            km = item.get("km")
            if (
                leg_name in leg_distances
                and isinstance(km, int | float)
                and km * 1000.0 > leg_distances[leg_name] + 1.0
            ):
                problems.append(
                    f"{label} entry {item.get('name')!r} is at km {km} on {leg_name}, "
                    f"past that leg's {leg_distances[leg_name] / 1000:.3f} km"
                )

    # --- segments must tile each leg they cover ----------------------
    segments = bundle.get("segments") or []
    by_leg: dict[str, list[dict[str, Any]]] = {}
    for segment in segments:
        by_leg.setdefault(segment.get("leg", "?"), []).append(segment)
    for leg_name, group in by_leg.items():
        ordered = sorted(group, key=lambda s: s.get("from_km", 0.0))
        for previous, current in pairwise(ordered):
            if abs(float(current["from_km"]) - float(previous["to_km"])) > 1e-6:
                problems.append(
                    f"segments on {leg_name} do not tile: {previous['name']!r} ends at "
                    f"{previous['to_km']} but {current['name']!r} starts at {current['from_km']}. "
                    f"A gap is silently unpaced road"
                )
                break

    return problems


def load_bundle_payload(
    session: Session, payload: dict[str, Any], source: str = "<payload>"
) -> LoadResult:
    """Load one validated bundle. Raises :class:`BundleValidationError` first."""
    problems = validate_bundle(payload, source)
    if problems:
        raise BundleValidationError(source, problems)

    course_data: dict[str, Any] = payload["course"]
    bundle_data: dict[str, Any] = payload["course_bundle"]
    legs_data: list[dict[str, Any]] = payload["course_bundle_legs"]

    course = session.scalar(select(Course).where(Course.slug == course_data["slug"]))
    if course is None:
        course = Course(slug=course_data["slug"])
        session.add(course)

    course.name = course_data["name"]
    course.place = course_data["place"]
    course.distance_type = DistanceType(course_data["distance_type"])
    course.difficulty = Difficulty(course_data["difficulty"])
    course.elevation_gain_m = course_data.get("elevation_gain_m")
    course.media_hero_path = course_data.get("media_hero_path")
    course.media_card_path = course_data.get("media_card_path")
    course.tone_color = course_data.get("tone_color")
    course.timezone = course_data["timezone"]
    course.lat = course_data["lat"]
    course.lng = course_data["lng"]
    course.is_fictional = bool(course_data.get("is_fictional", True))
    session.flush()

    version = bundle_data["version"]
    bundle = session.scalar(
        select(CourseBundle).where(
            CourseBundle.course_id == course.id, CourseBundle.version == version
        )
    )
    created = bundle is None
    if bundle is None:
        bundle = CourseBundle(course_id=course.id, version=version)
        session.add(bundle)

    bundle.status = BundleStatus(bundle_data.get("status", "draft"))
    bundle.provenance = Provenance(bundle_data.get("provenance", "ESTIMATED"))
    bundle.verified_at = bundle_data.get("verified_at")
    bundle.published_at = bundle_data.get("published_at")
    bundle.season_year = bundle_data.get("season_year")
    bundle.elevation_profile = bundle_data.get("elevation_profile") or {}
    bundle.barriers = bundle_data.get("barriers") or []
    bundle.aid_stations = bundle_data.get("aid_stations") or []
    bundle.waypoints = bundle_data.get("waypoints") or []
    bundle.segments = bundle_data.get("segments") or []
    bundle.elevation_source = bundle_data.get("elevation_source", "terrain")
    bundle.attribution = bundle_data["attribution"]
    bundle.changelog = bundle_data.get("changelog")
    bundle.plans_affected_count = bundle_data.get("plans_affected_count") or 0
    bundle.bundle_asset_key = bundle_data.get("bundle_asset_key")
    bundle.terrain_pmtiles_key = bundle_data.get("terrain_pmtiles_key")
    bundle.provenance_detail = payload.get("provenance_detail") or {}
    session.flush()

    existing = {
        leg.leg: leg
        for leg in session.scalars(
            select(CourseBundleLeg).where(CourseBundleLeg.bundle_id == bundle.id)
        )
    }
    for leg_data in legs_data:
        leg_enum = Leg(leg_data["leg"])
        leg = existing.get(leg_enum)
        if leg is None:
            leg = CourseBundleLeg(bundle_id=bundle.id, leg=leg_enum)
            session.add(leg)
        leg.geometry = leg_data["geometry"]
        leg.distance_m = leg_data["distance_m"]
        leg.elevation_gain_m = leg_data.get("elevation_gain_m") or 0
        leg.node_count = leg_data["node_count"]
        leg.surface_quality = SurfaceQuality(leg_data["surface_quality"])
    session.flush()

    result = LoadResult(
        slug=course.slug,
        version=version,
        created=created,
        course_id=str(course.id),
        bundle_id=str(bundle.id),
        legs=len(legs_data),
        segments=len(bundle.segments),
        barriers=len(bundle.barriers),
        aid_stations=len(bundle.aid_stations),
        waypoints=len(bundle.waypoints),
    )
    logger.info(
        "course bundle loaded",
        extra={
            "course_slug": result.slug,
            "bundle_version": result.version,
            "newly_created": result.created,
            "legs": result.legs,
            "segments": result.segments,
        },
    )
    return result


def load_bundle_file(session: Session, path: Path) -> LoadResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return load_bundle_payload(session, payload, source=str(path))


def load_bundle_directory(session: Session, directory: Path) -> list[LoadResult]:
    """Load every ``*.bundle.json`` in *directory*, in a stable order."""
    results: list[LoadResult] = []
    for path in sorted(directory.glob("*.bundle.json")):
        results.append(load_bundle_file(session, path))
    return results
