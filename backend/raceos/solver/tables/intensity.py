"""Intensity targets and the Stage 3 grid. §A → ``intensity.py``.

``IF_ref`` is an **evidence-based target, not the output of a free search**
(§4.1). The solver departs from it only when a named constraint requires it,
for two reasons — and the second is the one that matters:

1. **Explainability.** "IF 0.70 because that is the published band for an
   improver at full distance, and no barrier required more" is an explanation.
   "IF 0.7043 because that was the argmin" is not.
2. **The free optimum coincides with ``IF_ref`` anyway**, which is a
   consistency check on the model rather than a coincidence. For Athlete M,
   raising IF above 0.70 gains bike time at −291.8 min per unit IF and loses
   run time at +400.8, so the total-time derivative at ``IF_ref`` is +109.0 min
   per unit IF: strictly worse.

That second property depends on ``bike_coupling_c1``, the least evidenced
constant in the document. For Athlete M the optimum holds for any c₁ ≥ 1.165;
at the configured 1.6 there is headroom, but if calibration pushes c₁ below
~1.2 the free optimum moves above ``IF_ref`` and this rationale weakens.
"""

from __future__ import annotations

from typing import Final

from raceos.domain.enums import AthleteLevel, RiskLevel, SolverDistance

# ---------------------------------------------------------------------------
# Reference intensity (§4.2.1)
# ---------------------------------------------------------------------------

#: **`full` is well supported**: multiple independent sources converge on
#: 0.65–0.78 for age-groupers, derived from Allen & Coggan.
#:
#: **`half` is contested, and this matters more than anything else in this
#: table**, because 70.3 is a primary distance and we sit on one side of the
#: disagreement by assumption. Allen & Coggan are cited at 0.83–0.87;
#: TrainingPeaks spans 0.72–0.85; most coaching sources put age-groupers at
#: 0.75–0.80. This model takes the lower, age-grouper-weighted figure on the
#: reasoning that the higher band describes athletes racing for a result rather
#: than athletes trying to run well off the bike. That reasoning is plausible
#: and unverified, and it is worth ~8 minutes over 90 km — a third of the
#: entire clear/tight margin band. ⚠ VERIFY (§E-13); see LAUNCH_BLOCKERS.md.
#:
#: **`olympic` and `sprint` are extrapolated from the shape of the other two
#: rows and are OUT OF PRIMARY SCOPE (§0.1b)** — Low confidence, unvalidated,
#: and not to be treated as evidence-backed. Short-course racing is
#: drafting-legal and tactical in a way this model does not represent at all.
IF_REF: Final[dict[SolverDistance, dict[AthleteLevel, float]]] = {
    SolverDistance.FULL: {
        AthleteLevel.FIRST: 0.65,
        AthleteLevel.IMPROVER: 0.70,
        AthleteLevel.EXPERIENCED: 0.75,
    },
    SolverDistance.HALF: {
        AthleteLevel.FIRST: 0.72,
        AthleteLevel.IMPROVER: 0.78,
        AthleteLevel.EXPERIENCED: 0.83,
    },
    SolverDistance.OLYMPIC: {
        AthleteLevel.FIRST: 0.80,
        AthleteLevel.IMPROVER: 0.85,
        AthleteLevel.EXPERIENCED: 0.88,
    },
    SolverDistance.SPRINT: {
        AthleteLevel.FIRST: 0.85,
        AthleteLevel.IMPROVER: 0.90,
        AthleteLevel.EXPERIENCED: 0.95,
    },
}

#: Distances whose intensity bands are extrapolated rather than sourced. Their
#: golden cases exercise code paths; §C sets no error target for them, and they
#: must not be marketed on the same accuracy claim as full and 70.3.
UNVALIDATED_DISTANCES: Final[frozenset[SolverDistance]] = frozenset(
    {SolverDistance.OLYMPIC, SolverDistance.SPRINT}
)

