"""Every enumerated value in the system.

These are created as native PostgreSQL enums so an invalid state is impossible
at the storage layer rather than merely discouraged at the application layer.

**Two vocabularies for distance are kept deliberately** and must not be
normalised into one (see ``docs/FIELD_NAME_RECONCILIATION.md`` R-003):

* :class:`DistanceType` — ``Sprint | Olympic | 70.3 | Full`` — is what the
  frontend sends and what the database stores. It is the product's language.
* :class:`SolverDistance` — ``full | half | olympic | sprint`` — is what
  ``SOLVER_MODEL.md`` specifies and what every table in ``solver/tables/`` is
  keyed by. It is the model's language.

:data:`DISTANCE_TO_SOLVER` is the single mapping between them. Collapsing the
two would put either a solver term in front of users or a marketing term
inside the model's constant tables, and ``70.3`` is not a valid Python
identifier in the first place.
"""

from __future__ import annotations

from enum import Enum


class _StrEnum(str, Enum):
    """String-valued enum whose ``str()`` is its value, not ``Class.MEMBER``."""

    def __str__(self) -> str:
        return str(self.value)


# ---------------------------------------------------------------------------
# Identity and athlete
# ---------------------------------------------------------------------------


class AccountState(_StrEnum):
    ACTIVE = "active"
    DORMANT = "dormant"
    #: GDPR-erased. The row survives as a tombstone so invoices and audit
    #: records stay referentially intact; the PII on it is scrubbed.
    ERASED = "erased"


class UserTier(_StrEnum):
    FREE = "free"
    PER_RACE = "per_race"
    SEASON = "season"
    COACH = "coach"


class AthleteLevel(_StrEnum):
    FIRST = "first"
    IMPROVER = "improver"
    EXPERIENCED = "experienced"


class UnitSystem(_StrEnum):
    METRIC = "metric"
    IMPERIAL = "imperial"


class ConstraintSource(_StrEnum):
    """Provenance. Law 2: it travels with every value, forever.

    No branch in the solver reads this. An ``ESTIMATED`` constraint is used
    with exactly the numeric weight of a ``MEASURED`` one — there is no
    down-weighting anywhere (``SOLVER_MODEL.md`` §0.6), and a CI test asserts
    it by permuting every source in a golden input and requiring
    byte-identical numeric output.
    """

    MEASURED = "measured"
    TESTED = "tested"
    MANUAL = "manual"
    ESTIMATED = "estimated"


class BikePosition(_StrEnum):
    """SOLVER_MODEL.md §F.2. Drives CdA, the largest lever on the bike split."""

    ROAD_HOODS = "road_hoods"
    ROAD_DROPS = "road_drops"
    ROAD_CLIPONS = "road_clipons"
    TT_BIKE = "tt_bike"


class HelmetType(_StrEnum):
    STANDARD = "standard"
    AERO = "aero"


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------


class DistanceType(_StrEnum):
    """The product's vocabulary, from ``lib/raceDirectory.ts``."""

    SPRINT = "Sprint"
    OLYMPIC = "Olympic"
    HALF = "70.3"
    FULL = "Full"


class SolverDistance(_StrEnum):
    """The model's vocabulary, from ``SOLVER_MODEL.md`` §0.3."""

    FULL = "full"
    HALF = "half"
    OLYMPIC = "olympic"
    SPRINT = "sprint"


#: The only mapping between the two vocabularies. Configured here rather than
#: inferred anywhere, so a reader can see the whole correspondence at once.
DISTANCE_TO_SOLVER: dict[DistanceType, SolverDistance] = {
    DistanceType.SPRINT: SolverDistance.SPRINT,
    DistanceType.OLYMPIC: SolverDistance.OLYMPIC,
    DistanceType.HALF: SolverDistance.HALF,
    DistanceType.FULL: SolverDistance.FULL,
}

SOLVER_TO_DISTANCE: dict[SolverDistance, DistanceType] = {
    solver: product for product, solver in DISTANCE_TO_SOLVER.items()
}


class Difficulty(_StrEnum):
    APPROACHABLE = "APPROACHABLE"
    MODERATE = "MODERATE"
    HARD = "HARD"
    BRUTAL = "BRUTAL"


class Provenance(_StrEnum):
    """Law 2 for course facts. Every course fact carries one of these."""

    OFFICIAL = "OFFICIAL"
    CROWD = "CROWD"
    ESTIMATED = "ESTIMATED"


