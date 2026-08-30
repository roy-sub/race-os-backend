"""Normalising the two course formats into one ``CourseBundleSnapshot``.

Two very different artefacts feed the solver, and keeping both is deliberate
(``docs/FIELD_NAME_RECONCILIATION.md`` R-003):

**Golden fixtures** (``backend/tests/golden/courses/*.json``) are synthetic,
generated to reproduce ``SOLVER_MODEL.md`` §B.1's exact net gradients. Node
series are ``[[s_m, h_m], …]``; ``distance_type`` is the solver's vocabulary.

**Pipeline bundles** (``pipelines/course-ingest/out/bundles/*.bundle.json``)
are real routed geometry. Node series are the Z ordinates of an EWKT
``LINESTRING Z``; ``distance_type`` is the product's vocabulary.

They must never be interchangeable — a golden case that read a pipeline bundle
would make the solver's determinism guarantee a property of the routing engine
rather than of the solver, and regenerating a course would silently move the
expectations. ``test_golden_isolation.py`` asserts the separation. This module
is the *only* place either shape is understood.
"""

from __future__ import annotations

import json
import re
from itertools import pairwise
from pathlib import Path
from typing import Any

from raceos.domain.enums import (
    DISTANCE_TO_SOLVER,
    DistanceType,
    Leg,
    SolverDistance,
    SurfaceQuality,
)
from raceos.solver.models import (
    AidStation,
    Barrier,
    CourseBundleSnapshot,
    CourseLeg,
    CourseSegment,
    ElevationNode,
)

#: `SRID=4326;LINESTRING Z (lon lat elev, …)`
_EWKT = re.compile(r"LINESTRING\s*Z?\s*\(([^)]*)\)", re.IGNORECASE)


def _mean_elevation(nodes: tuple[ElevationNode, ...]) -> float:
    """Distance-weighted mean elevation over the delivered series."""
    if len(nodes) < 2:
        return nodes[0].h_m if nodes else 0.0
    weighted = 0.0
    total = 0.0
    for lower, upper in pairwise(nodes):
        run = upper.s_m - lower.s_m
        if run <= 0:
            continue
        weighted += (lower.h_m + upper.h_m) / 2.0 * run
        total += run
    return weighted / total if total else 0.0


# ---------------------------------------------------------------------------
# Golden fixtures
# ---------------------------------------------------------------------------


def from_golden_fixture(payload: dict[str, Any]) -> CourseBundleSnapshot:
    """Adapt a synthetic golden course."""
    legs: list[CourseLeg] = []
    for name, data in payload["legs"].items():
        leg = Leg(name)
        nodes = tuple(ElevationNode(s_m=float(s), h_m=float(h)) for s, h in data["nodes"])
        legs.append(
            CourseLeg(
                leg=leg,
                distance_m=float(data["distance_m"]),
                nodes=nodes,
                surface_quality=SurfaceQuality(data.get("surface_quality") or "typical_road"),
                mean_elevation_m=float(data.get("mean_elevation_m", _mean_elevation(nodes))),
            )
        )

    segments = tuple(
        CourseSegment(
            ordinal=int(s["ordinal"]),
            leg=Leg(s["leg"]),
            name=str(s["name"]),
            from_km=float(s["from_km"]),
            to_km=float(s["to_km"]),
            surface_quality=SurfaceQuality(s["surface_quality"]),
        )
        for s in payload["segments"]
    )

    barriers = tuple(
        Barrier(
            name=str(b["name"]),
            leg=Leg(b["leg"]),
            km=float(b["km"]),
            limit_minutes_from_start=float(b["limit_minutes_from_start"]),
        )
        for b in payload["barriers"]
    )

    aid_stations = tuple(
        AidStation(
            leg=Leg(a["leg"]),
            name=str(a["name"]),
            km=float(a["km"]),
            contents=tuple(a.get("contents") or ()),
        )
        for a in payload["aid_stations"]
    )

    return CourseBundleSnapshot(
        course_id=str(payload["golden_course_id"]),
        # Golden fixtures already speak the solver's vocabulary.
        distance=SolverDistance(payload["distance_type"]),
        legs=tuple(sorted(legs, key=lambda leg: leg.leg.value)),
        segments=tuple(sorted(segments, key=lambda s: s.ordinal)),
        barriers=tuple(sorted(barriers, key=lambda b: b.limit_minutes_from_start)),
        aid_stations=aid_stations,
        elevation_source=str(payload.get("elevation_source", "terrain")),
    )


