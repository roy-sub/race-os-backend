"""Stage 3 — Protect the barriers (~1.1 s). ``SOLVER_MODEL.md`` §3.

**This runs before any optimisation**, and the reason is a product one: a plan
that is beautifully optimised and misses a cut-off is worthless, whereas a plan
that is ugly and makes every cut-off is a finish. This stage establishes what
is *possible* before Stage 4 decides what is *good*.

The correction that makes it work
---------------------------------
The naive reading of "evaluate every barrier against the athlete's maximum
sustainable output" is to construct one maximum-effort profile and check every
barrier against it. **That is wrong, and the model demonstrates why.** For
Athlete M on ``C-TRAM`` in hot conditions:

======================  ==========  ==========  ============
Profile                 Bike        Run         Total
======================  ==========  ==========  ============
Planned, IF 0.70        406.11 min  270.57 min  762.75 min
"Maximum", IF 0.80      376.93 min  310.65 min  773.65 min
======================  ==========  ==========  ============

Riding at maximum intensity saves 29.2 minutes on the bike and loses 40.1 on
the run — a **net loss of 10.9 minutes to the finish**. The maximum-output
profile reaches the bike cut-off sooner and the *finish* later. Checking the
finish against it would wrongly declare feasible plans infeasible, which is
precisely the over-biking failure mode the product exists to prevent, showing
up inside the feasibility check.

So each barrier is evaluated against **the minimum ETA achievable at that
barrier, taken independently per barrier** over a fixed intensity grid.
"""

from __future__ import annotations

from dataclasses import dataclass

from raceos.domain.enums import (
    LEVER_KEYS,
    LEVER_LOWER_GOAL,
    MarginState,
    SolverDistance,
)
from raceos.solver.models import Barrier, ForecastSnapshot
from raceos.solver.profile import RaceProfile, build_profile
from raceos.solver.stages.s1_course import CourseGeometry
from raceos.solver.stages.s2_athlete import AthleteState
from raceos.solver.tables import intensity as intensity_tbl
from raceos.solver.tables import margins as margins_tbl
from raceos.solver.tables import rounding as rounding_tbl


def intensity_grid(athlete: AthleteState, reference_if: float) -> tuple[float, ...]:
    """The fixed grid Stage 3 scans (§3.2). 61-71 points depending on level.

    Built by integer steps rather than by repeated addition, so the points are
    identical on every platform rather than accumulating float error.
    """
    ceiling = intensity_tbl.IF_MAX_FEAS[athlete.level]
    floor = max(reference_if - intensity_tbl.IF_GRID_SPAN_BELOW_REF, intensity_tbl.IF_GRID_FLOOR)
    step = intensity_tbl.IF_GRID_STEP
    count = int(round((ceiling - floor) / step)) + 1
    return tuple(floor + index * step for index in range(count))


@dataclass(frozen=True)
class BarrierMinimum:
    barrier: Barrier
    eta_minutes: float
    if_at_min: float
    margin_minutes: float


@dataclass(frozen=True)
class Feasibility:
    feasible: bool
    minima: tuple[BarrierMinimum, ...]
    #: The **earliest missed** barrier, by ``limit_minutes_from_start`` (§F.5).
    earliest_missed: BarrierMinimum | None
    #: The smallest margin across all barriers, missed or not. Diagnostics.
    tightest: BarrierMinimum
    levers: tuple[str, ...]


def evaluate_barriers(
    geometry: CourseGeometry,
    athlete: AthleteState,
    forecast: ForecastSnapshot,
    *,
    reference_if: float,
    wbgt_c: float,
    density_pressure_hpa: float | None,
    distance: SolverDistance,
) -> Feasibility:
    """Scan the grid, take each barrier's own minimum, and judge."""
    grid = intensity_grid(athlete, reference_if)

    profiles: list[RaceProfile] = [
        build_profile(
            geometry,
            athlete,
            forecast,
            if_plan=candidate,
            wbgt_c=wbgt_c,
            density_pressure_hpa=density_pressure_hpa,
            distance=distance,
            # §3.2: the maximum-effort profile hurries transitions.
            hurry_transitions=True,
        )
        for candidate in grid
    ]

    minima: list[BarrierMinimum] = []
    for barrier in geometry.barriers:
        best_eta: float | None = None
        best_if = grid[0]
        for profile in profiles:
            eta = profile.elapsed_at(barrier.leg, barrier.km)
            # Ties break toward the **lowest IF** — the most conservative.
            # This matters for the swim-exit barrier, whose ETA does not depend
            # on bike intensity at all: every grid point ties, and the
            # tie-break makes the reported IF deterministic rather than an
            # artefact of grid order.
            if best_eta is None or eta < best_eta:
                best_eta = eta
                best_if = profile.if_plan
        assert best_eta is not None
        minima.append(
            BarrierMinimum(
                barrier=barrier,
                eta_minutes=best_eta,
                if_at_min=best_if,
                margin_minutes=barrier.limit_minutes_from_start - best_eta,
            )
        )

    missed = [entry for entry in minima if entry.margin_minutes < 0]
    tightest = min(minima, key=lambda entry: entry.margin_minutes)

    earliest_missed: BarrierMinimum | None = None
    levers: tuple[str, ...] = ()
    if missed:
        # §F.5: the EARLIEST missed, by limit — where the athlete's race
        # actually ends — not the tightest by margin, which almost always
        # names the finish and materially misinforms them.
        earliest_missed = min(missed, key=lambda entry: entry.barrier.limit_minutes_from_start)
        levers = compute_levers(
            geometry,
            athlete,
            forecast,
            target=earliest_missed,
            wbgt_c=wbgt_c,
            density_pressure_hpa=density_pressure_hpa,
            distance=distance,
        )

    return Feasibility(
        feasible=not missed,
        minima=tuple(minima),
        earliest_missed=earliest_missed,
        tightest=tightest,
        levers=levers,
    )