class BundleStatus(_StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


class SurfaceQuality(_StrEnum):
    """From the course bundle, not the athlete (``SOLVER_MODEL.md`` §I.2.2).

    Becomes ``Crr``. The gap between ``typical_road`` (0.0050) and
    ``rough_chipseal`` (0.0065) is worth about eight minutes over 180 km, so
    a surface change must show up in the blast-radius diff rather than pass
    silently.
    """

    SMOOTH_ASPHALT = "smooth_asphalt"
    TYPICAL_ROAD = "typical_road"
    ROUGH_CHIPSEAL = "rough_chipseal"


class Leg(_StrEnum):
    SWIM = "SWIM"
    BIKE = "BIKE"
    RUN = "RUN"


#: Legs in the fixed order the solver accumulates them (§0.4). Floating-point
#: addition is not associative, so the order is specified rather than left to
#: the implementer.
LEG_ORDER: tuple[Leg, ...] = (Leg.SWIM, Leg.BIKE, Leg.RUN)


class WaypointType(_StrEnum):
    """Deliberately not stored inside ``aid_stations``.

    "One action per aid station" (``SOLVER_MODEL.md`` §5.5) is a correctness
    property, and keeping the aid-station array pure makes it hold by
    construction rather than depending on every future reader remembering to
    filter on a discriminator.
    """

    TRANSITION = "transition"
    SPECIAL_NEEDS = "special_needs"
    DISTANCE_MARKER = "distance_marker"


class SegmentNameSource(_StrEnum):
    OSM_WAY = "OSM_WAY"
    DERIVED_TERRAIN = "DERIVED_TERRAIN"
    SYNTHETIC = "SYNTHETIC"


class RaceStatus(_StrEnum):
    UPCOMING = "upcoming"
    CANCELLED = "cancelled"
    DEFERRED = "deferred"
    COMPLETED = "completed"


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


class PlanStatus(_StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAST = "past"
    #: A coach-built plan is not the athlete's plan until they approve it.
    PENDING_ATHLETE_APPROVAL = "pending_athlete_approval"


class Feasibility(_StrEnum):
    CLEAR = "CLEAR"
    TIGHT = "TIGHT"
    STALE = "STALE"
    NOT_SOLVED = "NOT_SOLVED"


class MarginState(_StrEnum):
    """Boundaries are closed from above (``SOLVER_MODEL.md`` §3.5).

    Exactly 20.0 is ``clear``; exactly 0.0 is ``tight``. Comparison is against
    the value already rounded to 0.1 min, so a plan cannot flicker between
    states on a float-representation difference.
    """

    CLEAR = "clear"
    TIGHT = "tight"
    BAD = "bad"


class BagKey(_StrEnum):
    MORNING = "morning"
    BIKE_T1 = "bike_t1"
    RUN_T2 = "run_t2"
    BIKE_SN = "bike_sn"
    RUN_SN = "run_sn"


#: Exactly five bags, always, in this order — even when one is empty. An empty
#: Run Special Needs bag is information, not an omission (§6.1).
BAG_ORDER: tuple[BagKey, ...] = (
    BagKey.MORNING,
    BagKey.BIKE_T1,
    BagKey.RUN_T2,
    BagKey.BIKE_SN,
    BagKey.RUN_SN,
)


class RiskLevel(_StrEnum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class DriftCause(_StrEnum):
    FORECAST = "forecast"
    CONSTRAINT_CHANGE = "constraint_change"
    COURSE_BUNDLE_CHANGE = "course_bundle_change"


class DriftSeverity(_StrEnum):
    NORMAL = "normal"
    #: Any barrier margin would fall under the configured risk threshold.
    CUTOFF_RISK = "cutoff_risk"


class DriftStatus(_StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    DISMISSED = "dismissed"


class SolveJobStatus(_StrEnum):
    """The async escape hatch. The synchronous path is the default."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Post-race
# ---------------------------------------------------------------------------


class RaceFileFormat(_StrEnum):
    FIT = "fit"
    GPX = "gpx"
    TCX = "tcx"


class RaceFileStatus(_StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


class CompareState(_StrEnum):
    GOOD = "good"
    OK = "ok"
    WARN = "warn"
    BAD = "bad"


# ---------------------------------------------------------------------------
# Coach, sharing, billing
# ---------------------------------------------------------------------------


class CoachLinkStatus(_StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"


class ShareScope(_StrEnum):
    """No scope exposes constraints or account data. Not even ``FULL_PLAN``."""

    FULL_PLAN = "full_plan"
    PACING_ONLY = "pacing_only"
    BAGS_ONLY = "bags_only"
    RACE_CARD = "race_card"


class SubscriptionStatus(_StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    PAST_DUE = "past_due"


class PurchaseStatus(_StrEnum):
    """Two-phase: authorize, then capture only after a successful solve."""

    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    VOIDED = "voided"
    REFUNDED = "refunded"


class RefundReason(_StrEnum):
    RACE_CANCELLED = "race_cancelled"
    BUNDLE_ERROR = "bundle_error"
    OTHER = "other"


class Currency(_StrEnum):
    GBP = "GBP"
    USD = "USD"
    EUR = "EUR"


# ---------------------------------------------------------------------------
# Notifications, admin, ops
# ---------------------------------------------------------------------------


class NotificationType(_StrEnum):
    DRIFT = "drift"
    WEEK = "week"
    CUTOFF = "cutoff"
    BUNDLE = "bundle"
    ANALYSIS = "analysis"
    DIGEST = "digest"


#: Types whose in-app delivery cannot be switched off. The user chooses the
#: channel; they do not choose whether a cut-off warning exists.
CRITICAL_NOTIFICATION_TYPES: frozenset[NotificationType] = frozenset(
    {NotificationType.DRIFT, NotificationType.CUTOFF}
)


class NotificationSeverity(_StrEnum):
    INFO = "info"
    OK = "ok"
    WARN = "warn"
    BAD = "bad"


class DriftSensitivity(_StrEnum):
    EVERYTHING = "everything"
    BALANCED = "balanced"
    CRITICAL = "critical"


class CrowdCategory(_StrEnum):
    AID_STATION = "aid_station"
    CUTOFF = "cutoff"
    ELEVATION = "elevation"
    ROUTE = "route"
    SPECIAL_NEEDS = "special_needs"
    WATER_TEMP = "water_temp"


class CrowdStatus(_StrEnum):
    PENDING = "pending"
    PROMOTED = "promoted"
    HELD = "held"
    REJECTED = "rejected"


class CrowdConfidence(_StrEnum):
    HIGH = "high"
    MED = "med"
    LOW = "low"


class ServiceStatus(_StrEnum):
    NOMINAL = "nominal"
    DEGRADED = "degraded"
    DOWN = "down"


class IncidentSeverity(_StrEnum):
    SEV1 = "SEV-1"
    SEV2 = "SEV-2"
    SEV3 = "SEV-3"
    SEV4 = "SEV-4"


class AdminRole(_StrEnum):
    """RBAC by role, never a boolean.

    Support cannot see bundle publish controls or the refunds workspace, and
    that is expressed by not holding the role rather than by a UI condition.
    """

    SUPPORT = "support"
    OPS = "ops"
    ADMIN = "admin"


# ---------------------------------------------------------------------------
# Solver-facing
# ---------------------------------------------------------------------------


class BindDirection(_StrEnum):
    """Which way a candidate limit binds in ``bind()`` (§0.5)."""

    UPPER = "UPPER"
    LOWER = "LOWER"


#: The eight canonical athlete constraint keys, from ``lib/settings.ts``
#: CONSTRAINTS and ``lib/racePlan.ts`` CONSTRAINTS. All eight are required;
#: a missing one raises ``MissingConstraint`` naming the key, never a default.
CONSTRAINT_KEYS: tuple[str, ...] = (
    "swim_threshold_pace",
    "bike_threshold_power",
    "run_threshold_pace",
    "weight",
    "sweat_rate",
    "sodium_loss",
    "gut_carb_ceiling",
    "caffeine_tolerance",
)

#: Canonical unit per constraint key, as the frontend displays them.
CONSTRAINT_UNITS: dict[str, str] = {
    "swim_threshold_pace": "/100m",
    "bike_threshold_power": "w",
    "run_threshold_pace": "/km",
    "weight": "kg",
    "sweat_rate": "L/hr",
    "sodium_loss": "mg/L",
    "gut_carb_ceiling": "g/hr",
    "caffeine_tolerance": "mg",
}

#: Constraints that enter the time model, and the lever key each one emits
#: when perturbing it would change an infeasible outcome (§3.4). The other
#: four are deliberately absent: perturbing them returns zero, so offering
#: them would be dishonest.
LEVER_KEYS: dict[str, str] = {
    "bike_threshold_power": "raise_ftp",
    "run_threshold_pace": "improve_run_pace",
    "swim_threshold_pace": "improve_swim_pace",
    "weight": "reduce_weight",
}

#: Always available, and the only lever offered when nothing else clears the
#: significance threshold — an honest "nothing you can change before race day
#: closes this gap".
LEVER_LOWER_GOAL = "lower_goal"
