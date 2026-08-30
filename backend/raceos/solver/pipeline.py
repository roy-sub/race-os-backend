"""The six stages, orchestrated. ``SOLVER_MODEL.md`` Part II.

``solve(SolveInput) -> SolveOutput``. Pure: no database, no network, no clock,
no randomness. The same input produces byte-identical output on every platform
and in every process, which is what the golden suite exists to prove.

**Rounding happens once, here, at ``SolveOutput`` construction** (§0.4). Every
stage above works in unrounded values, and every cross-stage quantity is
derived from unrounded inputs — ``total_carb_g`` from the unrounded rate and
unrounded duration, never from the rounded rate. Doing it the other way is what
makes Stage 5's arithmetic-consistency invariant fail on long races.
"""

from __future__ import annotations

from raceos.domain.enums import (
    CONSTRAINT_KEYS,
    CONSTRAINT_UNITS,
    Feasibility,
    Leg,
    MarginState,
    SolverDistance,
)
from raceos.solver.environment import pressure_hpa_or_none, wbgt
from raceos.solver.models import (
    AidAction,
    Bag,
    ConstraintRef,
    Fuelling,
    Gate,
    Infeasibility,
    Segment,
    SolveInput,
    SolveOutput,
    Split,
)
from raceos.solver.profile import RaceProfile, build_profile
from raceos.solver.stages.s1_course import CourseGeometry, load_course
from raceos.solver.stages.s2_athlete import AthleteState, read_athlete
from raceos.solver.stages.s3_barriers import evaluate_barriers, intensity_grid, margin_state
from raceos.solver.stages.s5_fuelling import (
    FuellingResult,
    assert_carb_consistency,
    build_aid_actions,
    solve_fuelling,
)
from raceos.solver.stages.s6_bags import pack_bags
from raceos.solver.tables import intensity as intensity_tbl
from raceos.solver.tables import margins as margins_tbl
from raceos.solver.tables import precedence as prec_tbl
from raceos.solver.tables import rounding as rnd


class SolveInfeasible(Exception):
    """Stage 3 found a barrier that cannot be met. Carries the verdict.

    This is a **successful solve with an infeasible verdict**, not a failure —
    the API maps it to a 422 with the diagnostic detail attached, and Stage 4
    does not run.
    """

    def __init__(self, infeasibility: Infeasibility) -> None:
        super().__init__(f"misses {infeasibility.barrier} by {infeasibility.miss_minutes:.1f} min")
        self.infeasibility = infeasibility


def solve(request: SolveInput) -> SolveOutput:
    """Run all six stages."""
    # --- Stage 1 -----------------------------------------------------
    geometry = load_course(request.course)

    # --- Stage 2 -----------------------------------------------------
    athlete, assumed_from_athlete = read_athlete(request.athlete)

    assumed = list(assumed_from_athlete)
    pressure = pressure_hpa_or_none(request.forecast.pressure_hpa)
    if pressure is None:
        assumed.append("forecast.pressure_hpa")
    if request.forecast.cloud_cover_pct is None:
        assumed.append("forecast.cloud_cover_pct")

    wbgt_c = wbgt(
        request.forecast.temp_c,
        request.forecast.humidity,
        request.forecast.conditions,
        request.forecast.cloud_cover_pct,
    )
    distance = request.course.distance
    reference_if = intensity_tbl.IF_REF[distance][athlete.level]

    # --- Stage 3 -----------------------------------------------------
    feasibility = evaluate_barriers(
        geometry,
        athlete,
        request.forecast,
        reference_if=reference_if,
        wbgt_c=wbgt_c,
        density_pressure_hpa=pressure,
        distance=distance,
    )

    if not feasibility.feasible:
        missed = feasibility.earliest_missed
        assert missed is not None
        raise SolveInfeasible(
            Infeasibility(
                barrier=missed.barrier.name,
                miss_minutes=rnd.round_half_even(-missed.margin_minutes, rnd.MINUTES_DP),
                levers=feasibility.levers,
                tightest_barrier=feasibility.tightest.barrier.name,
                tightest_miss_minutes=rnd.round_half_even(
                    -feasibility.tightest.margin_minutes, rnd.MINUTES_DP
                ),
            )
        )

    # --- Stage 4 -----------------------------------------------------
    if_plan, barrier_bound = _planned_intensity(
        geometry,
        athlete,
        request,
        reference_if=reference_if,
        wbgt_c=wbgt_c,
        pressure=pressure,
        distance=distance,
    )
    profile = build_profile(
        geometry,
        athlete,
        request.forecast,
        if_plan=if_plan,
        wbgt_c=wbgt_c,
        density_pressure_hpa=pressure,
        distance=distance,
    )

    gates = _build_gates(profile, geometry)
    worst_margin = min(gate.margin_minutes for gate in gates)
    state = margin_state(worst_margin)

    # --- Stage 5 -----------------------------------------------------
    fuelling = solve_fuelling(
        profile, athlete, wbgt_c=wbgt_c, carb_override=request.options.carb_override
    )
    aid_actions = build_aid_actions(profile, fuelling, request.course.aid_stations)

    # --- Stage 6 -----------------------------------------------------
    bags = pack_bags(
        profile,
        athlete,
        fuelling,
        request.forecast,
        request.event,
        wbgt_c=wbgt_c,
        night_flag=request.options.night_flag,
    )

    return _build_output(
        profile=profile,
        athlete=athlete,
        request=request,
        gates=gates,
        worst_margin=worst_margin,
        state=state,
        fuelling=fuelling,
        aid_actions=aid_actions,
        bags=bags,
        assumed=assumed,
        barrier_bound=barrier_bound,
    )