RISK_ADJ: Final[dict[RiskLevel, float]] = {
    RiskLevel.CONSERVATIVE: -0.03,
    RiskLevel.BALANCED: 0.00,
    RiskLevel.AGGRESSIVE: 0.03,
}

#: Top of the published raceable band per level, +0.05. The contract's "no
#: sustained power above threshold" bound; it binds well below threshold for
#: every level.
IF_MAX_FEAS: Final[dict[AthleteLevel, float]] = {
    AthleteLevel.FIRST: 0.75,
    AthleteLevel.IMPROVER: 0.80,
    AthleteLevel.EXPERIENCED: 0.85,
}

IF_PLAN_MIN: Final[float] = 0.40

#: A short steep ramp may exceed threshold briefly; a segment may not be
#: planned at a sustained supra-threshold target.
IF_SEGMENT_CEILING: Final[float] = 1.05

# ---------------------------------------------------------------------------
# The Stage 3 intensity grid (§3.2)
# ---------------------------------------------------------------------------

IF_GRID_STEP: Final[float] = 0.005
IF_GRID_SPAN_BELOW_REF: Final[float] = 0.20
IF_GRID_FLOOR: Final[float] = 0.50

#: An athlete racing a cut-off does move through transition faster, but not by
#: an unbounded amount — they still have to find their bag. **Estimate, low
#: confidence**; worth about two minutes at full distance.
TRANSITION_HURRY_FACTOR: Final[float] = 0.85

#: When true, levers re-run the whole grid (4 × 105 ms) instead of evaluating
#: at the IF that minimised the base profile (~7 ms). §3.4 states the
#: approximation explicitly: the optimum IF moves negligibly under a 5%
#: constraint perturbation, and it affects only the *ranking* of levers, never
#: any number in a plan.
LEVER_REOPTIMISE: Final[bool] = False

# ---------------------------------------------------------------------------
# Gradient modulation (§4.2.1)
#
# Swain (1997) and Atkinson et al. (2007): the optimal strategy varies power in
# parallel with gradient; Atkinson measured ~26 s saved over 40 km at 10% power
# variability.
#
# `tanh` is chosen over a linear ramp with clamps because it is smooth
# everywhere, bounded by construction (no clamp needed), monotonic, symmetric
# about zero, and has no knee to tune. Output is confined to [0.88, 1.12] for
# any gradient, including a bad terrain sample.
# ---------------------------------------------------------------------------

#: Atkinson's ~10% variability, rounded up slightly. Medium confidence.
K_GRADE: Final[float] = 0.12

#: **Estimate. Low confidence** — nothing published; a pure shape parameter.
#: Sets the gradient at which modulation reaches 76% of maximum.
G_SCALE: Final[float] = 0.04

# ---------------------------------------------------------------------------
# Variability ceiling (§4.2.2)
#
# **Honest note on what this actually does.** At segment resolution with
# k_grade = 0.12, Athlete M's VI on a genuinely mountainous course comes out at
# 1.003 — nowhere near binding. That is not because the plan is smooth; it is
# because segment-level modelling cannot see the sub-30-second surges that
# produce real-world VI of 1.03–1.06. **The VI ceiling is a safety rail against
# a pathological configuration, not an active constraint**, and it must not be
# described to users as though the model were managing their variability.
# ---------------------------------------------------------------------------

VI_MAX: Final[dict[SolverDistance, float]] = {
    SolverDistance.FULL: 1.05,
    SolverDistance.HALF: 1.06,
    SolverDistance.OLYMPIC: 1.08,
    SolverDistance.SPRINT: 1.10,
}

#: A **fixed sequence, not a search**: deterministic, terminates in at most
#: four steps, and k_grade = 0 (flat power) always satisfies any ceiling ≥ 1.0.
VI_BACKOFF_SEQUENCE: Final[tuple[float, ...]] = (0.75, 0.50, 0.25, 0.0)
