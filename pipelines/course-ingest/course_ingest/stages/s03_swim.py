"""Stage 3 -- draw the swim leg.

The swim is the one leg with no road under it, and therefore the one place the
pipeline is allowed to draw geometry rather than follow it. What is drawn is
still constrained by real data: the buoy polygon must lie inside a real water
body from the map, clear of the shoreline by a configured margin, with the
start and finish arch at a real shoreline point.

Elevation on the swim leg is the DEM's median over the course, held constant.
A water surface is level; sampling it per node would inject gradient into a leg
that physically has none, and the solver reads gradient straight off the node
series.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import Config
from ..geo import (
    Point,
    bbox_of,
    clearance_to_segments,
    densify,
    destination,
    path_length_m,
    points_in_ring_np,
    ring_arrays,
)
from ..sources.base import RoadSource
from .s01_ingest import BuildPlan


class SwimDrawError(RuntimeError):
    """No plausible swim course fits in real water at this start."""


_SUBTYPE_PREFERENCE = {
    "sea": ("ocean", "physical", "water", "bay"),
    "harbour": ("physical", "ocean", "water", "bay"),
    "lake": ("lake", "reservoir", "water"),
    "canal": ("canal", "river", "water"),
}


@dataclass(frozen=True)
class SwimResult:
    points: tuple[Point, ...]
    buoys: tuple[Point, ...]
    laps: int
    lap_perimeter_m: float
    water_name: str | None
    water_subtype: str
    shape: str
    bearing_deg: float


def _shape_vertices(anchor: Point, bearing: float, shape: str, perimeter: float, aspect: float):
    """Buoy polygon whose start arch sits at `anchor` and which extends offshore
    along `bearing`.

    Both shapes are laid out symmetrically about the offshore axis rather than
    hung off one corner, so every buoy is at least as far out as the arch. A
    course with one vertex swung back along the shore is how a plausible-looking
    triangle ends up with a buoy in a car park.
    """
    perp = bearing + math.pi / 2.0
    if shape.startswith("rectangle"):
        short = perimeter / (2.0 * (1.0 + aspect))
        long = aspect * short
        a = destination(anchor, perp, -short / 2.0)
        b = destination(a, bearing, long)
        c = destination(b, perp, short)
        d = destination(c, bearing, -long)
        return [anchor, a, b, c, d, anchor]
    if shape.startswith("triangle"):
        side = perimeter / 3.0
        apex_offset = side * math.sqrt(3.0) / 2.0
        mid = destination(anchor, bearing, apex_offset)
        b = destination(mid, perp, -side / 2.0)
        c = destination(mid, perp, side / 2.0)
        return [anchor, b, c, anchor]
    raise SwimDrawError(f"unknown swim shape `{shape}`")


class WaterMask:
    """The union of every acceptable water body in the bbox, with tile seams
    removed.

    Overture tiles large water bodies into adjacent polygons, so a 250 m buoy
    course near a coast routinely straddles two of them. Testing against a
    single selected ring reports the middle of the sea as dry land, which is
    what a first attempt at this did. So containment is tested against the
    union, and clearance is measured only against segments that appear in one
    ring -- a seam shared by two water polygons is not a shoreline.
    """

    def __init__(self, rings, preference: tuple[str, ...]) -> None:
        self.rings = [r for r in rings if r[0] in preference]
        if not self.rings:
            raise SwimDrawError("no water bodies of an acceptable kind in range")
        self._arrays = [ring_arrays(ring) for _st, _nm, ring in self.rings]

        seen: dict[tuple, int] = {}
        for _st, _nm, ring in self.rings:
            for i in range(len(ring)):
                a = ring[i]
                b = ring[(i + 1) % len(ring)]
                key = tuple(sorted((_round(a), _round(b))))
                seen[key] = seen.get(key, 0) + 1
        self._shore: list[tuple[Point, Point]] = []
        for _st, _nm, ring in self.rings:
            for i in range(len(ring)):
                a = ring[i]
                b = ring[(i + 1) % len(ring)]
                if seen[tuple(sorted((_round(a), _round(b))))] == 1:
                    self._shore.append((a, b))

    def contains(self, points) -> list[bool]:
        result = [False] * len(points)
        for rx, ry in self._arrays:
            for i, inside in enumerate(points_in_ring_np(points, rx, ry)):
                if inside:
                    result[i] = True
        return result

    def describe(self, point: Point):
        """The named body a point sits in, for the bundle's provenance block."""
        for (subtype, name, _ring), (rx, ry) in zip(self.rings, self._arrays):
            if points_in_ring_np([point], rx, ry)[0]:
                return subtype, name
        return self.rings[0][0], self.rings[0][1]

    def clearance(self, points, bbox) -> list[float]:
        local = [
            seg for seg in self._shore
            if _in_box(seg[0], bbox, 0.02) or _in_box(seg[1], bbox, 0.02)
        ]
        if not local:
            return [float("inf")] * len(points)
        return [clearance_to_segments(p, local) for p in points]