def _planned_intensity(
    geometry: CourseGeometry,
    athlete: AthleteState,
    request: SolveInput,
    *,
    reference_if: float,
    wbgt_c: float,
    pressure: float | None,
    distance: SolverDistance,
) -> tuple[float, str | None]:
    """``IF_ref`` plus risk, raised only if a gate demands it (§4.2.1).

    ``barrier_adj`` is the **only** mechanism that moves intensity above
    ``IF_ref``: if the planned profile misses a gate, IF is raised along the
    grid to the lowest value that clears every gate, and the binding key
    becomes ``barrier:<name>``.
    """
    base = clampf(
        reference_if + intensity_tbl.RISK_ADJ[request.goal.risk],
        intensity_tbl.IF_PLAN_MIN,
        intensity_tbl.IF_MAX_FEAS[athlete.level],
    )

    def misses(candidate: float) -> str | None:
        """The first barrier this intensity fails to clear, if any."""
        profile = build_profile(
            geometry,
            athlete,
            request.forecast,
            if_plan=candidate,
            wbgt_c=wbgt_c,
            density_pressure_hpa=pressure,
            distance=distance,
        )
        for barrier in geometry.barriers:
            if profile.elapsed_at(barrier.leg, barrier.km) > barrier.limit_minutes_from_start:
                return str(barrier.name)
        return None

    missed = misses(base)
    if missed is None:
        return base, None

    for candidate in intensity_grid(athlete, reference_if):
        if candidate <= base:
            continue
        if misses(candidate) is None:
            return candidate, missed
    # Stage 3 already established every barrier is reachable somewhere on the
    # grid, so the ceiling is the best available answer.
    return intensity_tbl.IF_MAX_FEAS[athlete.level], missed


