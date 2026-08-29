"""Stage 5 -- map-match the bike and run legs onto road geometry.

Snaps each point to the nearest routable road span, subject to a continuity
constraint so the trace cannot hop between parallel carriageways. On the
generation path the input is already road geometry, so this is a verification
pass that should move nothing; the report says how far it moved anything, and a
non-zero maximum on a generated course is a bug worth seeing. On the GPX upload
path it is doing the real work of removing device noise.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..config import Config
from ..geo import Point, haversine_m, local_scale, point_segment_distance_m
from ..graph import RoadGraph


@dataclass(frozen=True)
class MatchReport:
    leg: str
    points: int
    matched: int
    max_offset_m: float
    mean_offset_m: float
    unmatched: int


class _EdgeIndex:
    """Uniform grid over edge vertices; enough for the tens of thousands of
    spans a course bbox contains, and free of any library dependency."""

    def __init__(self, graph: RoadGraph, cell_deg: float = 0.004) -> None:
        self.graph = graph
        self.cell = cell_deg
        self.buckets: dict[tuple[int, int], list[int]] = {}
        for ei, edge in enumerate(graph.edges):
            for x, y in edge.geometry:
                self.buckets.setdefault((int(x / cell_deg), int(y / cell_deg)), []).append(ei)
        for key in self.buckets:
            self.buckets[key] = sorted(set(self.buckets[key]))

    def near(self, p: Point) -> list[int]:
        cx, cy = int(p[0] / self.cell), int(p[1] / self.cell)
        out: set[int] = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                out.update(self.buckets.get((cx + dx, cy + dy), ()))
        return sorted(out)


def map_match(
    points: Sequence[Point],
    graph: RoadGraph,
    leg: str,
    cfg: Config,
    index: _EdgeIndex | None = None,
) -> tuple[list[Point], MatchReport]:
    mm = cfg["routing"]["map_match"]
    max_snap = float(mm["max_snap_distance_m"])
    window = float(mm["continuity_window_m"])
    idx = index or _EdgeIndex(graph)

    out: list[Point] = []
    offsets: list[float] = []
    unmatched = 0
    previous: Point | None = None
    previous_input: Point | None = None

    for p in points:
        # The continuity bound has to scale with how far the trace itself moved.
        # A rural road can run 600 m between vertices, and a fixed window would
        # reject every candidate there and leave the point unmatched.
        allowed = window
        if previous_input is not None:
            allowed = max(window, 2.0 * haversine_m(previous_input, p))
        best: tuple[float, Point] | None = None
        for ei in idx.near(p):
            geom = graph.edges[ei].geometry
            for i in range(len(geom) - 1):
                d, t = point_segment_distance_m(p, geom[i], geom[i + 1])
                if d > max_snap:
                    continue
                a, b = geom[i], geom[i + 1]
                cand = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                if previous is not None:
                    mx, my = local_scale(p[1])
                    step = (
                        ((cand[0] - previous[0]) * mx) ** 2 + ((cand[1] - previous[1]) * my) ** 2
                    ) ** 0.5
                    if step > allowed:
                        continue
                if best is None or d < best[0]:
                    best = (d, cand)
        if best is None:
            unmatched += 1
            out.append(p)
            offsets.append(0.0)
            previous = p
            previous_input = p
            continue
        out.append(best[1])
        offsets.append(best[0])
        previous = best[1]
        previous_input = p

    return out, MatchReport(
        leg=leg,
        points=len(points),
        matched=len(points) - unmatched,
        max_offset_m=max(offsets) if offsets else 0.0,
        mean_offset_m=(sum(offsets) / len(offsets)) if offsets else 0.0,
        unmatched=unmatched,
    )