def _round(p: Point) -> tuple[int, int]:
    return (int(round(p[0] * 1e7)), int(round(p[1] * 1e7)))


def _in_box(p: Point, bbox, margin: float) -> bool:
    x0, y0, x1, y1 = bbox
    return x0 - margin <= p[0] <= x1 + margin and y0 - margin <= p[1] <= y1 + margin


def draw_swim(plan: BuildPlan, cfg: Config, roads: RoadSource) -> SwimResult:
    swim_cfg = cfg["course"]["swim"]
    shape = swim_cfg["shape_by_distance"][plan.spec.distance_type]
    laps = 2 if shape.endswith("two_lap") else 1
    perimeter = plan.swim_target_m / laps
    aspect = float(swim_cfg["rectangle_aspect"])
    shore_offset = float(swim_cfg["shore_offset_m"])
    clearance_min = float(swim_cfg["min_water_clearance_m"])
    densify_m = float(swim_cfg["densify_m"])

    rings = roads.water_rings_in_bbox(plan.swim_bbox)
    if not rings:
        raise SwimDrawError(
            f"{plan.spec.course_id}: no water bodies found near "
            f"{plan.spec.start_lat:.4f},{plan.spec.start_lng:.4f}"
        )
    mask = WaterMask(rings, _SUBTYPE_PREFERENCE[plan.spec.water_kind])

    base_bearing = math.radians(plan.spec.swim_bearing_deg)
    # Deterministic search order: the specified bearing first, then alternating
    # offsets outward. A course that will not fit at any of these is a spec
    # problem, not something to paper over.
    offsets = [0.0] + [
        math.radians(sign * step)
        for step in range(10, 190, 10)
        for sign in (1, -1)
    ]

    max_anchors = int(swim_cfg["max_anchor_candidates"])
    errors: list[str] = []
    for offset in offsets:
        bearing = base_bearing + offset
        anchors = _anchor_candidates(
            (plan.spec.start_lng, plan.spec.start_lat), bearing, mask, shore_offset, clearance_min
        )
        if not anchors:
            errors.append(f"bearing {math.degrees(bearing) % 360:.0f}: no shoreline anchor")
            continue
        for anchor in anchors[:max_anchors]:
            vertices = _shape_vertices(anchor, bearing, shape, perimeter, aspect)
            dense = densify(vertices, densify_m)
            if not _all_clear(dense, mask, clearance_min):
                continue
            lap = dense
            points: list[Point] = []
            for i in range(laps):
                points.extend(lap if i == 0 else lap[1:])
            subtype, name = mask.describe(anchor)
            return SwimResult(
                points=tuple(points),
                buoys=tuple(vertices[:-1]),
                laps=laps,
                lap_perimeter_m=path_length_m(lap),
                water_name=name,
                water_subtype=subtype,
                shape=shape,
                bearing_deg=(math.degrees(bearing) % 360.0),
            )
        errors.append(f"bearing {math.degrees(bearing) % 360:.0f}: buoy course leaves the water")

    raise SwimDrawError(
        f"{plan.spec.course_id}: could not fit a {perimeter:.0f} m {shape} swim in real water at "
        f"{plan.spec.start_lat:.4f},{plan.spec.start_lng:.4f}. Tried {len(offsets)} bearings. "
        f"First failures: {errors[:3]}"
    )


def _anchor_candidates(start: Point, bearing: float, mask: "WaterMask", shore_offset: float, clearance_min: float):
    """Every qualifying start-arch position walking offshore from the spec's
    shoreline point, nearest first.

    Returning a list rather than the first hit lets the caller push the whole
    buoy course further out when the shape does not fit close in -- which is
    what a race organiser would do, and what a bay narrower than the course
    requires.
    """
    steps = [shore_offset + 10.0 * k for k in range(0, 61)]
    probes = [destination(start, bearing, d) for d in steps]
    inside = mask.contains(probes)
    clearances = mask.clearance(probes, bbox_of(probes))
    return [p for p, ok, clear in zip(probes, inside, clearances) if ok and clear >= clearance_min]


def _all_clear(points, mask: "WaterMask", clearance_min: float) -> bool:
    if not all(mask.contains(points)):
        return False
    box = bbox_of(points)
    return all(c >= clearance_min for c in mask.clearance(points, box))