def load_golden_course(path: Path) -> CourseBundleSnapshot:
    return from_golden_fixture(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Pipeline bundles
# ---------------------------------------------------------------------------


def parse_ewkt_linestring_z(ewkt: str) -> tuple[tuple[float, float, float], ...]:
    """``(lon, lat, elevation)`` triples from an EWKT ``LINESTRING Z``."""
    match = _EWKT.search(ewkt)
    if match is None:
        raise ValueError("geometry is not an EWKT LINESTRING Z")
    points: list[tuple[float, float, float]] = []
    for chunk in match.group(1).split(","):
        parts = chunk.split()
        if len(parts) < 3:
            raise ValueError(f"LINESTRING Z vertex has {len(parts)} ordinates, need 3")
        points.append((float(parts[0]), float(parts[1]), float(parts[2])))
    return tuple(points)


def _cumulative_nodes(
    points: tuple[tuple[float, float, float], ...], distance_m: float
) -> tuple[ElevationNode, ...]:
    """Turn vertices into ``(cumulative_distance, elevation)`` nodes.

    Distance is distributed evenly across the vertices and scaled to the leg's
    declared ``distance_m``. The pipeline resamples to ~10 m nodes, so spacing
    is already near-uniform, and the leg's own measured distance is more
    trustworthy than a haversine sum recomputed here — recomputing it would
    also make the solver's numbers depend on this module's geodesy rather than
    on the bundle's.
    """
    count = len(points)
    if count < 2:
        raise ValueError("a leg needs at least two vertices")
    step = distance_m / (count - 1)
    return tuple(
        ElevationNode(s_m=index * step, h_m=point[2]) for index, point in enumerate(points)
    )


def from_pipeline_bundle(payload: dict[str, Any]) -> CourseBundleSnapshot:
    """Adapt a generated course bundle."""
    course = payload["course"]
    bundle = payload["course_bundle"]

    legs: list[CourseLeg] = []
    for leg_data in payload["course_bundle_legs"]:
        distance_m = float(leg_data["distance_m"])
        points = parse_ewkt_linestring_z(leg_data["geometry"])
        nodes = _cumulative_nodes(points, distance_m)
        legs.append(
            CourseLeg(
                leg=Leg(leg_data["leg"]),
                distance_m=distance_m,
                nodes=nodes,
                surface_quality=SurfaceQuality(leg_data["surface_quality"]),
                mean_elevation_m=_mean_elevation(nodes),
            )
        )

    segments = tuple(
        CourseSegment(
            ordinal=int(s["ordinal"]),
            leg=Leg(s["leg"]),
            name=str(s["name"]),
            from_km=float(s["from_km"]),
            to_km=float(s["to_km"]),
            surface_quality=SurfaceQuality(s["surface_quality"]),
        )
        for s in bundle["segments"]
    )

    barriers = tuple(
        Barrier(
            name=str(b["name"]),
            leg=Leg(b["leg"]),
            km=float(b["km"]),
            limit_minutes_from_start=float(b["limit_minutes_from_start"]),
        )
        for b in bundle["barriers"]
    )

    aid_stations = tuple(
        AidStation(
            leg=Leg(a["leg"]),
            name=str(a["name"]),
            km=float(a["km"]),
            contents=tuple(a.get("contents") or ()),
        )
        for a in bundle["aid_stations"]
    )

    return CourseBundleSnapshot(
        course_id=str(course["slug"]),
        # The product's vocabulary translated once, here.
        distance=DISTANCE_TO_SOLVER[DistanceType(course["distance_type"])],
        legs=tuple(sorted(legs, key=lambda leg: leg.leg.value)),
        segments=tuple(sorted(segments, key=lambda s: s.ordinal)),
        barriers=tuple(sorted(barriers, key=lambda b: b.limit_minutes_from_start)),
        aid_stations=aid_stations,
        elevation_source=str(bundle.get("elevation_source", "terrain")),
    )


def load_pipeline_bundle(path: Path) -> CourseBundleSnapshot:
    return from_pipeline_bundle(json.loads(path.read_text(encoding="utf-8")))
