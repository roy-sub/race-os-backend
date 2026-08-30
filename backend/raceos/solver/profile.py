"""Costing a race at a given intensity. Shared by Stages 3 and 4.

Stage 3 needs to evaluate many candidate profiles across an intensity grid;
Stage 4 needs to evaluate one and keep its detail. Both are the same
computation, so it lives here once rather than being written twice and drifting.

The stages remain distinct in what they *decide*: Stage 3 establishes what is
possible before Stage 4 decides what is good. That ordering is a product
decision — a plan that is beautifully optimised and misses a cut-off is
worthless, whereas a plan that is ugly and makes every cut-off is a finish.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from raceos.domain.enums import AthleteLevel, Leg, SolverDistance
from raceos.solver.bind import clamp
from raceos.solver.cycling import relative_wind_angle, solve_speed
from raceos.solver.environment import (
    air_density,
    alt_factor,
    bike_heat_factor,
    run_heat_factor,
)
from raceos.solver.models import ForecastSnapshot
from raceos.solver.stages.s1_course import CourseGeometry, SegmentGeometry
from raceos.solver.stages.s2_athlete import AthleteState
from raceos.solver.tables import intensity as intensity_tbl
from raceos.solver.tables import run_model as run_tbl
from raceos.solver.tables import swim_model as swim_tbl
from raceos.solver.tables import transitions as trans_tbl

# ---------------------------------------------------------------------------
# Bike
# ---------------------------------------------------------------------------


def grade_mod(gradient: float, k_grade: float) -> float:
    """``1 + k_grade · tanh(g / g_scale)`` (§4.2.1).

    ``tanh`` rather than a linear ramp with clamps because it is smooth
    everywhere, **bounded by construction** so no clamp is needed, monotonic,
    symmetric about zero, and has no knee to tune. Output is confined to
    ``[1 − k_grade, 1 + k_grade]`` for any gradient, including a bad terrain
    sample.
    """
    return 1.0 + k_grade * math.tanh(gradient / intensity_tbl.G_SCALE)


@dataclass(frozen=True)
class SegmentResult:
    segment: SegmentGeometry
    target_watts: float | None
    target_pace_sec_per_km: float | None
    minutes: float


@dataclass(frozen=True)
class BikeResult:
    segments: tuple[SegmentResult, ...]
    minutes: float
    normalised_power: float
    average_power: float
    variability_index: float
    k_grade_used: float
    vi_ceiling_bound: bool
    segment_ceiling_bound: bool


def _segment_minutes_over_histogram(
    segment: SegmentGeometry,
    power_w: float,
    *,
    athlete: AthleteState,
    forecast: ForecastSnapshot,
    density_pressure_hpa: float | None,
) -> float:
    """Integrate time over the segment's gradient histogram (§1.1, §4.2.1).

    **This is the single most important implementation detail in Stage 4.**
    Solving speed once at the segment's net gradient would make the model 3-37%
    fast depending on terrain, because time is convex in gradient.

    Air density is computed at the *segment's* mean elevation, per §I.1.6:
    density is a property of the air, so it varies along the course, unlike
    ``alt_factor``, which is a property of the athlete and uses the leg mean.
    """
    density = air_density(
        forecast.temp_c,
        forecast.humidity,
        segment.mean_elevation_m,
        density_pressure_hpa,
    )
    crr = athlete.crr_for(segment.surface_quality)
    wind_angle = relative_wind_angle(forecast.wind_dir_deg, segment.bearing_rad)

    total_seconds = 0.0
    for gradient_bin, distance_m in segment.histogram:
        speed = solve_speed(
            power_w,
            density=density,
            cda=athlete.cda,
            crr=crr,
            mass_kg=athlete.total_mass_kg,
            gradient=gradient_bin,
            wind_speed_ms=forecast.wind_speed_ms,
            wind_relative_rad=wind_angle,
        )
        total_seconds += distance_m / speed
    return total_seconds / 60.0


def solve_bike(
    geometry: CourseGeometry,
    athlete: AthleteState,
    forecast: ForecastSnapshot,
    *,
    if_plan: float,
    wbgt_c: float,
    density_pressure_hpa: float | None,
    distance: SolverDistance,
) -> BikeResult:
    """Cost the bike leg at ``if_plan``, backing off ``k_grade`` if VI binds."""
    leg = geometry.leg(Leg.BIKE)
    ftp_alt = athlete.bike_threshold_power * alt_factor(leg.mean_elevation_m)
    base_power = ftp_alt * if_plan * bike_heat_factor(wbgt_c)
    segment_ceiling = athlete.bike_threshold_power * intensity_tbl.IF_SEGMENT_CEILING

    ceiling_bound = False
    vi_bound = False
    k_grade = intensity_tbl.K_GRADE
    attempts = (k_grade, *(k_grade * factor for factor in intensity_tbl.VI_BACKOFF_SEQUENCE))

    results: tuple[SegmentResult, ...] = ()
    normalised = average = 0.0
    variability = 1.0

    for attempt_index, candidate_k in enumerate(attempts):
        results = ()
        weighted_fourth = 0.0
        weighted_mean = 0.0
        total_minutes = 0.0
        ceiling_bound = False

        for segment in leg.segments:
            raw_power = base_power * grade_mod(segment.net_gradient, candidate_k)
            power = clamp(raw_power, 0.0, segment_ceiling)
            if raw_power > segment_ceiling:
                ceiling_bound = True

            minutes = _segment_minutes_over_histogram(
                segment,
                power,
                athlete=athlete,
                forecast=forecast,
                density_pressure_hpa=density_pressure_hpa,
            )
            results += (
                SegmentResult(
                    segment=segment,
                    target_watts=power,
                    target_pace_sec_per_km=None,
                    minutes=minutes,
                ),
            )
            total_minutes += minutes
            weighted_fourth += minutes * power**4
            weighted_mean += minutes * power

        if total_minutes <= 0:  # pragma: no cover - a bundle with no bike distance
            break

        # NP at segment resolution: segments are all >> 30 s, so the usual
        # 30 s rolling average degenerates to the segment itself (§4.2.2).
        normalised = (weighted_fourth / total_minutes) ** 0.25
        average = weighted_mean / total_minutes
        variability = normalised / average if average > 0 else 1.0

        if variability <= intensity_tbl.VI_MAX[distance]:
            k_grade = candidate_k
            break
        vi_bound = True
        k_grade = candidate_k
        if attempt_index == len(attempts) - 1:  # pragma: no cover - k=0 always passes
            break

    return BikeResult(
        segments=results,
        minutes=sum(r.minutes for r in results),
        normalised_power=normalised,
        average_power=average,
        variability_index=variability,
        k_grade_used=k_grade,
        vi_ceiling_bound=vi_bound,
        segment_ceiling_bound=ceiling_bound,
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def minetti_cost(gradient: float) -> float:
    """Metabolic cost of running, J·kg⁻¹·m⁻¹ (§4.3.2)."""
    i = clamp(gradient, run_tbl.MINETTI_VALID_MIN, run_tbl.MINETTI_VALID_MAX)
    c5, c4, c3, c2, c1, c0 = run_tbl.MINETTI_COEFFS
    return ((((c5 * i + c4) * i + c3) * i + c2) * i + c1) * i + c0


def d_grade(gradient: float) -> float:
    """Gradient multiplier on pace. **A cost ratio is not a pace ratio.**

    At −20% the metabolic cost halves, but nobody runs a marathon descent at
    double pace — eccentric loading and biomechanical speed limits intervene.
    So the conversion is damped asymmetrically, and clamped at both ends.
    """
    ratio = minetti_cost(gradient) / minetti_cost(0.0)
    alpha = run_tbl.ALPHA_UP if gradient >= 0 else run_tbl.ALPHA_DN
    return clamp(ratio**alpha, run_tbl.D_GRADE_MIN, run_tbl.D_GRADE_MAX)


@dataclass(frozen=True)
class RunResult:
    segments: tuple[SegmentResult, ...]
    minutes: float
    pace_target_sec_per_km: float
    d_dist: float
    d_bike: float
    d_heat: float
    heat_clamped: bool
    threshold_clamp_bound: bool


def solve_run(
    geometry: CourseGeometry,
    athlete: AthleteState,
    forecast: ForecastSnapshot,
    *,
    if_realised: float,
    wbgt_c: float,
    distance: SolverDistance,
) -> RunResult:
    """Cost the run leg. The pace chain is multiplicative (§4.3.1)."""
    leg = geometry.leg(Leg.RUN)
    run_km = leg.distance_m / 1000.0

    riegel = run_tbl.RIEGEL_R[athlete.level]
    d_dist = (run_km / athlete.d_thresh_km) ** (riegel - 1.0)

    reference_if = intensity_tbl.IF_REF[distance][athlete.level]
    d_bike = (
        1.0
        + run_tbl.BIKE_COUPLING_C0[distance]
        + run_tbl.BIKE_COUPLING_C1 * max(0.0, if_realised - reference_if)
    )

    heat = run_heat_factor(wbgt_c, athlete.level)
    altitude = alt_factor(leg.mean_elevation_m)

    raw_target = athlete.run_threshold_pace * d_dist * d_bike * heat.factor / altitude

    # §4.3.3: no long-leg pace faster than threshold. Cannot bind at full or
    # half, where D_dist alone exceeds 1; *can* bind at sprint in cool
    # conditions, which is correct — a sprint run is near threshold.
    threshold_bound = raw_target < athlete.run_threshold_pace
    pace_target = max(raw_target, athlete.run_threshold_pace)

    results: tuple[SegmentResult, ...] = ()
    for segment in leg.segments:
        seconds = 0.0
        for gradient_bin, distance_m in segment.histogram:
            seconds += (distance_m / 1000.0) * pace_target * d_grade(gradient_bin)
        minutes = seconds / 60.0
        # The displayed target is the segment's own pace: its time over its
        # distance. That is what the athlete runs, and it already carries the
        # terrain inside the segment.
        segment_km = segment.distance_m / 1000.0
        results += (
            SegmentResult(
                segment=segment,
                target_watts=None,
                target_pace_sec_per_km=(seconds / segment_km) if segment_km else 0.0,
                minutes=minutes,
            ),
        )

    return RunResult(
        segments=results,
        minutes=sum(r.minutes for r in results),
        pace_target_sec_per_km=pace_target,
        d_dist=d_dist,
        d_bike=d_bike,
        d_heat=heat.factor,
        heat_clamped=heat.clamped,
        threshold_clamp_bound=threshold_bound,
    )


# ---------------------------------------------------------------------------
# Swim
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SwimResult:
    minutes: float
    pace_sec_per_100m: float
    wetsuit: bool
    wetsuit_warning: bool


def wetsuit_decision(water_temp_c: float) -> tuple[bool, bool]:
    """``(wetsuit, award_warning)`` from Ironman competition rules (§4.4.3).

    These thresholds are **genuinely discontinuous** — 24.5 °C and 24.6 °C
    produce different equipment, hence a ~4.5% pace step. That discontinuity is
    in the rules, not the model, and smoothing it would be wrong.

    The permitted-but-not-award-eligible band assumes an athlete racing for a
    result. That is an assumption about intent rather than physiology, so it is
    surfaced as a warning rather than hidden.
    """
    if water_temp_c < swim_tbl.WETSUIT_MANDATORY_BELOW_C:
        return True, False
    if water_temp_c <= swim_tbl.WETSUIT_LEGAL_MAX_C:
        return True, False
    if water_temp_c <= swim_tbl.WETSUIT_NON_AWARD_MAX_C:
        return False, True
    return False, False


def water_temp_adjustment(water_temp_c: float) -> float:
    """Seconds per 100 m. Both coefficients are Low-confidence placeholders."""
    clamped = clamp(water_temp_c, swim_tbl.WATER_TEMP_CLAMP_MIN, swim_tbl.WATER_TEMP_CLAMP_MAX)
    cold = swim_tbl.C_COLD * max(0.0, swim_tbl.COLD_THRESHOLD_C - clamped)
    warm = swim_tbl.C_WARM * max(0.0, water_temp_c - swim_tbl.WARM_THRESHOLD_C)
    return cold + warm


def solve_swim(
    geometry: CourseGeometry, athlete: AthleteState, forecast: ForecastSnapshot
) -> SwimResult:
    """Cost the swim. CSS is an **asymptote**, so this is not a Riegel decay.

    ``pace_max(d) = CSS_pace · (d − D′) / d`` is *faster* than CSS at every
    finite distance. A durability term then engages past the critical-speed
    model's validity window, because beyond about 30 minutes real sustainable
    speed falls below CSS rather than approaching it from above.

    Order matters and is deliberate: the wetsuit **multiplies** swimming pace,
    while sighting is **additive** time a wetsuit does not reduce.
    """
    leg = geometry.leg(Leg.SWIM)
    metres = leg.distance_m

    pace_max = athlete.swim_threshold_pace * (metres - swim_tbl.D_PRIME_M) / metres
    est_minutes = metres * pace_max / 100.0 / 60.0
    excess = max(0.0, est_minutes - swim_tbl.CSS_VALIDITY_MIN)
    pace_dur = pace_max * (1.0 + swim_tbl.K_SWIM_DUR * excess)

    wetsuit, warning = wetsuit_decision(forecast.water_temp_c)
    pace = pace_dur * swim_tbl.WETSUIT_FACTOR if wetsuit else pace_dur
    pace += swim_tbl.OW_OVERHEAD[athlete.level]
    pace += water_temp_adjustment(forecast.water_temp_c)

    return SwimResult(
        minutes=(metres / 100.0) * pace / 60.0,
        pace_sec_per_100m=pace,
        wetsuit=wetsuit,
        wetsuit_warning=warning,
    )


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def transition_minutes(
    distance: SolverDistance,
    level: AthleteLevel,
    *,
    wetsuit: bool,
    hurry: bool,
) -> tuple[float, float]:
    """``(T1, T2)`` in minutes. Explicit segments with their own durations.

    ``hurry`` applies §3.2's factor, used only for Stage 3's maximum-effort
    profile: an athlete racing a cut-off does move through transition faster,
    but not by an unbounded amount — they still have to find their bag.
    """
    t1 = trans_tbl.T1_BASE_MIN[distance][level]
    if wetsuit:
        t1 += trans_tbl.WETSUIT_REMOVAL_MIN[level]
    t2 = trans_tbl.T2_BASE_MIN[distance][level]

    if hurry:
        factor = intensity_tbl.TRANSITION_HURRY_FACTOR
        return t1 * factor, t2 * factor
    return t1, t2


# ---------------------------------------------------------------------------
# A whole race at one intensity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RaceProfile:
    """Every leg costed at one planned intensity."""

    if_plan: float
    swim: SwimResult
    t1_minutes: float
    bike: BikeResult
    t2_minutes: float
    run: RunResult

    @property
    def total_minutes(self) -> float:
        """Legs accumulate in the fixed order swim, t1, bike, t2, run (§0.4).

        Floating-point addition is not associative, which is why the order is
        specified rather than left to the implementer.
        """
        return (
            self.swim.minutes
            + self.t1_minutes
            + self.bike.minutes
            + self.t2_minutes
            + self.run.minutes
        )

    def elapsed_at(self, leg: Leg, km: float) -> float:
        """Minutes from the start to *km* on *leg*, interpolating in-segment."""
        if leg is Leg.SWIM:
            fraction = _leg_fraction(km, self.swim_distance_km)
            return self.swim.minutes * fraction

        before_bike = self.swim.minutes + self.t1_minutes
        if leg is Leg.BIKE:
            return before_bike + _partial_minutes(self.bike.segments, km)

        before_run = before_bike + self.bike.minutes + self.t2_minutes
        return before_run + _partial_minutes(self.run.segments, km)

    #: Set by :func:`build_profile`; the swim has no segments to interpolate.
    swim_distance_km: float = 0.0


def _leg_fraction(km: float, leg_km: float) -> float:
    if leg_km <= 0:  # pragma: no cover - defensive
        return 1.0
    return clamp(km / leg_km, 0.0, 1.0)


def _partial_minutes(segments: tuple[SegmentResult, ...], km: float) -> float:
    """Cumulative minutes to *km*, interpolating linearly inside a segment.

    Linear interpolation within a segment is the right granularity here: the
    segment already carries its terrain through the histogram, and a barrier
    positioned mid-segment has no finer information to be placed against.
    """
    total = 0.0
    for result in segments:
        segment = result.segment
        if km >= segment.to_km:
            total += result.minutes
            continue
        if km <= segment.from_km:
            break
        span = segment.to_km - segment.from_km
        if span > 0:
            total += result.minutes * (km - segment.from_km) / span
        break
    return total


def build_profile(
    geometry: CourseGeometry,
    athlete: AthleteState,
    forecast: ForecastSnapshot,
    *,
    if_plan: float,
    wbgt_c: float,
    density_pressure_hpa: float | None,
    distance: SolverDistance,
    hurry_transitions: bool = False,
) -> RaceProfile:
    """Cost the whole race at ``if_plan``.

    The realised IF fed to the run's coupling term is the *planned* one, not
    the heat-derated realised power: §4.3.1's ``D_bike`` measures how hard the
    athlete rode relative to their reference band, and heat derating is not
    the athlete riding harder.
    """
    swim = solve_swim(geometry, athlete, forecast)
    t1, t2 = transition_minutes(
        distance, athlete.level, wetsuit=swim.wetsuit, hurry=hurry_transitions
    )
    bike = solve_bike(
        geometry,
        athlete,
        forecast,
        if_plan=if_plan,
        wbgt_c=wbgt_c,
        density_pressure_hpa=density_pressure_hpa,
        distance=distance,
    )
    run = solve_run(
        geometry,
        athlete,
        forecast,
        if_realised=if_plan,
        wbgt_c=wbgt_c,
        distance=distance,
    )
    return RaceProfile(
        if_plan=if_plan,
        swim=swim,
        t1_minutes=t1,
        bike=bike,
        t2_minutes=t2,
        run=run,
        swim_distance_km=geometry.leg(Leg.SWIM).distance_m / 1000.0,
    )
