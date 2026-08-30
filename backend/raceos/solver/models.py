"""``SolveInput`` and ``SolveOutput``. Build Spec Part 5.1, with §F applied.

Everything is frozen. The solver is a pure function of these two types: it
takes a ``SolveInput`` and returns a ``SolveOutput``, and everything it needs
is passed in. No database, no network, no wall clock.

Four §F contract changes are applied here:

* ``BikeSetup`` on :class:`AthleteSnapshot` (§F.2)
* ``pressure_hpa`` and ``cloud_cover_pct`` on :class:`ForecastSnapshot` (§F.3)
* ``measured_at_temp_c`` on :class:`ConstraintValue` (§F.4)
* :class:`Infeasibility` reports the **earliest missed** barrier and gains two
  diagnostic fields (§F.5); :class:`SolveOutput` gains ``assumed_fields`` (§F.6)

``schema_version`` is 2 as a result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time

from raceos.domain.enums import (
    AthleteLevel,
    BagKey,
    BikePosition,
    ConstraintSource,
    Feasibility,
    HelmetType,
    Leg,
    MarginState,
    RiskLevel,
    SolverDistance,
    SurfaceQuality,
)

#: Bumped 1 -> 2 by the four §F input changes. The snapshot is part of
#: `solve_input_hash`, so adding them changes the hash for every plan.
SCHEMA_VERSION: int = 2


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BikeSetup:
    """§F.2. Equipment facts, not measured physiology.

    They have no ``source``, no staleness window and no calibration path,
    which is why they live on the athlete rather than among the constraints.
    """

    position: BikePosition
    helmet: HelmetType


@dataclass(frozen=True)
class ConstraintValue:
    """One athlete constraint, with its provenance.

    **No branch anywhere in this model reads ``source``.** An ``estimated``
    value is used with exactly the numeric weight of a ``measured`` one — no
    down-weighting, no widened interval, no shrinkage toward a population mean
    (§0.6). Provenance travels through the solver untouched into
    ``plan_constraint_refs.source_label`` and is a presentation concern only.

    A CI test enforces this by permuting every ``source`` in a golden input and
    requiring byte-identical numeric output.
    """

    key: str
    value: float
    unit: str
    source: ConstraintSource
    #: §F.4. Currently meaningful for ``sweat_rate`` only. ``None`` means the
    #: solver assumes 15 °C WBGT and says so in ``assumed_fields``.
    measured_at_temp_c: float | None = None


@dataclass(frozen=True)
class AthleteSnapshot:
    level: AthleteLevel
    constraints: tuple[ConstraintValue, ...]
    #: ``None`` -> the solver assumes ``road_clipons`` + ``standard`` and emits
    #: ``athlete.bike_setup`` in ``assumed_fields``.
    bike_setup: BikeSetup | None = None

    def constraint(self, key: str) -> ConstraintValue | None:
        for entry in self.constraints:
            if entry.key == key:
                return entry
        return None


@dataclass(frozen=True)
class ElevationNode:
    """One delivered node: cumulative distance and terrain-sampled elevation."""

    s_m: float
    h_m: float


@dataclass(frozen=True)
class CourseSegment:
    """A named segment. The solver's primary unit of work (§1.1, §4.2.1)."""

    ordinal: int
    leg: Leg
    name: str
    from_km: float
    to_km: float
    surface_quality: SurfaceQuality
    #: Mean compass bearing in radians, when the source geometry carries
    #: coordinates. Golden fixtures are `(s_m, h_m)` series with no lon/lat, so
    #: theirs is ``None`` — which costs nothing, because wind direction is
    #: optional and absent in every golden case, and §I.2.1's
    #: direction-averaged form never reads a bearing.
    bearing_rad: float | None = None


@dataclass(frozen=True)
class Barrier:
    name: str
    leg: Leg
    km: float
    limit_minutes_from_start: float


@dataclass(frozen=True)
class AidStation:
    leg: Leg
    name: str
    km: float
    contents: tuple[str, ...]


@dataclass(frozen=True)
class CourseLeg:
    leg: Leg
    distance_m: float
    #: The delivered node series, used **exactly as delivered**. §1.2 forbids
    #: smoothing: no moving average, no Savitzky-Golay, no spline, no gradient
    #: clipping. A model that smoothed would systematically under-predict
    #: climbing time, which is the failure mode that invariant prevents.
    nodes: tuple[ElevationNode, ...]
    surface_quality: SurfaceQuality
    mean_elevation_m: float


@dataclass(frozen=True)
class CourseBundleSnapshot:
    """The pinned course. One shape, whatever produced it.

    Two very different artefacts normalise into this: the pipeline's generated
    bundles (EWKT ``LINESTRING Z``, product distance vocabulary) and the
    synthetic golden fixtures (``[[s_m, h_m]]`` arrays, solver vocabulary).
    Adapters live in :mod:`raceos.solver.adapters`; the solver itself sees only
    this.
    """

    course_id: str
    distance: SolverDistance
    legs: tuple[CourseLeg, ...]
    segments: tuple[CourseSegment, ...]
    barriers: tuple[Barrier, ...]
    aid_stations: tuple[AidStation, ...]
    elevation_source: str = "terrain"

    def leg(self, which: Leg) -> CourseLeg:
        for entry in self.legs:
            if entry.leg is which:
                return entry
        raise KeyError(f"bundle has no {which.value} leg")

    def segments_for(self, which: Leg) -> tuple[CourseSegment, ...]:
        return tuple(s for s in self.segments if s.leg is which)


@dataclass(frozen=True)
class GoalSpec:
    goal_minutes: float | None = None
    risk: RiskLevel = RiskLevel.BALANCED
    first_timer: bool = False


