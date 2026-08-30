"""Cycling power–speed. ``SOLVER_MODEL.md`` §I.2.

Martin et al. (1998), validated at R² = 0.97 with a standard error of 2.7 W.
This is the standard model and there is no serious competitor to it.

The kinetic-energy term is **dropped, deliberately**. Martin's full model
includes ``½(m + I/r²)(v_f² − v_i²)/Δt``; segments here are 5–26 km long and
the course returns to its start, so net ΔKE is a rounding error at segment
resolution. That is why the model is described as *steady-state per segment* —
an explicit simplification, not an omission.
"""

from __future__ import annotations

import math

from raceos.domain.enums import AthleteLevel, BikePosition, HelmetType
from raceos.solver.bind import clamp
from raceos.solver.tables import equipment as eq
from raceos.solver.tables import physics as phys


def cda_for(position: BikePosition, level: AthleteLevel, helmet: HelmetType) -> float:
    """Drag area, m². Driven by ``bike_setup``, never by ``athlete.level`` alone.

    Sanity of the span (§I.2.3): ``experienced + tt_bike + aero`` = 0.225,
    matching the reported "well-optimised age-grouper 0.20–0.23"; ``first +
    road_hoods + standard`` = 0.345, plausible for a nervous first-timer
    sitting up. The table spans the right range at both ends.
    """
    raw = eq.CDA_BASE[position] + eq.CDA_LEVEL_ADJ[level] + eq.CDA_HELMET_ADJ[helmet]
    return clamp(raw, eq.CDA_MIN, eq.CDA_MAX)


def aerodynamic_power(
    density: float,
    cda: float,
    speed_ms: float,
    wind_speed_ms: float,
    wind_relative_rad: float | None,
) -> float:
    """Aerodynamic power, W. Uses the direction-averaged form when needed.

    **Wind of unknown direction always costs time**, and that is worth
    dwelling on because it is both free and correct. Drag is quadratic, so a
    headwind costs more than the equal-and-opposite tailwind saves. Averaging
    ``(v + w·cos φ)²`` over uniformly distributed φ gives ``v² + w²/2``
    exactly, since ``E[cos φ] = 0`` and ``E[cos² φ] = ½`` — and that ``w²/2``
    term *is* the asymmetry.

    A model that set unknown wind to zero would systematically under-predict
    every windy race. Cost: one extra addition inside the bisection. No
    quadrature, no sampling, no loss of determinism.
    """
    area = cda + phys.SPOKE_DRAG_AREA_FW
    if wind_relative_rad is None:
        # Direction unknown: v² + w²/2, exactly.
        squared = speed_ms * speed_ms + (wind_speed_ms * wind_speed_ms) / 2.0
    else:
        air_speed = speed_ms + wind_speed_ms * math.cos(wind_relative_rad)
        squared = air_speed * air_speed
    return 0.5 * density * area * squared * speed_ms


def total_wheel_power(
    speed_ms: float,
    *,
    density: float,
    cda: float,
    crr: float,
    mass_kg: float,
    gradient: float,
    wind_speed_ms: float,
    wind_relative_rad: float | None,
) -> float:
    """Power at the wheel required to hold *speed_ms*, W.

    Four terms: aerodynamic, rolling resistance, gravity, and wheel-bearing
    friction. The drivetrain division happens once, outside, so this is
    genuinely wheel power rather than crank power.
    """
    theta = math.atan(gradient)
    return _wheel_power(
        speed_ms,
        density=density,
        cda=cda,
        crr=crr,
        mass_kg=mass_kg,
        cos_theta=math.cos(theta),
        sin_theta=math.sin(theta),
        wind_speed_ms=wind_speed_ms,
        wind_relative_rad=wind_relative_rad,
    )


