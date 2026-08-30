"""Stage 2 — Read athlete constraints (~0.3 s). ``SOLVER_MODEL.md`` §2.

Loads all eight constraints with their ``source``, validates them against the
plausibility ranges, converts to internal units, and derives the equipment
parameters.

**No branch in this stage or any later stage reads ``constraint.source``.**
Provenance is carried, never consulted. An ``estimated`` constraint is used
with exactly the numeric weight of a ``measured`` one — no down-weighting, no
widened interval, no shrinkage toward a population mean.

That is a deliberate and slightly uncomfortable choice, so it is worth stating
why it is right: a model that quietly hedged estimated inputs would produce a
plan that is neither the plan implied by the athlete's stated numbers nor the
plan implied by any other numbers, and the "Why this?" drawer could not
honestly explain it.

The two threshold definitions, stated once so they cannot be got wrong:

``run_threshold_pace``
    The pace the athlete could hold in an all-out **one-hour** race, s·km⁻¹ —
    the running equivalent of FTP. *Not* 10 km pace, *not* lactate-threshold
    pace, *not* marathon pace.

``swim_threshold_pace``
    **Critical Swim Speed**, s·(100 m)⁻¹, from the 400/200 time-trial pair.
    An **asymptotic** threshold speed — the slope of the distance-time line —
    not the pace at any particular distance.

These are not the same kind of quantity, and §4.4 explains why that matters: a
one-hour anchor is a point on a distance-time curve, while an asymptote is its
slope. Modelling both with the same Riegel decay would be wrong, and an earlier
draft of the document did exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass

from raceos.domain.enums import (
    CONSTRAINT_KEYS,
    AthleteLevel,
    BikePosition,
    HelmetType,
    SurfaceQuality,
)
from raceos.solver.cycling import cda_for
from raceos.solver.errors import ImplausibleConstraint, MissingConstraint
from raceos.solver.models import AthleteSnapshot
from raceos.solver.tables import equipment as eq
from raceos.solver.tables import plausibility as plaus


@dataclass(frozen=True)
class AthleteState:
    """The eight constraints plus everything derived from them (§2.3)."""

    level: AthleteLevel

    swim_threshold_pace: float
    bike_threshold_power: float
    run_threshold_pace: float
    weight: float
    sweat_rate: float
    sodium_loss: float
    gut_carb_ceiling: float
    caffeine_tolerance: float

    #: ``sweat_rate.measured_at_temp_c`` (§F.4), or ``None``.
    sweat_measured_at_temp_c: float | None

    total_mass_kg: float
    cda: float
    #: The distance covered in one hour at threshold, km. The anchor point for
    #: §4.3.1's Riegel extrapolation, and meaningful **only** under the
    #: one-hour definition above.
    #:
    #: There is deliberately no swim analogue: CSS is an asymptote, so it has
    #: no characteristic distance, and §4.4 uses the critical-speed model
    #: directly rather than an anchor.
    d_thresh_km: float

    bike_position: BikePosition
    helmet: HelmetType
    bike_setup_assumed: bool

    def crr_for(self, surface: SurfaceQuality) -> float:
        """Rolling resistance from the **course surface**, not the athlete."""
        return eq.CRR[surface]


def _require(snapshot: AthleteSnapshot, key: str) -> float:
    """Read one constraint, or raise naming the key. Never a silent default."""
    entry = snapshot.constraint(key)
    if entry is None:
        raise MissingConstraint(key)

    limits = plaus.PLAUSIBILITY[key]
    if not limits.minimum <= entry.value <= limits.maximum:
        raise ImplausibleConstraint(key, entry.value, limits.minimum, limits.maximum)
    return float(entry.value)


def read_athlete(snapshot: AthleteSnapshot) -> tuple[AthleteState, tuple[str, ...]]:
    """Stage 2. Returns the derived state and any ``assumed_fields`` it added.

    All eight constraints are required. A missing one raises
    :class:`MissingConstraint` naming the key — never a silent default, because
    a defaulted constraint produces a plan for an athlete who does not exist
    and nothing downstream could tell that from a real one.
    """
    values = {key: _require(snapshot, key) for key in CONSTRAINT_KEYS}
    assumed: list[str] = []

    setup = snapshot.bike_setup
    if setup is None:
        # §I.2.3: the fallback can sit up to 0.045 m² from the athlete's true
        # value — about 15 minutes over 180 km — so supplying `bike_setup`
        # later will frequently cross the drift thresholds. That is correct
        # behaviour: it is new information that genuinely moves the plan.
        position = eq.CDA_FALLBACK_POSITION
        helmet = eq.CDA_FALLBACK_HELMET
        assumed.append("athlete.bike_setup")
    else:
        position = setup.position
        helmet = setup.helmet

    sweat_entry = snapshot.constraint("sweat_rate")
    sweat_temp = sweat_entry.measured_at_temp_c if sweat_entry else None
    if sweat_temp is None:
        assumed.append("sweat_rate.measured_at_temp_c")

    state = AthleteState(
        level=snapshot.level,
        swim_threshold_pace=values["swim_threshold_pace"],
        bike_threshold_power=values["bike_threshold_power"],
        run_threshold_pace=values["run_threshold_pace"],
        weight=values["weight"],
        sweat_rate=values["sweat_rate"],
        sodium_loss=values["sodium_loss"],
        gut_carb_ceiling=values["gut_carb_ceiling"],
        caffeine_tolerance=values["caffeine_tolerance"],
        sweat_measured_at_temp_c=sweat_temp,
        total_mass_kg=values["weight"] + eq.BIKE_KIT_MASS_KG[snapshot.level],
        cda=cda_for(position, snapshot.level, helmet),
        d_thresh_km=3600.0 / values["run_threshold_pace"],
        bike_position=position,
        helmet=helmet,
        bike_setup_assumed=setup is None,
    )
    return state, tuple(assumed)


# ---------------------------------------------------------------------------
# §2.5.2 — converting a race result to the one-hour quantity
# ---------------------------------------------------------------------------


def threshold_pace_from_race(race_km: float, race_seconds: float, level: AthleteLevel) -> float:
    """Convert a recent race result to one-hour threshold pace, s·km⁻¹.

    Uses **the same Riegel exponent the model itself uses**, so the anchor and
    the extrapolation are inverses and round-trip exactly: an athlete who
    enters a race result and then races that same distance gets their actual
    result back.

    The asymmetry this exists to remove: accepting a 10 km pace directly as
    threshold pace is not a small approximation applied evenly. Its size
    depends on how fast the athlete is, because a 10 km race lasts a very
    different time for each of them — 3.0% optimistic for a 35-minute
    10 km runner, 0.0% for a 60-minute one. **The bias runs the wrong way: it
    is largest for the athletes who race closest to their limits.**

    Lives here rather than in a service because it must use the solver's own
    ``riegel_r``; a second copy of that exponent elsewhere would let the two
    drift and silently mix two populations in every back-test.
    """
    from raceos.solver.tables import run_model as run_tbl

    if race_km < run_tbl.RIEGEL_MIN_RACE_KM:
        raise ValueError(
            f"race distance {race_km} km is below {run_tbl.RIEGEL_MIN_RACE_KM} km, "
            f"where the Riegel form is unreliable; refuse rather than convert"
        )
    if race_seconds <= 0:
        raise ValueError("race duration must be positive")

    exponent = run_tbl.RIEGEL_R[level]
    one_hour_km = race_km * (3600.0 / race_seconds) ** (1.0 / exponent)
    return float(3600.0 / one_hour_km)