def _perturbed(athlete: AthleteState, key: str) -> AthleteState:
    """The athlete with one constraint improved by the configured fraction.

    "Improving" differs per constraint: more power and less weight are both
    improvements, and so is a *lower* pace, because pace is seconds per unit
    distance.
    """
    from dataclasses import replace

    delta = margins_tbl.LEVER_PERTURBATION
    if key == "bike_threshold_power":
        return replace(athlete, bike_threshold_power=athlete.bike_threshold_power * (1 + delta))
    if key == "run_threshold_pace":
        improved = athlete.run_threshold_pace * (1 - delta)
        return replace(athlete, run_threshold_pace=improved, d_thresh_km=3600.0 / improved)
    if key == "swim_threshold_pace":
        return replace(athlete, swim_threshold_pace=athlete.swim_threshold_pace * (1 - delta))
    if key == "weight":
        lighter = athlete.weight * (1 - delta)
        return replace(
            athlete,
            weight=lighter,
            total_mass_kg=athlete.total_mass_kg - (athlete.weight - lighter),
        )
    raise ValueError(f"{key!r} is not lever-eligible")  # pragma: no cover


def compute_levers(
    geometry: CourseGeometry,
    athlete: AthleteState,
    forecast: ForecastSnapshot,
    *,
    target: BarrierMinimum,
    wbgt_c: float,
    density_pressure_hpa: float | None,
    distance: SolverDistance,
) -> tuple[str, ...]:
    """One or two concrete changes that would alter the outcome (§3.4).

    One-at-a-time numerical sensitivity: deterministic, cheap, and directly
    explainable. Levers are computed **at the reported barrier** — the earliest
    missed one — because they must change *the outcome the athlete was told
    about*. Offering "raise FTP" because it would help a finish the athlete
    will never reach would be advice about a hypothetical race.

    ``sweat_rate``, ``sodium_loss``, ``gut_carb_ceiling`` and
    ``caffeine_tolerance`` are **not** lever-eligible: they do not enter the
    time model, so perturbing them returns zero and offering them would be
    dishonest.

    One stated approximation (§3.4, §0.8): each perturbed constraint is
    evaluated at the IF that produced the base minimum rather than by re-running
    the whole grid. Re-optimising per lever would cost 4 × 105 ms against ~7 ms;
    the optimum IF moves negligibly under a 5% perturbation, and the
    approximation affects only the *ranking* of levers, never a number in a plan.
    """

    def eta_at(state: AthleteState, if_plan: float) -> float:
        profile = build_profile(
            geometry,
            state,
            forecast,
            if_plan=if_plan,
            wbgt_c=wbgt_c,
            density_pressure_hpa=density_pressure_hpa,
            distance=distance,
            hurry_transitions=True,
        )
        return profile.elapsed_at(target.barrier.leg, target.barrier.km)

    if intensity_tbl.LEVER_REOPTIMISE:  # pragma: no cover - config-gated
        base = target.eta_minutes
    else:
        base = eta_at(athlete, target.if_at_min)

    deltas: list[tuple[float, str]] = []
    # Iterate a tuple in a fixed order, never a dict, so ranking ties break
    # deterministically (§0.4).
    for key in ("bike_threshold_power", "run_threshold_pace", "swim_threshold_pace", "weight"):
        delta = eta_at(_perturbed(athlete, key), target.if_at_min) - base
        if -delta >= margins_tbl.LEVER_SIGNIFICANCE_MINUTES:
            deltas.append((delta, LEVER_KEYS[key]))

    if not deltas:
        # An honest "nothing you can change before race day closes this gap."
        return (LEVER_LOWER_GOAL,)

    deltas.sort(key=lambda pair: (pair[0], pair[1]))
    return tuple(name for _, name in deltas[:2])


def margin_state(worst_margin_minutes: float) -> MarginState:
    """§3.5. Both boundaries are **closed from above**.

    Exactly 20.0 is ``clear``; exactly 0.0 is ``tight``. The comparison is
    against the value already rounded to 0.1 min, so a plan cannot flicker
    between states on a float-representation difference.
    """
    rounded = rounding_tbl.round_half_even(worst_margin_minutes, rounding_tbl.MINUTES_DP)
    if rounded >= margins_tbl.MARGIN_CLEAR_MIN:
        return MarginState.CLEAR
    if rounded >= margins_tbl.MARGIN_TIGHT_MIN:
        return MarginState.TIGHT
    return MarginState.BAD