def _wheel_power(
    speed_ms: float,
    *,
    density: float,
    cda: float,
    crr: float,
    mass_kg: float,
    cos_theta: float,
    sin_theta: float,
    wind_speed_ms: float,
    wind_relative_rad: float | None,
) -> float:
    """As :func:`total_wheel_power`, with the road angle already resolved.

    ``atan``/``cos``/``sin`` of the gradient are constant for a given histogram
    bin, but the bisection evaluates the balance sixty times — so computing
    them inside the loop repeats three transcendental calls sixty times per
    speed solve, on a path that runs ~110,000 times for one Stage 3 grid over a
    real course. Hoisting them out is exact: identical arithmetic, same
    fixed-iteration bisection, same answer to the last bit.
    """
    aero = aerodynamic_power(density, cda, speed_ms, wind_speed_ms, wind_relative_rad)
    rolling = crr * mass_kg * phys.GRAVITY * cos_theta * speed_ms
    gravity = mass_kg * phys.GRAVITY * sin_theta * speed_ms
    bearings = speed_ms * (phys.BEARING_C0 + phys.BEARING_C1 * speed_ms) * 1e-3
    return aero + rolling + gravity + bearings


def solve_speed(
    power_w: float,
    *,
    density: float,
    cda: float,
    crr: float,
    mass_kg: float,
    gradient: float,
    wind_speed_ms: float,
    wind_relative_rad: float | None = None,
) -> float:
    """Ground speed for a given crank power, m·s⁻¹. §I.2.4.

    **Fixed-iteration bisection, never tolerance-terminated.** A tolerance test
    is a platform-dependent branch and would break the byte-identical
    guarantee; sixty halvings of a 29.5 m·s⁻¹ bracket leave a residual interval
    below float64 resolution, so the result is exact and identical everywhere.

    **Bisection rather than Newton is mandatory.** Newton diverges on steep
    descents, where the gravity term makes the power function non-monotonic
    near the lower bracket bound — observed during modelling, where Newton
    returned 1.8 km·h⁻¹ for a −4.5% descent that bisection solves at 61.8.

    ``power_w`` is floored at zero: on a steep descent the modulated target may
    go negative, which means freewheeling, not braking-as-power.
    """
    wheel_power = max(0.0, power_w) * phys.ETA_DRIVETRAIN

    # Everything that does not vary with speed is computed once. The
    # bisection body then costs four multiplications and three additions
    # instead of three transcendental calls and a function call, and it runs
    # 60 times per solve on a path that executes ~110,000 times for one
    # Stage 3 grid over a real 18,000-node course.
    #
    # **The grouping preserves the original evaluation order exactly**, so the
    # arithmetic is bit-for-bit what `_wheel_power` computes — verified by the
    # golden suite being unchanged, which is the only proof that matters for a
    # byte-identical guarantee.
    theta = math.atan(gradient)
    k_aero = 0.5 * density * (cda + phys.SPOKE_DRAG_AREA_FW)
    k_roll = crr * mass_kg * phys.GRAVITY * math.cos(theta)
    k_grav = mass_kg * phys.GRAVITY * math.sin(theta)
    if wind_relative_rad is None:
        half_wind_squared = (wind_speed_ms * wind_speed_ms) / 2.0
        wind_component = None
    else:
        half_wind_squared = 0.0
        wind_component = wind_speed_ms * math.cos(wind_relative_rad)

    lo = phys.SPEED_BRACKET_LO
    hi = phys.SPEED_BRACKET_HI
    for _ in range(phys.BISECTION_ITERATIONS):
        mid = (lo + hi) / 2.0
        if wind_component is None:
            squared = mid * mid + half_wind_squared
        else:
            air_speed = mid + wind_component
            squared = air_speed * air_speed
        required = (
            k_aero * squared * mid
            + k_roll * mid
            + k_grav * mid
            + mid * (phys.BEARING_C0 + phys.BEARING_C1 * mid) * 1e-3
        )
        if required < wheel_power:
            lo = mid
        else:
            hi = mid

    return min((lo + hi) / 2.0, phys.V_DESCENT_MAX)


def relative_wind_angle(wind_dir_deg: float | None, bearing_rad: float) -> float | None:
    """``θ_wind − bearing`` in radians, or ``None`` when direction is unknown."""
    if wind_dir_deg is None:
        return None
    return math.radians(wind_dir_deg) - bearing_rad
