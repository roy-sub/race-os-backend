"""Stage 5 — Balance fuelling (~0.4 s). ``SOLVER_MODEL.md`` §5.

Every quantity here is bound through :func:`~raceos.solver.bind.bind`, never
through ``min()``/``max()``, so each one can name the limit that produced it.
That is not decoration: §5.6's worked example turns on it. Athlete M's sweat
rate implies 1140 mL·h⁻¹, but the gastric cap is 1000 — **the athlete cannot
absorb their own sweat rate**, and the "Why this?" drawer must name the gastric
cap rather than the sweat rate. That is a genuine and common long-course
situation, and naming it correctly is the point.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

from raceos.domain.enums import BindDirection, Leg
from raceos.solver.bind import Candidate, bind, clamp
from raceos.solver.environment import wbgt
from raceos.solver.models import AidAction, AidStation
from raceos.solver.profile import RaceProfile
from raceos.solver.stages.s2_athlete import AthleteState
from raceos.solver.tables import fuelling as fuel_tbl


def carb_duration_target(hours: float) -> float:
    """``L(t)``, the duration-based literature target (§5.1).

    Piecewise-linear and continuous by construction: ``L(1) = 30``,
    ``L(2) = 60``, ``L(2.5) = 60``, ``L(4) = 90``. Held flat outside the knots.
    """
    knots = fuel_tbl.CARB_DURATION_KNOTS
    if hours <= knots[0][0]:
        return knots[0][1]
    if hours >= knots[-1][0]:
        return knots[-1][1]
    for (x0, y0), (x1, y1) in pairwise(knots):
        if x0 <= hours <= x1:
            if x1 == x0:  # pragma: no cover - knots are strictly increasing
                return y1
            return y0 + (y1 - y0) * (hours - x0) / (x1 - x0)
    raise AssertionError("unreachable: knots are sorted and bracketed above")


@dataclass(frozen=True)
class FuellingResult:
    carb_g_per_hr: float
    fluid_ml_per_hr: float
    sodium_mg_per_hr: float
    caffeine_mg_total: float
    total_carb_g: float
    duration_hours: float
    overridden: bool
    requires_multiple_transportable: bool
    binding_carb_key: str
    binding_fluid_key: str
    binding_sodium_key: str
    binding_caffeine_key: str
    sweat_effective_l_per_hr: float
    #: Emitted as an ``OVER_CEILING`` warning by the API when true.
    override_above_ceiling: bool


def _sweat_reference_wbgt(athlete: AthleteState) -> float:
    """The WBGT the athlete's sweat test is assumed to have been run at.

    When ``measured_at_temp_c`` is supplied (§F.4) it is converted onto the
    WBGT axis using two further defaults — a sweat test records a temperature
    but almost never a humidity or a sky state. Those two are themselves
    estimates; the gain is that the *temperature*, the term that dominates,
    becomes a measurement instead of an assumption.
    """
    if athlete.sweat_measured_at_temp_c is None:
        return fuel_tbl.W_SWEAT_REF
    return wbgt(
        athlete.sweat_measured_at_temp_c,
        fuel_tbl.SWEAT_TEST_DEFAULT_RH,
        fuel_tbl.SWEAT_TEST_DEFAULT_CONDITIONS,
    )


def solve_fuelling(
    profile: RaceProfile,
    athlete: AthleteState,
    *,
    wbgt_c: float,
    carb_override: float | None,
) -> FuellingResult:
    """Stage 5.

    ``duration_hours`` is bike + run **moving time only** — not swim, not
    transitions. Carbohydrate is not ingested at a meaningful rate during a
    swim, and including it would inflate ``total_carb_g`` beyond what the plan
    actually asks the athlete to consume.
    """
    duration_hours = (profile.bike.minutes + profile.run.minutes) / 60.0

    # --- carbohydrate ------------------------------------------------
    target = carb_duration_target(duration_hours)
    override_above_ceiling = False
    if carb_override is not None:
        # An override is a statement that the athlete knows their gut better
        # than the stored constraint. It is not a statement that they have
        # repealed intestinal transport, so it cannot exceed the hard maximum.
        override_above_ceiling = carb_override > athlete.gut_carb_ceiling
        clamped = min(carb_override, fuel_tbl.CARB_HARD_MAX)
        carb = clamped
        carb_key = (
            "model:carb_hard_max"
            if carb_override > fuel_tbl.CARB_HARD_MAX
            else "options:carb_override"
        )
        overridden = True
    else:
        bound = bind(
            (
                Candidate("model:carb_duration_target", target, BindDirection.UPPER),
                Candidate("gut_carb_ceiling", athlete.gut_carb_ceiling, BindDirection.UPPER),
            )
        )
        carb = bound.value
        carb_key = bound.binding_key
        overridden = False

    # --- fluid -------------------------------------------------------
    reference = _sweat_reference_wbgt(athlete)
    sweat_effective = athlete.sweat_rate * (1.0 + fuel_tbl.K_SWEAT * max(0.0, wbgt_c - reference))
    fluid_bound = bind(
        (
            Candidate(
                "sweat_rate",
                sweat_effective * 1000.0 * fuel_tbl.REPLACE_FRAC,
                BindDirection.UPPER,
            ),
            Candidate("model:gastric_emptying_cap", fuel_tbl.GASTRIC_CAP_ML, BindDirection.UPPER),
        )
    )

    # --- sodium ------------------------------------------------------
    # `sodium_loss` is a CONCENTRATION (mg per litre of sweat), so it must be
    # multiplied by sweat *volume* to yield a rate. Confusing the two produces
    # a number about 1.5x wrong and has its own unit test.
    from_losses = athlete.sodium_loss * sweat_effective * fuel_tbl.REPLACE_FRAC_NA
    acsm_floor = fuel_tbl.ACSM_MIN_G_PER_L * (fluid_bound.value / 1000.0) * 1000.0
    sodium_bound = bind(
        (
            Candidate("sodium_loss", from_losses, BindDirection.LOWER),
            Candidate("model:acsm_sodium_floor", acsm_floor, BindDirection.LOWER),
        )
    )
    sodium = clamp(sodium_bound.value, fuel_tbl.SODIUM_MIN, fuel_tbl.SODIUM_MAX)

    # --- caffeine ----------------------------------------------------
    caffeine_bound = bind(
        (
            Candidate(
                "model:caffeine_dose_per_kg",
                fuel_tbl.CAFFEINE_MG_PER_KG * athlete.weight,
                BindDirection.UPPER,
            ),
            Candidate("caffeine_tolerance", athlete.caffeine_tolerance, BindDirection.UPPER),
        )
    )

    # `total_carb_g` is computed from the UNROUNDED rate and UNROUNDED
    # duration, then rounded once (§0.4). Computing it from the rounded rate
    # is what makes the arithmetic-consistency invariant fail on long races.
    total_carb = carb * duration_hours

    return FuellingResult(
        carb_g_per_hr=carb,
        fluid_ml_per_hr=fluid_bound.value,
        sodium_mg_per_hr=sodium,
        caffeine_mg_total=caffeine_bound.value,
        total_carb_g=total_carb,
        duration_hours=duration_hours,
        overridden=overridden,
        requires_multiple_transportable=carb > fuel_tbl.CARB_SINGLE_TRANSPORTER_MAX,
        binding_carb_key=carb_key,
        binding_fluid_key=fluid_bound.binding_key,
        binding_sodium_key=sodium_bound.binding_key,
        binding_caffeine_key=caffeine_bound.binding_key,
        sweat_effective_l_per_hr=sweat_effective,
        override_above_ceiling=override_above_ceiling,
    )


def caffeine_schedule_minutes(profile: RaceProfile) -> tuple[float, float, float]:
    """Clock minutes of the three doses, relative to the start (§5.4).

    The pre-start dose is negative by construction: the ISSN's "most commonly
    used timing of 60 min pre-exercise", pulled forward to account for the swim.
    """
    bike_start = profile.swim.minutes + profile.t1_minutes
    return (
        -fuel_tbl.CAFFEINE_PRE_START_MIN,
        bike_start + profile.bike.minutes * fuel_tbl.CAFFEINE_BIKE_FRACTION,
        bike_start + profile.bike.minutes + profile.t2_minutes,
    )


def build_aid_actions(
    profile: RaceProfile,
    fuelling: FuellingResult,
    aid_stations: tuple[AidStation, ...],
) -> tuple[AidAction, ...]:
    """One action per aid station from the bundle, in course order (§5.5).

    **Never a synthesised station** (§1.2). If a leg has none, it gets none —
    the model does not invent one at a plausible interval.
    """
    relevant = [station for station in aid_stations if station.leg in (Leg.BIKE, Leg.RUN)]
    # Course order: bike before run, then ascending km. A fixed sort key, not
    # the bundle's incidental ordering.
    relevant.sort(key=lambda s: (0 if s.leg is Leg.BIKE else 1, s.km))

    bike_start = profile.swim.minutes + profile.t1_minutes
    run_start = bike_start + profile.bike.minutes + profile.t2_minutes

    actions: list[AidAction] = []
    for ordinal, station in enumerate(relevant, start=1):
        clock = profile.elapsed_at(station.leg, station.km)
        moving_start = bike_start if station.leg is Leg.BIKE else run_start
        # Moving hours exclude the swim and both transitions, matching the
        # basis `carb_g_per_hr` was computed on.
        moving_hours = max(0.0, clock - moving_start) / 60.0
        if station.leg is Leg.RUN:
            moving_hours += profile.bike.minutes / 60.0

        actions.append(
            AidAction(
                ordinal=ordinal,
                leg=station.leg,
                at_clock_minutes=clock,
                at_km=station.km,
                station_name=station.name,
                action_text=_action_text(fuelling, station),
                cumulative_carb_g=fuelling.carb_g_per_hr * moving_hours,
            )
        )
    return tuple(actions)


def _action_text(fuelling: FuellingResult, station: AidStation) -> str:
    """Deterministic template prose. Every correct number is present.

    ``PHRASING_ENABLED`` is false in V1, so this is the shipping path — plainer
    prose, not degraded data.
    """
    parts = [f"{fuelling.carb_g_per_hr:.0f} g carb"]
    if "water" in station.contents or "sports_drink" in station.contents:
        parts.append(f"{fuelling.fluid_ml_per_hr:.0f} ml fluid")
    parts.append(f"{fuelling.sodium_mg_per_hr:.0f} mg sodium")
    return "Take " + ", ".join(parts) + " per hour."


def assert_carb_consistency(
    fuelling: FuellingResult, rounded_rate: int, rounded_total: int
) -> None:
    """The contract's arithmetic-consistency invariant, as a postcondition.

    The tolerance exists solely to absorb the single rounding of the rate to
    1 g. **A failure larger than that means a rounded value leaked into a
    computation** — exactly the bug §0.4 exists to prevent — so it raises
    rather than warns.
    """
    implied = rounded_rate * fuelling.duration_hours
    if abs(rounded_total - implied) > fuel_tbl.CARB_CONSISTENCY_TOLERANCE_G + math.ceil(
        fuelling.duration_hours
    ):
        raise AssertionError(
            f"carb arithmetic inconsistent: total {rounded_total} g against "
            f"{rounded_rate} g/h x {fuelling.duration_hours:.4f} h = {implied:.2f} g"
        )
