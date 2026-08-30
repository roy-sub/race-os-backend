"""Stage 1 — Load the course (~0.2 s). ``SOLVER_MODEL.md`` §1.

Turns the pinned bundle into the derived geometry the later stages consume,
and **derives nothing that is not already implied by the delivered node
series.**

The consequential piece is the gradient histogram. Net gradient sets a
segment's *power target*, because the gravity term integrates to ``m·G·Δh``
regardless of path and because a target the athlete can hold has to describe
the segment as a whole. But **net gradient must never be used to compute the
segment's time.** Time is convex in gradient — a kilometre up at 4% and a
kilometre down at 4% take much longer than two kilometres at 0% — so by
Jensen's inequality, solving speed once at the net gradient is always too fast,
never too slow. Measured against a per-node solve:

===========================  ==========  ================  =======
Terrain (node-gradient SD)   Per-node    Net-gradient      Error
===========================  ==========  ================  =======
Near-flat coastal (0.010)    377.75 min  365.15 min        −3.34%
Gently rolling (0.020)       410.65 min  366.34 min        −10.79%
Rolling (0.035)              478.83 min  368.15 min        −23.11%
Mountainous (0.055)          586.12 min  370.59 min        −36.77%
===========================  ==========  ================  =======

Even a near-flat course is 3.3% fast, which is the whole error budget of §C.3
spent on a quadrature choice. On rolling terrain the model would be unusable.

So time is integrated over a **gradient histogram**: distance is exact in every
bin, no node is discarded and no gradient is averaged away. This is
**quadrature, not smoothing** — the distinction matters because §1.2 forbids
smoothing. A moving average would flatten a climb's peak gradient and make it
easier; binning preserves the exact distance-at-each-gradient distribution, so
a 12% pitch stays a 12% pitch and merely shares a bin with 11.9%.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

from raceos.domain.enums import Leg, SurfaceQuality
from raceos.solver.bind import clamp
from raceos.solver.errors import BundleIncomplete
from raceos.solver.models import Barrier, CourseBundleSnapshot, CourseLeg, ElevationNode
from raceos.solver.tables import physics as phys


@dataclass(frozen=True)
class SegmentGeometry:
    """Everything Stage 4 needs to cost one named segment."""

    ordinal: int
    leg: Leg
    name: str
    from_km: float
    to_km: float
    surface_quality: SurfaceQuality
    distance_m: float
    #: ``Σ max(0, h_{j+1} − h_j)`` over the delivered nodes — the raw
    #: definition, matching what the bundle's ``segments[].elevation_gain_m``
    #: carries so the two can never disagree.
    elevation_gain_m: float
    #: ``(h_end − h_start) / d_seg``. Sets the **power target**, never the time.
    net_gradient: float
    mean_elevation_m: float
    #: Mean bearing in radians, for the wind term. Derived from the leg
    #: geometry; a derived quantity, not new course data.
    bearing_rad: float
    #: ``{gradient_bin: distance_m}``. What time is integrated over.
    histogram: tuple[tuple[float, float], ...]
    #: True when any node in this segment hit the ±30% clamp. Propagates to
    #: the plan's provenance display (§1.3).
    terrain_quality_flag: bool
    #: How many of this segment's node pairs clamped, and how many there were.
    #: Summed per leg, because §1.3's rejection threshold is "more than 2% of a
    #: **leg's** nodes" — a per-segment test is far stricter and rejects real
    #: bundles that the specification accepts. Found by running the actual
    #: Tramuntana bundle, where one 323-node segment through a road cutting
    #: clamps 10 nodes (3.1% of that segment, 0.06% of the leg).
    clamped_nodes: int
    counted_nodes: int


@dataclass(frozen=True)
class LegGeometry:
    leg: Leg
    distance_m: float
    mean_elevation_m: float
    surface_quality: SurfaceQuality
    segments: tuple[SegmentGeometry, ...]


@dataclass(frozen=True)
class CourseGeometry:
    legs: dict[Leg, LegGeometry]
    barriers: tuple[Barrier, ...]

    def leg(self, which: Leg) -> LegGeometry:
        return self.legs[which]


def node_gradient(lower: ElevationNode, upper: ElevationNode) -> tuple[float, bool]:
    """Forward difference, with **no smoothing**. Returns (gradient, clamped).

    The ±30% clamp is not smoothing — it is a guard against a single bad
    terrain sample producing a 400% gradient that makes a segment unrideable.
    """
    run = upper.s_m - lower.s_m
    if run <= 0:
        return 0.0, False
    raw = (upper.h_m - lower.h_m) / run
    clamped = clamp(raw, -phys.NODE_GRADIENT_CLAMP, phys.NODE_GRADIENT_CLAMP)
    return clamped, clamped != raw


def bin_gradient(gradient: float) -> float:
    """Round to the nearest histogram bin centre.

    ``round`` here is banker's rounding in Python, which is deterministic and
    platform-independent — the property that matters. The bin width is a config
    value, not a literal.
    """
    width = phys.GRADIENT_BIN_WIDTH
    return round(gradient / width) * width


def _bearing_rad(index: int, total: int) -> float:
    """A stable mean bearing for a segment.

    The golden fixtures carry no lon/lat — they are ``(s_m, h_m)`` series — so
    a true geographic bearing is not derivable for them. Wind direction is
    optional in the forecast and absent in every golden case, in which case
    §I.2.1's direction-averaged form is used and the bearing is never read.

    When direction *is* supplied, a real bearing must come from the bundle's
    geometry; the adapter attaches it. This fallback distributes segments
    evenly around the compass so an out-and-back course does not accidentally
    model a permanent tailwind, which is the failure mode a constant would
    produce.
    """
    if total <= 0:  # pragma: no cover - defensive
        return 0.0
    return (2.0 * math.pi * index) / total


def build_segment_geometry(
    leg: CourseLeg,
    ordinal: int,
    name: str,
    from_km: float,
    to_km: float,
    surface_quality: SurfaceQuality,
    index: int,
    total: int,
    bearing_rad: float | None = None,
) -> SegmentGeometry:
    """Aggregate the nodes falling in ``[from_km, to_km)`` (§1.1)."""
    start_m = from_km * 1000.0
    end_m = to_km * 1000.0

    distance = 0.0
    gain = 0.0
    weighted_elevation = 0.0
    histogram: dict[float, float] = {}
    clamped_nodes = 0
    counted_nodes = 0
    h_start: float | None = None
    h_end: float | None = None

    for lower, upper in pairwise(leg.nodes):
        # A node pair belongs to this segment when its lower node does.
        if lower.s_m < start_m - 1e-9 or lower.s_m >= end_m - 1e-9:
            continue
        run = upper.s_m - lower.s_m
        if run <= 0:
            continue

        if h_start is None:
            h_start = lower.h_m
        h_end = upper.h_m

        distance += run
        gain += max(0.0, upper.h_m - lower.h_m)
        weighted_elevation += (lower.h_m + upper.h_m) / 2.0 * run

        gradient, was_clamped = node_gradient(lower, upper)
        counted_nodes += 1
        if was_clamped:
            clamped_nodes += 1

        key = bin_gradient(gradient)
        histogram[key] = histogram.get(key, 0.0) + run

    if distance <= 0 or h_start is None or h_end is None:
        raise BundleIncomplete(
            f"segment {name!r} on {leg.leg.value} spans no delivered nodes "
            f"({from_km}-{to_km} km); the elevation series and the segment "
            f"table disagree"
        )

    net_gradient = (h_end - h_start) / distance
    mean_elevation = weighted_elevation / distance

    # Sorted by bin so accumulation order is fixed (§0.4): floating-point
    # addition is not associative, so the order is specified rather than left
    # to dictionary iteration.
    ordered = tuple(sorted(histogram.items()))

    return SegmentGeometry(
        ordinal=ordinal,
        leg=leg.leg,
        name=name,
        from_km=from_km,
        to_km=to_km,
        surface_quality=surface_quality,
        distance_m=distance,
        elevation_gain_m=gain,
        net_gradient=net_gradient,
        mean_elevation_m=mean_elevation,
        bearing_rad=bearing_rad if bearing_rad is not None else _bearing_rad(index, total),
        histogram=ordered,
        terrain_quality_flag=clamped_nodes > 0,
        clamped_nodes=clamped_nodes,
        counted_nodes=counted_nodes,
    )


def load_course(bundle: CourseBundleSnapshot) -> CourseGeometry:
    """Stage 1. Raises :class:`BundleIncomplete` on any §1.2 violation."""
    # Elevation is terrain-sampled, never barometric — enforced at ingest,
    # asserted here.
    if bundle.elevation_source != "terrain":
        raise BundleIncomplete(
            f"bundle elevation_source is {bundle.elevation_source!r}; §1.2 "
            f"requires terrain-sampled elevation"
        )

    # Zero barriers is a data error, not a solvable plan.
    if not bundle.barriers:
        raise BundleIncomplete("bundle has zero barriers; §1.2 calls that a data error")

    # Barrier chronology: a bike cut-off before the swim exit is corrupt.
    limits = [b.limit_minutes_from_start for b in bundle.barriers]
    if limits != sorted(limits):
        raise BundleIncomplete(f"barriers are not monotonic in limit_minutes_from_start: {limits}")

    legs: dict[Leg, LegGeometry] = {}
    for leg in bundle.legs:
        specs = bundle.segments_for(leg.leg)
        segments: list[SegmentGeometry] = []
        for index, spec in enumerate(specs):
            segments.append(
                build_segment_geometry(
                    leg,
                    ordinal=spec.ordinal,
                    name=spec.name,
                    from_km=spec.from_km,
                    to_km=spec.to_km,
                    surface_quality=spec.surface_quality,
                    index=index,
                    total=len(specs),
                    bearing_rad=spec.bearing_rad,
                )
            )
        # §1.3: the rejection threshold is a fraction of the LEG's nodes.
        leg_clamped = sum(s.clamped_nodes for s in segments)
        leg_counted = sum(s.counted_nodes for s in segments)
        if leg_counted and leg_clamped / leg_counted > phys.NODE_CLAMP_FAIL_FRACTION:
            raise BundleIncomplete(
                f"{leg.leg.value} leg: {leg_clamped}/{leg_counted} nodes exceed the "
                f"±{phys.NODE_GRADIENT_CLAMP:.0%} gradient clamp "
                f"({leg_clamped / leg_counted:.1%} > "
                f"{phys.NODE_CLAMP_FAIL_FRACTION:.0%}); the elevation series is "
                f"not fit for purpose"
            )

        legs[leg.leg] = LegGeometry(
            leg=leg.leg,
            distance_m=leg.distance_m,
            mean_elevation_m=leg.mean_elevation_m,
            surface_quality=leg.surface_quality,
            # Accumulate in `ordinal` order (§0.4).
            segments=tuple(sorted(segments, key=lambda s: s.ordinal)),
        )

    return CourseGeometry(legs=legs, barriers=tuple(bundle.barriers))
