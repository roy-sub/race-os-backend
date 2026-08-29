"""Stage 1 -- ingest the seed specification.

Resolves a `CourseSpec` into the concrete targets the rest of the pipeline
works against: nominal leg distances, lap structure, and the bounding boxes the
road and DEM sources will be asked for.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import Config
from ..geo import Point, local_scale
from ..spec import CourseSpec


@dataclass(frozen=True)
class BuildPlan:
    spec: CourseSpec
    start: Point
    swim_target_m: float
    bike_target_m: float
    run_target_m: float
    bike_lap_m: float
    run_lap_m: float
    bike_character: str
    run_character: str
    bike_bbox: tuple[float, float, float, float]
    run_bbox: tuple[float, float, float, float]
    swim_bbox: tuple[float, float, float, float]
    waypoint_count: int


def _bbox_for(start: Point, target_m: float, cfg: Config) -> tuple[float, float, float, float]:
    """A box big enough to hold a loop of `target_m` around `start`.

    Sized from the loop's own geometry: a closed loop of length L reaches at
    most L / (2*pi) from its centre if circular, and rather less in practice
    because roads are not circles. The configured margin covers the difference.
    """
    routing = cfg["routing"]
    radius_m = target_m / (2.0 * math.pi) * (1.0 + float(routing["graph_bbox_margin_fraction"]))
    m_lon, m_lat = local_scale(start[1])
    dlon = radius_m / m_lon
    dlat = radius_m / m_lat
    floor = float(routing["graph_bbox_min_degrees"])
    dlon = max(dlon, floor)
    dlat = max(dlat, floor)
    return (start[0] - dlon, start[1] - dlat, start[0] + dlon, start[1] + dlat)


def ingest(spec: CourseSpec, cfg: Config) -> BuildPlan:
    distances = cfg["course"]["distances"][spec.distance_type]
    swim_m = float(distances["swim_m"])
    bike_m = float(distances["bike_m"])
    run_m = float(distances["run_m"])

    if spec.bike.laps < 1 or spec.run.laps < 1:
        raise ValueError(f"{spec.course_id}: lap counts must be >= 1")

    bike_lap = bike_m / spec.bike.laps
    run_lap = run_m / spec.run.laps
    start = (spec.start_lng, spec.start_lat)

    return BuildPlan(
        spec=spec,
        start=start,
        swim_target_m=swim_m,
        bike_target_m=bike_m,
        run_target_m=run_m,
        bike_lap_m=bike_lap,
        run_lap_m=run_lap,
        bike_character=spec.bike.character or spec.character,
        run_character=spec.run.character or spec.character,
        bike_bbox=_bbox_for(start, bike_lap, cfg),
        run_bbox=_bbox_for(start, run_lap, cfg),
        swim_bbox=_bbox_for(start, max(swim_m, 4000.0), cfg),
        waypoint_count=int(cfg["routing"]["loop"]["waypoint_count_by_distance"][spec.distance_type]),
    )
