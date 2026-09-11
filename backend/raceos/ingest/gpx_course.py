"""Turn an athlete's uploaded route files into a loadable course bundle.

**This is the same shape of work the offline pipeline does, done online for one
athlete.** ``pipelines/course-ingest`` builds the official catalogue from
Overture road geometry; this module builds one athlete's own race from the
files they supply, and emits the *identical* bundle payload so that
:mod:`raceos.ingest.bundle_loader` validates and loads it by exactly the same
rules. A course an athlete added is not a second-class course: it is solved by
the same solver, against the same invariants, and rejected by the same
validator when it is wrong.

Six steps, each a function below:

1. **Parse.** Track points out of the GPX, in file order.
2. **Clean.** Drop repeated and implausible points.
3. **Resample.** Re-space to ~10 m nodes, so gradient is stable and comparable
   with every other course in the directory.
4. **Elevation.** Sample the DEM at each node — never the file's own ``<ele>``
   (see :mod:`raceos.ingest.elevation` for why).
5. **Segments.** Contiguous runs of similar gradient, named from the terrain.
6. **Furniture.** Aid stations, transitions, special needs, distance markers
   and the cut-off ladder, from the same documented rules the pipeline uses
   (``pipelines/course-ingest/config/furniture.yaml``), every item stamped
   ``ESTIMATED`` because nobody has verified this course but the athlete.

What this module will not do is guess. A leg whose distance is far from the
nominal distance for its format, a file with no track points, a route that
leaves DEM coverage — each is returned as a problem naming what is wrong, and
no bundle is written. A rejected upload costs the athlete a minute; a bundle
accepted with a fabricated gradient costs them their race plan.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from raceos.domain.enums import DistanceType, Leg
from raceos.ingest.elevation import ConstantElevation, ElevationSource
from raceos.logging import get_logger

logger = get_logger(__name__)

EARTH_RADIUS_M = 6_371_008.8

#: Node spacing, metres. Matches the pipeline exactly (Part 10.2 step 4) so a
#: submitted course and a generated one produce comparable gradients.
NODE_SPACING_M = 10.0

#: Hysteresis threshold for reported ascent, metres. A DEM sampled every 10 m
#: has a vertical noise floor, and an unfiltered sum credits all of it as
#: climbing.
GAIN_THRESHOLD_M = 3.0

#: Nominal leg distances, from the same table the pipeline uses.
NOMINAL_DISTANCES: dict[DistanceType, dict[Leg, float]] = {
    DistanceType.FULL: {Leg.SWIM: 3800.0, Leg.BIKE: 180_000.0, Leg.RUN: 42_195.0},
    DistanceType.HALF: {Leg.SWIM: 1900.0, Leg.BIKE: 90_000.0, Leg.RUN: 21_100.0},
    DistanceType.OLYMPIC: {Leg.SWIM: 1500.0, Leg.BIKE: 40_000.0, Leg.RUN: 10_000.0},
    DistanceType.SPRINT: {Leg.SWIM: 750.0, Leg.BIKE: 20_000.0, Leg.RUN: 5_000.0},
}

#: How far a submitted leg may deviate from nominal and still be accepted.
#:
#: Much wider than the pipeline's ±0.5%, and deliberately so: the pipeline
#: *routes* to a target and can correct itself, whereas an athlete is tracing a
#: real course from an athlete guide and will be a few hundred metres out. Too
#: tight a tolerance here rejects honest uploads; too loose accepts a 40 km
#: "marathon". ±8% refuses the wrong distance without arguing about a GPS
#: trace's last kilometre.
DISTANCE_TOLERANCE = 0.08

#: Gradient bands for segmentation, ascending. Transcribed from
#: ``pipelines/course-ingest/config/course.yaml`` so both paths name a hill the
#: same way.
GRADIENT_BANDS: tuple[tuple[float, str], ...] = (
    (-0.045, "Steep descent"),
    (-0.015, "Descent"),
    (-0.004, "False flat down"),
    (0.004, "Flat"),
    (0.015, "False flat up"),
    (0.045, "Climb"),
    (float("inf"), "Steep climb"),
)

#: Shortest run of road that earns its own named segment, metres. Below this a
#: segment is noise and is merged into its neighbour.
MIN_SEGMENT_M = 800.0

#: Reference cut-off ladders, minutes from the athlete's start. Identical to
#: ``furniture.yaml``; the seeded courses and a submitted one therefore agree
#: on the shape of a cut-off.
REFERENCE_CUTOFFS: dict[DistanceType, dict[str, float]] = {
    DistanceType.FULL: {"swim_exit": 140.0, "bike_cutoff": 630.0, "finish": 960.0},
    DistanceType.HALF: {"swim_exit": 70.0, "bike_cutoff": 330.0, "finish": 510.0},
    DistanceType.OLYMPIC: {"swim_exit": 50.0, "bike_cutoff": 170.0, "finish": 240.0},
    DistanceType.SPRINT: {"swim_exit": 30.0, "bike_cutoff": 95.0, "finish": 130.0},
}

AID_SPACING: dict[DistanceType, dict[Leg, tuple[float, float]]] = {
    DistanceType.FULL: {Leg.BIKE: (20.0, 25.0), Leg.RUN: (2.1, 2.1)},
    DistanceType.HALF: {Leg.BIKE: (20.0, 22.5), Leg.RUN: (2.1, 2.1)},
    DistanceType.OLYMPIC: {Leg.BIKE: (20.0, 20.0), Leg.RUN: (2.5, 2.5)},
    DistanceType.SPRINT: {Leg.BIKE: (10.0, 10.0), Leg.RUN: (2.5, 2.5)},
}
AID_MIN_TAIL_KM = 1.0
AID_FULL_SERVICE_EVERY: dict[Leg, int] = {Leg.BIKE: 2, Leg.RUN: 4}
AID_CONTENTS: dict[Leg, dict[str, list[str]]] = {
    Leg.BIKE: {
        "standard": ["water", "sports_drink", "banana", "energy_bar"],
        "full_service": [
            "water",
            "sports_drink",
            "energy_gel",
            "banana",
            "energy_bar",
            "salt_tablets",
            "toilets",
            "mechanical_support",
        ],
    },
    Leg.RUN: {
        "standard": ["water", "sports_drink", "cola", "energy_gel"],
        "full_service": [
            "water",
            "sports_drink",
            "cola",
            "energy_gel",
            "salt_tablets",
            "fruit",
            "ice",
            "sponges",
            "toilets",
            "medical",
        ],
    },
}
DISTANCE_MARKER_STEP_KM: dict[Leg, dict[DistanceType, float]] = {
    Leg.SWIM: {
        DistanceType.FULL: 0.95,
        DistanceType.HALF: 0.95,
        DistanceType.OLYMPIC: 0.75,
        DistanceType.SPRINT: 0.375,
    },
    Leg.BIKE: {
        DistanceType.FULL: 10.0,
        DistanceType.HALF: 10.0,
        DistanceType.OLYMPIC: 5.0,
        DistanceType.SPRINT: 5.0,
    },
    Leg.RUN: {
        DistanceType.FULL: 1.0,
        DistanceType.HALF: 1.0,
        DistanceType.OLYMPIC: 1.0,
        DistanceType.SPRINT: 1.0,
    },
}
SPECIAL_NEEDS_LEGS: dict[DistanceType, tuple[Leg, ...]] = {
    DistanceType.FULL: (Leg.BIKE, Leg.RUN),
    DistanceType.HALF: (Leg.BIKE,),
    DistanceType.OLYMPIC: (),
    DistanceType.SPRINT: (),
}

LEG_ORDER: tuple[Leg, ...] = (Leg.SWIM, Leg.BIKE, Leg.RUN)


class GpxError(ValueError):
    """A submitted file could not be turned into a leg. Names what is wrong."""


# ---------------------------------------------------------------------------
# 1-3. Parse, clean, resample
# ---------------------------------------------------------------------------


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle metres between two ``(lng, lat)`` points."""
    lng1, lat1 = math.radians(a[0]), math.radians(a[1])
    lng2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def parse_gpx(data: bytes, *, filename: str = "route.gpx") -> list[tuple[float, float]]:
    """Every track point in the file, in order, as ``(lng, lat)``.

    Track points first, then route points: a ``<trk>`` is a recorded or traced
    line and a ``<rte>`` is a set of instructions, so where a file has both the
    track is the geometry. Waypoints are ignored — a scatter of unordered
    points is not a route, and connecting them in document order would draw a
    line nobody rides.
    """
    import gpxpy

    try:
        parsed = gpxpy.parse(data.decode("utf-8", errors="replace"))
    except Exception as exc:  # gpxpy raises several unrelated types
        raise GpxError(
            f"{filename} could not be read as GPX. Export the route again from "
            f"whatever drew it, and check the file is not a .fit or .tcx "
            f"renamed."
        ) from exc

    points: list[tuple[float, float]] = []
    for track in parsed.tracks:
        for segment in track.segments:
            points.extend((float(p.longitude), float(p.latitude)) for p in segment.points)
    if not points:
        for route in parsed.routes:
            points.extend((float(p.longitude), float(p.latitude)) for p in route.points)

    if len(points) < 2:
        raise GpxError(
            f"{filename} contains no route. It has fewer than two track points, "
            f"so there is no line to follow."
        )
    return points