def clampf(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _build_gates(profile: RaceProfile, geometry: CourseGeometry) -> tuple[Gate, ...]:
    gates: list[Gate] = []
    for barrier in geometry.barriers:
        eta = profile.elapsed_at(barrier.leg, barrier.km)
        margin = barrier.limit_minutes_from_start - eta
        gates.append(
            Gate(
                name=barrier.name,
                leg=barrier.leg,
                limit_minutes=barrier.limit_minutes_from_start,
                eta_minutes=rnd.round_half_even(eta, rnd.MINUTES_DP),
                margin_minutes=rnd.round_half_even(margin, rnd.MINUTES_DP),
                load_pct=rnd.round_half_even(
                    100.0 * eta / barrier.limit_minutes_from_start, rnd.PERCENT_DP
                ),
                state=margin_state(margin),
            )
        )
    return tuple(gates)


def _binding_constraint_key(
    gates: tuple[Gate, ...], profile: RaceProfile, barrier_bound: str | None
) -> str:
    """§3.5's fixed resolution order.

    1. Any gate under the clear threshold -> ``barrier:<name>`` of the tightest.
       **A cut-off in play outranks everything**: it is what the athlete needs
       to know.
    2. Otherwise the key of the quantity that determined the largest leg by
       projected time.
    3. Otherwise ``model:if_ceiling``.
    """
    if barrier_bound is not None:
        return f"barrier:{barrier_bound}"

    tight = [gate for gate in gates if gate.margin_minutes < margins_tbl.MARGIN_CLEAR_MIN]
    if tight:
        tightest = min(tight, key=lambda gate: gate.margin_minutes)
        return f"barrier:{tightest.name}"

    if profile.bike.minutes >= profile.run.minutes:
        return "bike_threshold_power"
    if profile.run.minutes > 0:
        return "run_threshold_pace"
    return prec_tbl.BINDING_FALLBACK_KEY  # pragma: no cover - a race with no run


def _build_output(
    *,
    profile: RaceProfile,
    athlete: AthleteState,
    request: SolveInput,
    gates: tuple[Gate, ...],
    worst_margin: float,
    state: MarginState,
    fuelling: FuellingResult,
    aid_actions: tuple[AidAction, ...],
    bags: tuple[Bag, ...],
    assumed: list[str],
    barrier_bound: str | None,
) -> SolveOutput:
    """Construct the output. **The one place rounding happens.**"""
    segments: list[Segment] = []
    # Bike then run, each in its own ordinal order — the fixed accumulation
    # order §0.4 requires, renumbered contiguously for the plan.
    for ordinal, result in enumerate((*profile.bike.segments, *profile.run.segments), start=1):
        segments.append(
            Segment(
                ordinal=ordinal,
                leg=result.segment.leg,
                name=result.segment.name,
                from_km=result.segment.from_km,
                to_km=result.segment.to_km,
                terrain_desc=_terrain_desc(result.segment.net_gradient),
                target_watts=(
                    int(rnd.round_half_even(result.target_watts, rnd.TARGET_WATTS_DP))
                    if result.target_watts is not None
                    else None
                ),
                target_pace_sec_per_km=(
                    int(
                        rnd.round_half_even(
                            result.target_pace_sec_per_km, rnd.TARGET_PACE_SEC_PER_KM_DP
                        )
                    )
                    if result.target_pace_sec_per_km is not None
                    else None
                ),
                target_minutes=rnd.round_half_even(result.minutes, rnd.MINUTES_DP),
                note="",
            )
        )

    swim_pace = rnd.round_half_even(profile.swim.pace_sec_per_100m, rnd.SWIM_PACE_DP)
    run_pace = rnd.round_half_even(
        profile.run.pace_target_sec_per_km, rnd.TARGET_PACE_SEC_PER_KM_DP
    )
    splits = (
        Split(
            leg=Leg.SWIM,
            distance=request.course.leg(Leg.SWIM).distance_m / 1000.0,
            target_pace_or_power=_pace_str(swim_pace),
            unit="/100m",
            split_minutes=rnd.round_half_even(profile.swim.minutes, rnd.MINUTES_DP),
            note="Wetsuit" if profile.swim.wetsuit else "Non-wetsuit",
        ),
        Split(
            leg=Leg.BIKE,
            distance=request.course.leg(Leg.BIKE).distance_m / 1000.0,
            target_pace_or_power=str(
                int(rnd.round_half_even(profile.bike.average_power, rnd.TARGET_WATTS_DP))
            ),
            unit="w",
            split_minutes=rnd.round_half_even(profile.bike.minutes, rnd.MINUTES_DP),
            note=f"{profile.if_plan:.2f} IF",
        ),
        Split(
            leg=Leg.RUN,
            distance=request.course.leg(Leg.RUN).distance_m / 1000.0,
            target_pace_or_power=_pace_str(run_pace),
            unit="/km",
            split_minutes=rnd.round_half_even(profile.run.minutes, rnd.MINUTES_DP),
            note="Heat adjusted" if profile.run.d_heat > 1.0 else "",
        ),
    )

    carb_rate = int(rnd.round_half_even(fuelling.carb_g_per_hr, rnd.CARB_G_DP))
    carb_total = int(rnd.round_half_even(fuelling.total_carb_g, rnd.CARB_G_DP))
    assert_carb_consistency(fuelling, carb_rate, carb_total)

    packed = Fuelling(
        carb_g_per_hr=carb_rate,
        fluid_ml_per_hr=int(rnd.round_to_step(fuelling.fluid_ml_per_hr, rnd.FLUID_ML_STEP)),
        sodium_mg_per_hr=int(rnd.round_to_step(fuelling.sodium_mg_per_hr, rnd.SODIUM_MG_STEP)),
        caffeine_mg_total=int(rnd.round_to_step(fuelling.caffeine_mg_total, rnd.CAFFEINE_MG_STEP)),
        total_carb_g=carb_total,
        overridden=fuelling.overridden,
        requires_multiple_transportable=fuelling.requires_multiple_transportable,
        binding_carb_key=fuelling.binding_carb_key,
        binding_fluid_key=fuelling.binding_fluid_key,
        binding_sodium_key=fuelling.binding_sodium_key,
        binding_caffeine_key=fuelling.binding_caffeine_key,
    )

    binding_key = _binding_constraint_key(gates, profile, barrier_bound)

    return SolveOutput(
        feasibility=(Feasibility.CLEAR if state is MarginState.CLEAR else Feasibility.TIGHT),
        projected_minutes=rnd.round_half_even(profile.total_minutes, rnd.MINUTES_DP),
        splits=splits,
        segments=tuple(segments),
        gates=gates,
        fuelling=packed,
        aid_actions=aid_actions,
        bags=bags,
        constraint_refs=_constraint_refs(request, athlete, binding_key, packed),
        binding_constraint_key=binding_key,
        worst_margin_minutes=rnd.round_half_even(worst_margin, rnd.MINUTES_DP),
        margin_state=state,
        # Sorted lexicographically so it is deterministic and diffable (§F.6).
        assumed_fields=tuple(sorted(assumed)),
        infeasibility=None,
        stage_timings_ms={},
        wetsuit_warning=profile.swim.wetsuit_warning,
        wetsuit_used=profile.swim.wetsuit,
    )


def _terrain_desc(net_gradient: float) -> str:
    percent = net_gradient * 100.0
    if percent >= 3.0:
        return f"Climb {percent:.1f}%"
    if percent <= -3.0:
        return f"Descent {percent:.1f}%"
    if abs(percent) < 0.5:
        return "Flat"
    return f"Rolling {percent:+.1f}%"


def _pace_str(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _constraint_refs(
    request: SolveInput,
    athlete: AthleteState,
    binding_key: str,
    fuelling: Fuelling,
) -> tuple[ConstraintRef, ...]:
    """The "Why this?" drawer, snapshotted at solve time.

    ``source_label`` carries provenance through untouched. **No numeric field
    here was read by the model** — §0.6: provenance is carried, never consulted.
    """
    binding_from_fuelling = {
        fuelling.binding_carb_key,
        fuelling.binding_fluid_key,
        fuelling.binding_sodium_key,
        fuelling.binding_caffeine_key,
    }
    values = {
        "swim_threshold_pace": athlete.swim_threshold_pace,
        "bike_threshold_power": athlete.bike_threshold_power,
        "run_threshold_pace": athlete.run_threshold_pace,
        "weight": athlete.weight,
        "sweat_rate": athlete.sweat_rate,
        "sodium_loss": athlete.sodium_loss,
        "gut_carb_ceiling": athlete.gut_carb_ceiling,
        "caffeine_tolerance": athlete.caffeine_tolerance,
    }

    refs: list[ConstraintRef] = []
    for key in CONSTRAINT_KEYS:
        entry = request.athlete.constraint(key)
        refs.append(
            ConstraintRef(
                key=key,
                name=key.replace("_", " ").title(),
                value=f"{values[key]:g}",
                unit=CONSTRAINT_UNITS[key],
                source_label=entry.source.value if entry else "",
                binding=(key == binding_key or key in binding_from_fuelling),
                description="",
                affects_text="",
                override_text="",
            )
        )
    return tuple(refs)
