"""Stage 4 -- clean the raw geometry.

Drops duplicate vertices, removes positional outliers, closes loops that should
close, and hands back one point list per leg. On the generation path the input
is already road geometry so there is little to do; on the GPX upload path this
is the stage that earns its keep, and it is the same code either way.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..config import Config
from ..geo import Point, haversine_m


@dataclass(frozen=True)
class CleanReport:
    leg: str
    input_points: int
    output_points: int
    duplicates_removed: int
    outliers_removed: int
    loop_closed: bool
    closure_gap_m: float


def clean_leg(
    points: Sequence[Point],
    leg: str,
    cfg: Config,
    close_loop: bool,
) -> tuple[list[Point], CleanReport]:
    spacing = float(cfg["course"]["resample"]["node_spacing_m"])
    # A vertex closer than a tenth of the target node spacing carries no shape
    # information and only destabilises the gradient at resample time.
    min_step = spacing * 0.1
    # An outlier is a vertex whose detour past its neighbours is implausible for
    # a road: it is the signature of a GPS spike or a routing artefact.
    max_detour_ratio = 6.0
    max_jump_m = 2000.0

    pts = list(points)
    n_in = len(pts)
    if n_in < 2:
        raise ValueError(f"{leg}: fewer than two points to clean")

    deduped: list[Point] = [pts[0]]
    for p in pts[1:]:
        if haversine_m(deduped[-1], p) >= min_step:
            deduped.append(p)
    if haversine_m(deduped[-1], pts[-1]) > 0.0 and deduped[-1] != pts[-1]:
        deduped.append(pts[-1])
    duplicates = len(pts) - len(deduped)

    kept: list[Point] = [deduped[0]]
    outliers = 0
    for i in range(1, len(deduped) - 1):
        a, b, c = kept[-1], deduped[i], deduped[i + 1]
        direct = haversine_m(a, c)
        detour = haversine_m(a, b) + haversine_m(b, c)
        if haversine_m(a, b) > max_jump_m:
            outliers += 1
            continue
        if direct > 1.0 and detour / direct > max_detour_ratio:
            outliers += 1
            continue
        kept.append(b)
    kept.append(deduped[-1])

    gap = haversine_m(kept[0], kept[-1])
    closed = False
    if close_loop and gap > 0.0:
        # The loop router returns to the same graph node, so any gap here is
        # sub-metre float drift; snapping it shut keeps the leg's start and
        # finish exactly coincident, which the transition placement relies on.
        kept[-1] = kept[0]
        closed = True

    return kept, CleanReport(
        leg=leg,
        input_points=n_in,
        output_points=len(kept),
        duplicates_removed=duplicates,
        outliers_removed=outliers,
        loop_closed=closed,
        closure_gap_m=gap,
    )