def clean(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop repeats and single-point jumps.

    A consumer GPS parked at a start line emits hundreds of identical points,
    and loses fix under a bridge and re-acquires a kilometre away. Both are
    artefacts of the recording rather than of the course, and both would
    survive resampling as real geometry.
    """
    out: list[tuple[float, float]] = [points[0]]
    for point in points[1:]:
        step = haversine_m(out[-1], point)
        if step < 0.5:
            continue
        # 400 m between consecutive samples is a lost fix, not a road.
        if step > 400.0 and len(out) > 1:
            logger.info("dropped a GPS jump", extra={"jump_m": round(step)})
            continue
        out.append(point)
    if len(out) < 2:
        raise GpxError(
            "Every point in this file is in the same place. There is no route to follow."
        )
    return out


def resample(
    points: Sequence[tuple[float, float]], spacing_m: float = NODE_SPACING_M
) -> list[tuple[float, float]]:
    """Re-space the line to even nodes, keeping the first and last exactly.

    Even spacing is what makes gradient comparable between courses: an
    unresampled trace has nodes wherever the recording device happened to emit
    one, so the same hill reads steeper on a trace recorded every second than
    on one recorded every ten.
    """
    out: list[tuple[float, float]] = [points[0]]
    carry = 0.0
    for a, b in pairwise(points):
        span = haversine_m(a, b)
        if span <= 0:
            continue
        travelled = spacing_m - carry
        while travelled <= span:
            t = travelled / span
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            travelled += spacing_m
        carry = (carry + span) % spacing_m
    if out[-1] != tuple(points[-1]):
        out.append(tuple(points[-1]))
    return out


def path_length_m(points: Sequence[tuple[float, float]]) -> float:
    return sum(haversine_m(a, b) for a, b in pairwise(points))


# ---------------------------------------------------------------------------
# 4. Elevation, and the gain figure derived from it
# ---------------------------------------------------------------------------


def filtered_gain_m(heights: Sequence[float], threshold_m: float = GAIN_THRESHOLD_M) -> float:
    """Hysteresis-filtered ascent: the figure a screen shows.

    A rise counts once it clears *threshold* above the last reversal. The
    solver does not read this — it recomputes from the node series — so the two
    differ by design and do not disagree: one is surveyed ascent, the other is
    every centimetre of the model.
    """
    if not heights:
        return 0.0
    gain = 0.0
    #: The last height a rise has been credited from, and the running high
    #: since it. A rise is credited once it clears *threshold* above the
    #: reference; a fall resets the reference only once it clears the same
    #: amount below the high, which is what stops sampling noise being
    #: counted as a hill in either direction.
    reference = heights[0]
    high = heights[0]
    for height in heights[1:]:
        if height >= high:
            high = height
            if high - reference >= threshold_m:
                gain += high - reference
                reference = high
        elif high - height >= threshold_m:
            reference = height
            high = height
    return gain


# ---------------------------------------------------------------------------
# The leg
# ---------------------------------------------------------------------------


@dataclass
class BuiltLeg:
    leg: Leg
    #: ``(lng, lat, elevation_m)`` at ~10 m spacing.
    nodes: list[tuple[float, float, float]]
    distance_m: float
    elevation_gain_m: float

    @property
    def distance_km(self) -> float:
        return self.distance_m / 1000.0

    def ewkt(self) -> str:
        """``SRID=4326;LINESTRING Z (...)`` — what PostGIS takes directly."""
        body = ", ".join(f"{lng:.6f} {lat:.6f} {ele:.2f}" for lng, lat, ele in self.nodes)
        return f"SRID=4326;LINESTRING Z ({body})"


def build_leg(
    leg: Leg,
    data: bytes,
    *,
    filename: str,
    elevation: ElevationSource,
) -> BuiltLeg:
    """One uploaded file to one resampled, DEM-sampled leg."""
    points = resample(clean(parse_gpx(data, filename=filename)))
    # A level water surface: sampling a DEM across a swim would inject
    # gradient into a leg that has none, and the solver reads gradient
    # straight off the node series.
    source = ConstantElevation(0.0) if leg is Leg.SWIM else elevation
    heights = source.sample(points)
    nodes = [(lng, lat, height) for (lng, lat), height in zip(points, heights, strict=True)]
    return BuiltLeg(
        leg=leg,
        nodes=nodes,
        distance_m=path_length_m(points),
        elevation_gain_m=0.0 if leg is Leg.SWIM else filtered_gain_m(heights),
    )


def check_distance(leg: BuiltLeg, distance_type: DistanceType) -> str | None:
    """The one check worth blocking on. Returns a problem, or ``None``."""
    nominal = NOMINAL_DISTANCES[distance_type][leg.leg]
    deviation = abs(leg.distance_m - nominal) / nominal
    if deviation <= DISTANCE_TOLERANCE:
        return None
    return (
        f"The {leg.leg.value.lower()} file measures {leg.distance_km:.2f} km, but "
        f"a {distance_type.value} {leg.leg.value.lower()} is {nominal / 1000:.2f} km "
        f"— that is {deviation * 100:.0f}% out. Check you have uploaded the right "
        f"file for this leg, and that it covers every lap."
    )


# ---------------------------------------------------------------------------
# 5. Segments
# ---------------------------------------------------------------------------


def _band(gradient: float) -> str:
    for limit, name in GRADIENT_BANDS:
        if gradient < limit:
            return name
    return GRADIENT_BANDS[-1][1]  # pragma: no cover - inf guards this


def build_segments(leg: BuiltLeg, start_ordinal: int) -> list[dict[str, Any]]:
    """Contiguous runs of similar gradient, tiling the leg with no gaps.

    Tiling matters more than the naming does: a gap between two segments is a
    stretch of road the plan never paces, and it would not look like an error
    anywhere — the numbers either side would still be right.
    """
    if len(leg.nodes) < 2:
        return []

    # Gradient over a 100 m window rather than node to node: at 10 m spacing a
    # 30 m DEM's own noise dominates the per-node slope.
    window = max(1, int(100.0 / NODE_SPACING_M))
    gradients: list[float] = []
    for index in range(len(leg.nodes) - 1):
        far = min(index + window, len(leg.nodes) - 1)
        run = sum(haversine_m(leg.nodes[i][:2], leg.nodes[i + 1][:2]) for i in range(index, far))
        rise = leg.nodes[far][2] - leg.nodes[index][2]
        gradients.append(rise / run if run > 0 else 0.0)

    runs: list[tuple[str, int, int]] = []
    current = _band(gradients[0])
    start = 0
    for index, gradient in enumerate(gradients[1:], start=1):
        band = _band(gradient)
        if band != current:
            runs.append((current, start, index))
            current, start = band, index
    runs.append((current, start, len(gradients)))

    # Merge anything too short into the run before it, so the leg stays tiled.
    merged: list[tuple[str, int, int]] = []
    for band, start, end in runs:
        length = sum(haversine_m(leg.nodes[i][:2], leg.nodes[i + 1][:2]) for i in range(start, end))
        if merged and length < MIN_SEGMENT_M:
            previous = merged[-1]
            merged[-1] = (previous[0], previous[1], end)
        else:
            merged.append((band, start, end))

    cumulative = [0.0]
    for a, b in pairwise(leg.nodes):
        cumulative.append(cumulative[-1] + haversine_m(a[:2], b[:2]))

    segments: list[dict[str, Any]] = []
    for offset, (band, start, end) in enumerate(merged):
        from_km = cumulative[start] / 1000.0
        to_km = cumulative[min(end, len(cumulative) - 1)] / 1000.0
        heights = [node[2] for node in leg.nodes[start : end + 1]]
        run_m = (to_km - from_km) * 1000.0
        segments.append(
            {
                "ordinal": start_ordinal + offset,
                "leg": leg.leg.value,
                # Named from the terrain, never invented: there is no road name
                # in a GPX file, and making one up would put a fictional place
                # on a race card.
                "name": f"{band} · km {from_km:.1f}-{to_km:.1f}",
                "name_source": "DERIVED_TERRAIN",
                "from_km": round(from_km, 4),
                "to_km": round(to_km, 4),
                "elevation_gain_m": round(filtered_gain_m(heights), 2),
                "net_gradient": round((heights[-1] - heights[0]) / run_m if run_m > 0 else 0.0, 5),
                "surface_quality": "typical_road",
            }
        )
    # The last segment must land exactly on the leg's end, or the tiling check
    # in the loader fails on a rounding difference.
    if segments:
        segments[-1]["to_km"] = round(leg.distance_km, 4)
    return segments


# ---------------------------------------------------------------------------
# 6. Furniture
# ---------------------------------------------------------------------------


def build_aid_stations(leg_km: dict[Leg, float], distance_type: DistanceType) -> list[dict]:
    out: list[dict] = []
    for leg in (Leg.BIKE, Leg.RUN):
        rule = AID_SPACING[distance_type].get(leg)
        if rule is None:
            continue
        first, spacing = rule
        every = AID_FULL_SERVICE_EVERY[leg]
        km, index = first, 0
        while km <= leg_km[leg] - AID_MIN_TAIL_KM:
            index += 1
            kind = "full_service" if index % every == 0 else "standard"
            out.append(
                {
                    "leg": leg.value,
                    "name": f"{leg.value.title()} aid {index}",
                    "km": round(km, 3),
                    "contents": list(AID_CONTENTS[leg][kind]),
                    "provenance": "ESTIMATED",
                }
            )
            km += spacing
    return out


def build_waypoints(
    leg_km: dict[Leg, float],
    distance_type: DistanceType,
    aid_stations: Sequence[dict],
) -> list[dict]:
    out: list[dict] = [
        {
            "type": "transition",
            "leg": Leg.BIKE.value,
            "name": "T1",
            "km": 0.0,
            "provenance": "ESTIMATED",
        },
        {
            "type": "transition",
            "leg": Leg.RUN.value,
            "name": "T2",
            "km": 0.0,
            "provenance": "ESTIMATED",
        },
    ]
    for leg in SPECIAL_NEEDS_LEGS[distance_type]:
        target = leg_km[leg] * 0.5
        # Snapped to a real stopping point where there is one within 4 km: a
        # bag an athlete has to stop for in the middle of nowhere is a bag they
        # ride past.
        near = [
            station
            for station in aid_stations
            if station["leg"] == leg.value and abs(station["km"] - target) <= 4.0
        ]
        km = (
            min(near, key=lambda s: (abs(s["km"] - target), s["km"]))["km"]
            if near
            else round(target, 3)
        )
        out.append(
            {
                "type": "special_needs",
                "leg": leg.value,
                "name": f"{leg.value.title()} special needs",
                "km": km,
                "provenance": "ESTIMATED",
            }
        )
    for leg in LEG_ORDER:
        step = DISTANCE_MARKER_STEP_KM[leg][distance_type]
        km = step
        while km < leg_km[leg] - 1e-6:
            out.append(
                {
                    "type": "distance_marker",
                    "leg": leg.value,
                    "name": f"{km:g} km",
                    "km": round(km, 3),
                    "provenance": "ESTIMATED",
                }
            )
            km += step
    return out


def build_barriers(
    leg_km: dict[Leg, float],
    distance_type: DistanceType,
    generosity: float = 1.0,
) -> tuple[list[dict], dict[str, float]]:
    """The cut-off ladder, minutes from the athlete's start.

    Monotonic by construction and scaled from one reference ladder, because a
    course made harder by hand-editing four barriers is a course whose cut-offs
    may not be in chronological order — and an out-of-order ladder is not a
    solvable plan.
    """
    reference = REFERENCE_CUTOFFS[distance_type]
    swim_exit = reference["swim_exit"] * generosity
    bike_cutoff = reference["bike_cutoff"] * generosity
    finish = reference["finish"] * generosity

    barriers: list[dict] = [
        {
            "name": "swim_exit",
            "leg": Leg.SWIM.value,
            "limit_minutes_from_start": round(swim_exit),
            "km": round(leg_km[Leg.SWIM], 3),
        }
    ]
    if distance_type in (DistanceType.FULL, DistanceType.HALF):
        km = leg_km[Leg.BIKE] * 0.6667
        barriers.append(
            {
                "name": f"bike_km_{km:.0f}",
                "leg": Leg.BIKE.value,
                "limit_minutes_from_start": round(swim_exit + 0.7551 * (bike_cutoff - swim_exit)),
                "km": round(km, 3),
            }
        )
    barriers.append(
        {
            "name": "bike_cutoff",
            "leg": Leg.BIKE.value,
            "limit_minutes_from_start": round(bike_cutoff),
            "km": round(leg_km[Leg.BIKE], 3),
        }
    )
    barriers.append(
        {
            "name": "finish",
            "leg": Leg.RUN.value,
            "limit_minutes_from_start": round(finish),
            "km": round(leg_km[Leg.RUN], 3),
        }
    )
    ratios = {
        "generosity": generosity,
        "swim_exit_min": float(barriers[0]["limit_minutes_from_start"]),
        "bike_cutoff_min": round(bike_cutoff),
        "finish_min": round(finish),
        "reference_swim_exit_min": reference["swim_exit"],
        "reference_bike_cutoff_min": reference["bike_cutoff"],
        "reference_finish_min": reference["finish"],
    }
    return barriers, ratios


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------


def _display_series(leg: BuiltLeg, max_points: int = 400) -> dict[str, list[float]]:
    """A downsample for charting only. Never solve against it."""
    cumulative = [0.0]
    for a, b in pairwise(leg.nodes):
        cumulative.append(cumulative[-1] + haversine_m(a[:2], b[:2]))
    step = max(1, len(leg.nodes) // max_points)
    indices = list(range(0, len(leg.nodes), step))
    if indices[-1] != len(leg.nodes) - 1:
        indices.append(len(leg.nodes) - 1)
    return {
        "s_km": [round(cumulative[i] / 1000.0, 4) for i in indices],
        "h_m": [round(leg.nodes[i][2], 2) for i in indices],
    }


@dataclass
class BuildRequest:
    """Everything needed to turn three files into a course."""

    slug: str
    name: str
    place: str
    timezone: str
    distance_type: DistanceType
    lat: float
    lng: float
    season_year: int
    version: str = "v1"
    tone_color: str = "#3E352B"
    cutoff_generosity: float = 1.0
    changelog: str = "Added by an athlete from their own route files."


@dataclass
class BuildResult:
    payload: dict[str, Any]
    problems: list[str] = field(default_factory=list)
    legs: dict[Leg, BuiltLeg] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


def build_bundle(
    request: BuildRequest,
    files: dict[Leg, tuple[str, bytes]],
    *,
    elevation: ElevationSource,
) -> BuildResult:
    """Three uploaded files to one bundle payload the loader will take.

    Every problem found is collected rather than raised on the first, so an
    athlete who uploaded two wrong files is told about both instead of
    discovering the second after fixing the first.
    """
    problems: list[str] = []
    legs: dict[Leg, BuiltLeg] = {}

    for leg in LEG_ORDER:
        entry = files.get(leg)
        if entry is None:
            problems.append(f"No {leg.value.lower()} route file was uploaded.")
            continue
        filename, data = entry
        try:
            built = build_leg(leg, data, filename=filename, elevation=elevation)
        except Exception as exc:  # reported to the athlete, never raised at them
            problems.append(str(exc) if str(exc) else f"{filename} could not be processed.")
            continue
        problem = check_distance(built, request.distance_type)
        if problem:
            problems.append(problem)
        legs[leg] = built

    if problems:
        return BuildResult(payload={}, problems=problems, legs=legs)

    leg_km = {leg: built.distance_km for leg, built in legs.items()}
    aid_stations = build_aid_stations(leg_km, request.distance_type)
    waypoints = build_waypoints(leg_km, request.distance_type, aid_stations)
    barriers, ratios = build_barriers(leg_km, request.distance_type, request.cutoff_generosity)

    segments: list[dict[str, Any]] = []
    for leg in (Leg.BIKE, Leg.RUN):
        segments.extend(build_segments(legs[leg], start_ordinal=len(segments) + 1))

    total_gain = round(sum(built.elevation_gain_m for built in legs.values()))

    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_by": "raceos-gpx-ingest",
        "course": {
            "slug": request.slug,
            "name": request.name,
            "place": request.place,
            "distance_type": request.distance_type.value,
            # An athlete-supplied course has not been reviewed by anyone, so it
            # arrives at the least alarming rung and the provenance column on
            # every screen says ESTIMATED.
            "difficulty": _difficulty_for(total_gain, request.distance_type),
            "elevation_gain_m": total_gain,
            "timezone": request.timezone,
            "lat": request.lat,
            "lng": request.lng,
            "tone_color": request.tone_color,
            "is_fictional": False,
            "media_hero_path": None,
            "media_card_path": None,
        },
        "course_bundle": {
            "course_id": request.slug,
            "version": request.version,
            "status": "published",
            "provenance": "ESTIMATED",
            "season_year": request.season_year,
            "verified_at": None,
            "published_at": datetime.now(UTC).isoformat(),
            "elevation_source": "terrain",
            "attribution": (f"Route supplied by the athlete · Elevation: {elevation.attribution}"),
            "changelog": request.changelog,
            "plans_affected_count": 0,
            "bundle_asset_key": None,
            "terrain_pmtiles_key": None,
            "elevation_profile": {
                "authority": "course_bundle_legs.geometry (Z ordinate)",
                "note": (
                    "`display` is a downsample for charting only; never solve "
                    "against it. `gain_m` is hysteresis-filtered surveyed "
                    "ascent, not the node-series sum."
                ),
                "legs": {
                    leg.value: {
                        "gain_m": round(built.elevation_gain_m, 2),
                        "display": _display_series(built),
                    }
                    for leg, built in legs.items()
                },
            },
            "barriers": barriers,
            "aid_stations": aid_stations,
            "waypoints": waypoints,
            "segments": segments,
        },
        "course_bundle_legs": [
            {
                "leg": leg.value,
                "geometry": legs[leg].ewkt(),
                "distance_m": round(legs[leg].distance_m, 2),
                "elevation_gain_m": round(legs[leg].elevation_gain_m),
                "node_count": len(legs[leg].nodes),
                "surface_quality": "typical_road",
            }
            for leg in LEG_ORDER
        ],
        "provenance_detail": {
            "road_source": "athlete_upload:gpx",
            "dem_source": elevation.attribution,
            "node_spacing_m": NODE_SPACING_M,
            "gain_threshold_m": GAIN_THRESHOLD_M,
            "cutoff_ratios": ratios,
            "note": (
                "Submitted by an athlete from their own route files. The route "
                "is theirs; the elevation is terrain-sampled and the cut-off "
                "ladder is the reference ladder for this distance, scaled. "
                "Nothing here is an organiser's published course data."
            ),
        },
    }
    return BuildResult(payload=payload, problems=[], legs=legs)


def _difficulty_for(total_gain_m: float, distance_type: DistanceType) -> str:
    """A difficulty band from the climbing, so the directory can sort.

    Bands are metres of ascent per kilometre of racing, which is the only
    comparison that works across distances — 1,200 m over a Full is a rolling
    day and over a Sprint it is not possible.
    """
    nominal = NOMINAL_DISTANCES[distance_type]
    total_km = sum(nominal.values()) / 1000.0
    per_km = total_gain_m / total_km if total_km else 0.0
    if per_km < 5.0:
        return "APPROACHABLE"
    if per_km < 10.0:
        return "MODERATE"
    if per_km < 16.0:
        return "HARD"
    return "BRUTAL"
