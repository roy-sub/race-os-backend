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
    #: Brought in from a tool outside RaceOS — a bike-split modeller, a
    #: coach's spreadsheet, a lab report.
    #:
    #: Distinct from ``MANUAL``, which was the nearest available stamp and is
    #: wrong: manual means a person typed what they believe, imported means a
    #: named external tool produced it. They age differently and they are
    #: defended differently, and a drawer saying "manual" about a figure from
    #: a bike-split modeller tells the athlete the wrong thing about their own
    #: number. ``source_detail`` carries which tool.
    IMPORTED = "imported"


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


class CourseAvailability(_StrEnum):
    """Whether the directory row can actually be planned for yet.

    A course is listed long before it is planable. Announcing the season and
    then hiding fourteen of its fifteen events would be worse for an athlete
    than saying plainly which one is ready — so a ``COMING_SOON`` row is real,
    visible and honest, and it simply cannot be entered.
    """

    #: A published bundle exists. The race can be entered and planned.
    AVAILABLE = "available"
    #: Announced, dated, listed — no course data yet. Not enterable.
    COMING_SOON = "coming_soon"


class CourseVisibility(_StrEnum):
    """Who a course is listed for.

    The two values are not a permission model — nothing sensitive hides behind
    them. They separate the *marketing* course, which exists to show a signed
    out visitor what a race map looks like, from the *catalogue*, which is the
    set of real events an athlete can plan for. Showing both to a signed-in
    athlete would put an illustrative, deliberately out-of-scale map next to
    the surveyed ones and invite them to be compared.
    """

    #: In the athlete-facing catalogue. Listed to everyone.
    CATALOGUE = "catalogue"
    #: The signed-out showcase only. Never listed to an authenticated user.
    SHOWCASE = "showcase"
    #: Withdrawn from the directory, and kept only so that a plan already
    #: solved against it still has something to name. Listed to nobody.
    #: Courses are retired rather than deleted because deleting one would
    #: orphan every plan an athlete already paid for.
    RETIRED = "retired"


class CurationStatus(_StrEnum):
    """Whether an athlete-submitted course has been reviewed into the catalogue.

    A submitted course is usable by the athlete who submitted it from the
    moment it builds — they added it to race it, and making them wait on a
    queue to plan their own race would be a worse product for no safety gain,
    because they are the only one who can see it.

    What review decides is the *other* direction: whether everyone else sees
    it too. The catalogue is the set of courses this system says are surveyed,
    and that claim is the product. One unchecked GPX trace promoted into it
    quietly makes every other row less trustworthy, because a reader cannot
    tell which kind of row they are looking at.

    ``REJECTED`` does not delete anything or take the course away from its
    submitter. It records that it was looked at and not published, with a
    reason, so the next reviewer does not start again from nothing.
    """

    #: Built, private to its submitter, not yet looked at.
    UNREVIEWED = "unreviewed"
    #: Reviewed and listed to everyone, like a house course.
    PUBLISHED = "published"
    #: Reviewed and not listed. Stays with its submitter, with a reason.
    REJECTED = "rejected"


class SubmissionStatus(_StrEnum):
    """Where an athlete-submitted course has got to.

    ``FAILED`` is a resting state, not an error the athlete has to clear: the
    problems are attached to the row, the files they already uploaded are
    still there, and re-submitting after replacing one file is the whole
    recovery path.
    """

    #: Created, files not all uploaded yet.
    DRAFT = "draft"
    #: Complete and waiting to be built.
    QUEUED = "queued"
    #: Being built right now.
    PROCESSING = "processing"
    #: Built, loaded, and usable as a course.
    READY = "ready"
    #: Rejected. ``problems`` says why, in the athlete's terms.
    FAILED = "failed"


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
    """Every kind of thing the system tells an athlete about.

    Each one corresponds to an event the code actually produces. There is no
    entry here for something a job might emit one day: an unreachable type
    shows up in the preferences screen as a switch that governs nothing.
    """

    DRIFT = "drift"
    WEEK = "week"
    CUTOFF = "cutoff"
    BUNDLE = "bundle"
    ANALYSIS = "analysis"
    DIGEST = "digest"
    #: A plan built by a coach is waiting for the athlete to approve it. Not
    #: "plan solved": a solve the athlete asked for finishes while they are
    #: looking at it, and telling someone what they can already see is noise.
    #: This one they cannot see, because somebody else did it.
    PLAN_READY = "plan_ready"
    #: A coach shared a plan or a link with this athlete.
    COACH_SHARED = "coach_shared"
    #: An athlete accepted a coach's invitation. The only coach-facing type.
    ATHLETE_ACCEPTED = "athlete_accepted"
    PAYMENT_SUCCEEDED = "payment_succeeded"
    PAYMENT_FAILED = "payment_failed"
    #: A subscription is about to renew. Sent before the charge, not after —
    #: the point is the chance to cancel, which a receipt does not give.
    SUBSCRIPTION_RENEWING = "subscription_renewing"
    #: A support agent has asked to look at this account.
    #:
    #: Not on the original list of twelve, and added because it was being sent
    #: as ``DIGEST``: an athlete who had switched the weekly digest off would
    #: never have been told somebody asked to read their account. A privacy
    #: notice that a convenience preference can mute is not a notice.
    SUPPORT_ACCESS = "support_access"
    #: A reviewer published or declined a course this athlete submitted.
    COURSE_REVIEWED = "course_reviewed"


#: Types whose in-app delivery cannot be switched off. The user chooses the
#: channel; they do not choose whether a cut-off warning exists.
#:
#: ``PAYMENT_FAILED`` joins them for the same reason: an athlete whose card was
#: declined loses access to things they believe they have paid for, and
#: learning that from a locked screen instead of a message is the worst
#: available version of it.
CRITICAL_NOTIFICATION_TYPES: frozenset[NotificationType] = frozenset(
    {
        NotificationType.DRIFT,
        NotificationType.CUTOFF,
        NotificationType.PAYMENT_FAILED,
        # Somebody asking to read your account is not something you opt into
        # hearing about.
        NotificationType.SUPPORT_ACCESS,
    }
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
