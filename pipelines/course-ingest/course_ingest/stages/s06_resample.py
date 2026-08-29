"""Stage 6 -- resample to ~10 m nodes.

Fixed node spacing is what makes gradient stable within a course and comparable
between courses: the solver reads gradient as a forward difference over the
delivered nodes (SOLVER_MODEL.md 1.1), so uneven spacing would make an identical
hill look different depending on how the source happened to place its vertices.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..config import Config
from ..geo import Point, cumulative_m, resample


@dataclass(frozen=True)
class ResampleReport:
    leg: str
    input_points: int
    node_count: int
    spacing_m: float
    length_m: float
    min_step_m: float
    max_step_m: float


def resample_leg(points: Sequence[Point], leg: str, cfg: Config):
    spacing = float(cfg["course"]["resample"]["node_spacing_m"])
    nodes = resample(points, spacing)
    cum = cumulative_m(nodes)
    steps = [cum[i + 1] - cum[i] for i in range(len(cum) - 1)]
    return nodes, ResampleReport(
        leg=leg,
        input_points=len(points),
        node_count=len(nodes),
        spacing_m=spacing,
        length_m=cum[-1],
        min_step_m=min(steps) if steps else 0.0,
        max_step_m=max(steps) if steps else 0.0,
    )
