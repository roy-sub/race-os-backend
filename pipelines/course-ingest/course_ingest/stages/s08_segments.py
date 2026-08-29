"""Stage 8 -- per-node gradient, per-segment climb, and named segments.

A segment is a contiguous run of similar gradient above a length threshold,
named from the OpenStreetMap way that carries most of it. Segments are the
solver's primary unit of work: it sets one power target per segment from the
segment's NET gradient, then integrates time over the node series inside it
(SOLVER_MODEL.md 4.2.1). So a segment boundary in the wrong place costs
accuracy, and a segment that is too short to hold a target is noise.

Two things this stage is careful not to do:

* It does not smooth the delivered elevation series. SOLVER_MODEL.md 1.2 forbids
  it outright, because smoothing flattens a climb's peak gradient and makes it
  cheaper than it is. The windowed gradient computed here is used ONLY to decide
  where a band changes; the emitted node series is exactly what the DEM gave.
* It does not store the segment's gradient as the source of truth. `net_gradient`
  is emitted for display and diffing, but it is reproducible from the node
  series, which is what the solver actually reads.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

from ..config import Config
from ..geo import Point, cumulative_m, elevation_gain
from .s02_route import WaySpan


class SegmentationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Segment:
    ordinal: int
    leg: str
    name: str
    from_km: float
    to_km: float
    net_gradient: float
    elevation_gain_m: float
    surface_quality: str
    name_source: str


@dataclass(frozen=True)
class SegmentationResult:
    segments: tuple[Segment, ...]
    node_gradients: tuple[float, ...]
    leg_surface_quality: str


def node_gradients(cum: Sequence[float], heights: Sequence[float]) -> list[float]:
    """Forward difference, unsmoothed -- the same definition the solver uses."""
    out: list[float] = []
    for j in range(len(heights) - 1):
        ds = cum[j + 1] - cum[j]
        out.append(0.0 if ds <= 0.0 else (heights[j + 1] - heights[j]) / ds)
    out.append(out[-1] if out else 0.0)
    return out


def _windowed_gradient(cum: Sequence[float], heights: Sequence[float], window_m: float) -> list[float]:
    """Gradient over a sliding window. Used only for band assignment."""
    n = len(heights)
    out = [0.0] * n
    lo = 0
    hi = 0
    for i in range(n):
        while cum[i] - cum[lo] > window_m / 2.0:
            lo += 1
        while hi + 1 < n and cum[hi + 1] - cum[i] <= window_m / 2.0:
            hi += 1
        ds = cum[hi] - cum[lo]
        out[i] = 0.0 if ds <= 0.0 else (heights[hi] - heights[lo]) / ds
    return out


def _band_index(bands: Sequence[dict], gradient: float) -> int:
    for i, band in enumerate(bands):
        if gradient < float(band["max"]):
            return i
    return len(bands) - 1


def _span_lookup(spans: Sequence[WaySpan], total_m: float):
    """Return `attributes_between(from_m, to_m) -> (name_by_m, surface_by_m)`."""
    if not spans or total_m <= 0.0:
        return lambda a, b: ({}, {})
    bounds = [(s.from_fraction * total_m, s.to_fraction * total_m, s) for s in spans]

    def between(a: float, b: float):
        names: dict[str, float] = defaultdict(float)
        surfaces: dict[str | None, float] = defaultdict(float)
        for s0, s1, span in bounds:
            lo = max(a, s0)
            hi = min(b, s1)
            if hi <= lo:
                continue
            if span.name:
                names[span.name] += hi - lo
            surfaces[span.surface] += hi - lo
        return dict(names), dict(surfaces)

    return between


def resolve_surface(surfaces: dict, cfg: Config) -> str:
    """Map Overture `road_surface` values onto the solver's `surface_quality`.

    An unmapped value fails loudly. SOLVER_MODEL.md I.2.2 turns this enum into
    Crr, and the gap between `typical_road` and `rough_chipseal` is worth about
    eight minutes over 180 km -- too much to default silently.
    """
    surface_map = cfg["course"]["surface_map"]
    absent_default = cfg["course"]["surface_absent_default"]
    tally: dict[str, float] = defaultdict(float)
    for value, metres in surfaces.items():
        if value is None:
            tally[absent_default] += metres
            continue
        if value not in surface_map:
            raise SegmentationError(
                f"unmapped road_surface value `{value}`. Add it to "
                f"config/course.yaml surface_map -- the pipeline will not guess a Crr."
            )
        tally[surface_map[value]] += metres
    if not tally:
        return absent_default
    return sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def segment_leg(
    nodes: Sequence[Point],
    heights: Sequence[float],
    spans: Sequence[WaySpan],
    leg: str,
    cfg: Config,
) -> SegmentationResult:
    seg_cfg = cfg["course"]["segmentation"]
    bands = seg_cfg["bands"]
    min_len = float(seg_cfg["min_segment_m"][leg])
    max_len = float(seg_cfg["max_segment_m"][leg])
    max_segments = int(seg_cfg["max_segments"][leg])
    naming = seg_cfg["naming"]

    cum = cumulative_m(nodes)
    total = cum[-1]
    raw_grad = node_gradients(cum, heights)
    band_grad = _windowed_gradient(cum, heights, float(seg_cfg["band_window_m"]))

    # 1. Contiguous runs of one band.
    runs: list[list[int]] = []
    current_band = _band_index(bands, band_grad[0])
    start = 0
    for i in range(1, len(nodes)):
        b = _band_index(bands, band_grad[i])
        if b != current_band:
            runs.append([start, i, current_band])
            start = i
            current_band = b
    runs.append([start, len(nodes) - 1, current_band])

    # 2. Absorb runs shorter than the threshold into the neighbour whose band is
    #    nearer in gradient; ties merge backwards, which keeps the pass stable.
    runs = _merge_short(runs, cum, min_len)

    # 3. Split any run longer than the maximum at even intervals. One power
    #    target across a whole 42 km leg is not a raceable plan.
    runs = _split_long(runs, cum, max_len)

    # 4. Cap the segment count by merging the adjacent pair whose net gradients
    #    differ least, repeatedly. Deterministic: ties break on position.
    while len(runs) > max_segments:
        best_i, best_d = 0, None
        for i in range(len(runs) - 1):
            g1 = _net_gradient(heights, cum, runs[i])
            g2 = _net_gradient(heights, cum, runs[i + 1])
            d = abs(g1 - g2)
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        runs[best_i] = [runs[best_i][0], runs[best_i + 1][1], runs[best_i][2]]
        del runs[best_i + 1]

    lookup = _span_lookup(spans, total)
    blocklist = [re.compile(p, re.IGNORECASE) for p in naming["name_blocklist_patterns"]]
    min_coverage = float(naming["min_name_coverage"])
    used_names: dict[str, int] = defaultdict(int)

    segments: list[Segment] = []
    leg_surface_tally: dict[str | None, float] = defaultdict(float)
    for ordinal, (i0, i1, band) in enumerate(runs, start=1):
        seg_len = cum[i1] - cum[i0]
        names, surfaces = lookup(cum[i0], cum[i1])
        for key, metres in surfaces.items():
            leg_surface_tally[key] += metres

        name, source = _choose_name(
            names, seg_len, blocklist, min_coverage, bands[band], ordinal, naming
        )
        count = used_names[name]
        used_names[name] += 1
        if count:
            name = naming["duplicate_suffix_template"].format(name=name, n=count + 1)

        # Per SOLVER_MODEL.md 1.1, a segment's `elevation_gain_m` is the plain
        # sum of positive node differences inside it -- the same quantity the
        # solver recomputes, so the two can never disagree.
        gain = elevation_gain(heights[i0 : i1 + 1], 0.0)
        segments.append(
            Segment(
                ordinal=ordinal,
                leg=leg,
                name=name,
                from_km=round(cum[i0] / 1000.0, 4),
                to_km=round(cum[i1] / 1000.0, 4),
                net_gradient=round(
                    0.0 if seg_len <= 0 else (heights[i1] - heights[i0]) / seg_len, 6
                ),
                elevation_gain_m=round(gain, 2),
                surface_quality=resolve_surface(surfaces, cfg),
                name_source=source,
            )
        )

    return SegmentationResult(
        segments=tuple(segments),
        node_gradients=tuple(raw_grad),
        leg_surface_quality=resolve_surface(dict(leg_surface_tally), cfg),
    )


def _net_gradient(heights: Sequence[float], cum: Sequence[float], run) -> float:
    i0, i1 = run[0], run[1]
    d = cum[i1] - cum[i0]
    return 0.0 if d <= 0 else (heights[i1] - heights[i0]) / d


def _split_long(runs: list[list[int]], cum: Sequence[float], max_len: float) -> list[list[int]]:
    out: list[list[int]] = []
    for i0, i1, band in runs:
        length = cum[i1] - cum[i0]
        if length <= max_len:
            out.append([i0, i1, band])
            continue
        pieces = int(length // max_len) + 1
        step = length / pieces
        cuts = [i0]
        for k in range(1, pieces):
            target = cum[i0] + step * k
            j = cuts[-1]
            while j < i1 and cum[j] < target:
                j += 1
            cuts.append(min(j, i1))
        cuts.append(i1)
        for a, b in zip(cuts, cuts[1:]):
            if b > a:
                out.append([a, b, band])
    return out


def _merge_short(runs: list[list[int]], cum: Sequence[float], min_len: float) -> list[list[int]]:
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for i, run in enumerate(runs):
            if cum[run[1]] - cum[run[0]] >= min_len:
                continue
            if i == 0:
                runs[1][0] = run[0]
                del runs[0]
            elif i == len(runs) - 1:
                runs[i - 1][1] = run[1]
                del runs[i]
            else:
                prev_gap = abs(runs[i - 1][2] - run[2])
                next_gap = abs(runs[i + 1][2] - run[2])
                if prev_gap <= next_gap:
                    runs[i - 1][1] = run[1]
                else:
                    runs[i + 1][0] = run[0]
                del runs[i]
            changed = True
            break
    return runs


def _choose_name(names, seg_len, blocklist, min_coverage, band, ordinal, naming):
    if seg_len > 0 and names:
        ranked = sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))
        for candidate, metres in ranked:
            if any(p.match(candidate) for p in blocklist):
                continue
            if metres / seg_len >= min_coverage:
                return naming["named_template"].format(name=candidate), naming["name_source_named"]
    return (
        naming["unnamed_template"].format(terrain_desc=band["terrain_desc"], ordinal=ordinal),
        naming["name_source_unnamed"],
    )
