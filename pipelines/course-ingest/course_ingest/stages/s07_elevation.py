"""Stage 7 -- sample elevation from the DEM.

Every height in a bundle comes from the terrain model. GPS and barometric
altitude are never read, even where a source carries them: a barometer drifts
with the weather, and the solver derives every gradient, every segment power
target and every climb from this series.

If a DEM tile is missing the source raises `MissingDemTile` and the build fails.
Interpolating across a coverage gap would produce a smooth, plausible-looking,
wrong hill.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Sequence

from ..config import Config
from ..geo import Point, elevation_gain
from ..sources.base import DemSource


@dataclass(frozen=True)
class ElevationReport:
    leg: str
    nodes: int
    min_m: float
    max_m: float
    mean_m: float
    gain_m: float
    gain_m_raw_nodes: float
    loss_m: float
    flattened_water_surface: bool
    gain_threshold_m: float


def sample_leg(
    nodes: Sequence[Point],
    leg: str,
    dem: DemSource,
    cfg: Config,
    water_surface: bool = False,
) -> tuple[list[float], ElevationReport]:
    heights = dem.sample(nodes)

    if water_surface:
        # A water surface is level by definition. Sampling it per node would
        # inject gradient into a leg that physically has none, and the solver
        # reads gradient straight off this series. The DEM still supplies the
        # value -- it is the median of the samples over the course, not a
        # fabricated constant.
        level = statistics.median(heights)
        sea_level = float(cfg["course"]["swim"]["sea_level_m"])
        if abs(level - sea_level) < 2.0:
            level = sea_level
        heights = [level] * len(nodes)

    threshold = float(cfg["course"]["elevation"]["gain_threshold_m"])
    raw_gain = elevation_gain(heights, 0.0)
    gain = elevation_gain(heights, threshold)
    loss = sum(max(0.0, heights[i] - heights[i + 1]) for i in range(len(heights) - 1))

    return heights, ElevationReport(
        leg=leg,
        nodes=len(nodes),
        min_m=min(heights),
        max_m=max(heights),
        mean_m=sum(heights) / len(heights),
        gain_m=gain,
        gain_m_raw_nodes=raw_gain,
        loss_m=loss,
        flattened_water_surface=water_surface,
        gain_threshold_m=threshold,
    )