@dataclass(frozen=True)
class ForecastSnapshot:
    temp_c: float
    humidity: float
    wind_speed_ms: float
    conditions: str
    water_temp_c: float
    #: Direction is optional. When absent, §I.2.1's closed-form
    #: direction-averaged form is used — and wind of unknown direction always
    #: *costs* time, because drag is quadratic. A model that set unknown wind
    #: to zero would systematically under-predict every windy race.
    wind_dir_deg: float | None = None
    #: §F.3. Sea-level (QNH) pressure. ``None`` -> ISA standard, declared in
    #: ``assumed_fields``. Treated as absent outside [870, 1085].
    pressure_hpa: float | None = None
    #: §F.3. ``None`` -> the categorical mapping from ``conditions``, declared
    #: in ``assumed_fields``.
    cloud_cover_pct: float | None = None


@dataclass(frozen=True)
class EventSpec:
    event_date: date
    start_time_local: time
    timezone: str
    lat: float
    lng: float
    #: The UTC offset **in effect on the event date**, including summer time.
    #: Getting this or the longitude sign wrong silently returns sunrise
    #: instead of sunset (§I.1.7).
    utc_offset_hours: float


@dataclass(frozen=True)
class SolveOptions:
    #: Requires the calling service to have written an ``override_events`` row
    #: first (§5.1).
    carb_override: float | None = None
    night_flag: bool = False
    preview_only: bool = False


@dataclass(frozen=True)
class SolveInput:
    schema_version: int
    athlete: AthleteSnapshot
    course: CourseBundleSnapshot
    goal: GoalSpec
    forecast: ForecastSnapshot
    event: EventSpec
    options: SolveOptions


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    leg: Leg
    distance: float
    target_pace_or_power: str
    unit: str
    split_minutes: float
    note: str


@dataclass(frozen=True)
class Segment:
    ordinal: int
    leg: Leg
    name: str
    from_km: float
    to_km: float
    terrain_desc: str
    target_watts: int | None
    target_pace_sec_per_km: int | None
    target_minutes: float
    note: str


@dataclass(frozen=True)
class Gate:
    name: str
    leg: Leg
    limit_minutes: float
    eta_minutes: float
    margin_minutes: float
    load_pct: float
    state: MarginState


@dataclass(frozen=True)
class Fuelling:
    carb_g_per_hr: int
    fluid_ml_per_hr: int
    sodium_mg_per_hr: int
    caffeine_mg_total: int
    total_carb_g: int
    overridden: bool
    requires_multiple_transportable: bool
    binding_carb_key: str
    binding_fluid_key: str
    binding_sodium_key: str
    binding_caffeine_key: str


@dataclass(frozen=True)
class AidAction:
    ordinal: int
    leg: Leg
    at_clock_minutes: float
    at_km: float
    station_name: str
    action_text: str
    cumulative_carb_g: float


@dataclass(frozen=True)
class BagItem:
    name: str
    qty: str | None
    note: str | None
    #: Mandatory. An item with no upstream justification cannot be emitted —
    #: asserted as a stage postcondition, not left as a convention (§6.1).
    reason_constraint_key: str
    reason_text: str


@dataclass(frozen=True)
class Bag:
    key: BagKey
    name: str
    when_label: str
    items: tuple[BagItem, ...]

    @property
    def item_count(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class ConstraintRef:
    key: str
    name: str
    value: str
    unit: str | None
    source_label: str
    binding: bool
    description: str
    affects_text: str
    override_text: str


@dataclass(frozen=True)
class Infeasibility:
    """§F.5. **The reported barrier is the earliest missed, not the tightest.**

    Timing errors accumulate along a race, so an athlete who misses a mid-race
    bike cut-off necessarily misses the finish by more. "Tightest by margin"
    therefore almost always names the finish, while the athlete's race actually
    ends at the bike cut-off, hours earlier.

    Told "you miss the finish by 132 minutes", this athlete would reasonably
    conclude the race is far out of reach. Told "you miss the bike cut-off by
    10 minutes", they learn the truth: ten minutes is a gap a winter of work —
    or a flatter race — genuinely closes. The two framings lead to opposite
    decisions, which is what made this worth changing the contract for.

    The user-facing message must be built from ``barrier``/``miss_minutes``,
    never from the tightest pair. The tightest pair is retained for the admin
    blast-radius view, which genuinely does want the worst case.
    """

    barrier: str
    miss_minutes: float
    #: One or two keys, computed **at** ``barrier`` — the levers must change
    #: the outcome the athlete was told about.
    levers: tuple[str, ...]
    tightest_barrier: str
    tightest_miss_minutes: float


@dataclass(frozen=True)
class SolveOutput:
    feasibility: Feasibility
    projected_minutes: float
    splits: tuple[Split, ...]
    segments: tuple[Segment, ...]
    gates: tuple[Gate, ...]
    fuelling: Fuelling
    aid_actions: tuple[AidAction, ...]
    #: Exactly five, always, in the fixed order.
    bags: tuple[Bag, ...]
    constraint_refs: tuple[ConstraintRef, ...]
    binding_constraint_key: str
    worst_margin_minutes: float
    margin_state: MarginState
    #: §F.6. Sorted dotted paths of optional inputs that were absent and for
    #: which the solver substituted a documented default. Sorted
    #: lexicographically so it is deterministic and diffable in golden files.
    assumed_fields: tuple[str, ...]
    infeasibility: Infeasibility | None = None
    stage_timings_ms: dict[str, int] = field(default_factory=dict)
    #: Set when the wetsuit is legal but not award-eligible (§4.4.3).
    wetsuit_warning: bool = False
    wetsuit_used: bool = False
