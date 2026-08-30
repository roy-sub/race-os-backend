"""The environment model. ``SOLVER_MODEL.md`` §I.1.

Air density, psychrometric wet-bulb temperature, WBGT, the two heat curves,
altitude, and solar position. Stages 3 through 6 all consume these, which is
why they are specified once here rather than duplicated five times.

Everything in this module is a pure function of its arguments. No clock, no
randomness, no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

from raceos.domain.enums import AthleteLevel
from raceos.solver.bind import clamp
from raceos.solver.tables import heat_curve as hc
from raceos.solver.tables import physics as phys

# ---------------------------------------------------------------------------
# §I.1.1 Air density
# ---------------------------------------------------------------------------


def isa_pressure(elevation_m: float, sea_level_pa: float = phys.P0_PA) -> float:
    """Pressure at *elevation_m*, from the International Standard Atmosphere.

    When ``forecast.pressure_hpa`` is supplied it replaces the standard
    sea-level term (§F.3). Providers almost always report pressure already
    reduced to sea level (QNH), in which case this collapses to the ISA formula
    with the measured pressure substituted — which is exactly what this
    signature expresses. **The adapter must confirm its provider's
    convention:** station pressure passed as sea-level pressure is wrong by
    roughly 1.2% per 100 m of elevation.
    """
    height = clamp(elevation_m, phys.ELEVATION_MIN_M, phys.ELEVATION_MAX_M)
    ratio = 1.0 - (phys.LAPSE_RATE_K_PER_M * height) / phys.ISA_SEA_LEVEL_TEMP_K
    return float(sea_level_pa * ratio**phys.ISA_EXPONENT)


def saturation_vapour_pressure(temp_c: float) -> float:
    """Tetens. Pascals."""
    return phys.TETENS_A * math.exp((phys.TETENS_B * temp_c) / (temp_c + phys.TETENS_C))


def air_density(
    temp_c: float,
    humidity_pct: float,
    elevation_m: float,
    pressure_hpa: float | None = None,
) -> float:
    """Moist-air density, kg·m⁻³.

    Aerodynamic drag scales linearly with density, and density moves by ~11%
    between a cold sea-level morning and a hot mountain afternoon, so this is
    not a refinement.

    The humidity correction is small and moves in the opposite direction to
    intuition — **moist air is *less* dense than dry air**, by about 0.6% at
    31 °C and 55% RH — but it is free to include.
    """
    temp = clamp(temp_c, phys.TEMP_MIN_C, phys.TEMP_MAX_C)
    humidity = clamp(humidity_pct, phys.HUMIDITY_MIN_PCT, phys.HUMIDITY_MAX_PCT)

    sea_level_pa = pressure_hpa * 100.0 if pressure_hpa is not None else phys.P0_PA
    total = isa_pressure(elevation_m, sea_level_pa)

    vapour = (humidity / 100.0) * saturation_vapour_pressure(temp)
    dry = total - vapour
    kelvin = temp + 273.15

    density = dry / (phys.R_DRY_AIR * kelvin) + vapour / (phys.R_WATER_VAPOUR * kelvin)

    # Outside this band is a programming error, not an input error (§I.1.1).
    if not phys.AIR_DENSITY_MIN <= density <= phys.AIR_DENSITY_MAX:
        raise AssertionError(
            f"air density {density:.4f} kg/m3 outside "
            f"[{phys.AIR_DENSITY_MIN}, {phys.AIR_DENSITY_MAX}] for "
            f"T={temp_c} RH={humidity_pct} h={elevation_m}"
        )
    return density


def pressure_hpa_or_none(pressure_hpa: float | None) -> float | None:
    """Range-check ``forecast.pressure_hpa``; outside the band it is absent."""
    if pressure_hpa is None:
        return None
    if phys.PRESSURE_HPA_MIN <= pressure_hpa <= phys.PRESSURE_HPA_MAX:
        return pressure_hpa
    return None


# ---------------------------------------------------------------------------
# §I.1.2 Psychrometric wet-bulb temperature
# ---------------------------------------------------------------------------


def wet_bulb_temp(temp_c: float, humidity_pct: float) -> float:
    """Stull (2011). °C.

    **Every ``atan`` here is in radians.** Using degrees is the single most
    common implementation error with this formula, and it silently produces
    plausible-looking numbers rather than an obvious failure.
    """
    temp = clamp(temp_c, phys.TEMP_MIN_C, phys.TEMP_MAX_C)
    rh = clamp(humidity_pct, phys.HUMIDITY_MIN_PCT, phys.HUMIDITY_MAX_PCT)
    c1, c2, c3, c4, c5, c6 = hc.STULL_COEFFS

    return float(
        temp * math.atan(c1 * math.sqrt(rh + c2))
        + math.atan(temp + rh)
        - math.atan(rh - c3)
        + c4 * rh**hc.STULL_RH_EXPONENT * math.atan(c5 * rh)
        - c6
    )


# ---------------------------------------------------------------------------
# §I.1.3 Wet Bulb Globe Temperature
# ---------------------------------------------------------------------------


def cloud_fraction_for(conditions: str, cloud_cover_pct: float | None) -> float:
    """Cloud fraction, preferring the numeric field when it is supplied.

    Supplying ``cloud_cover_pct`` (§F.3) removes the last discontinuity from
    the environment model. Under the categorical fallback the input is
    genuinely discrete: a forecast moving from ``clear`` to ``partly_cloudy``
    moves WBGT by 0.46 °C and a mid-level run pace by roughly 1.5 s·km⁻¹, with
    no intermediate values.
    """
    if cloud_cover_pct is not None:
        return clamp(cloud_cover_pct, 0.0, 100.0) / 100.0
    try:
        return hc.CLOUD_FRACTION[conditions]
    except KeyError:
        raise ValueError(
            f"unknown forecast conditions {conditions!r}; expected one of "
            f"{sorted(hc.CLOUD_FRACTION)}"
        ) from None


def globe_offset(cloud_fraction: float) -> float:
    """Linear interpolation between the clear-sky and overcast offsets."""
    return (
        hc.GLOBE_OFFSET_CLEAR_C * (1.0 - cloud_fraction)
        + hc.GLOBE_OFFSET_OVERCAST_C * cloud_fraction
    )


def wbgt(
    temp_c: float,
    humidity_pct: float,
    conditions: str,
    cloud_cover_pct: float | None = None,
) -> float:
    """Wet Bulb Globe Temperature, °C.

    The heat-stress index both heat curves are expressed in. **Not dry-bulb
    temperature, and typically several degrees below it.**

    Wind substantially reduces globe temperature and increases evaporative
    cooling, and is **not modelled here** (D-5): a hot windy day is genuinely
    less stressful than a hot still day, so this over-states heat cost on windy
    days. That is the opposite sign to D-1's bike-duration gap, but the two do
    not reliably cancel.
    """
    t_wet = wet_bulb_temp(temp_c, humidity_pct)
    t_globe = temp_c + globe_offset(cloud_fraction_for(conditions, cloud_cover_pct))
    return hc.WBGT_W_WET * t_wet + hc.WBGT_W_GLOBE * t_globe + hc.WBGT_W_DRY * temp_c


def wbgt_indoor(temp_c: float, humidity_pct: float) -> float:
    """WBGT with no radiant load, ``T_g = T``.

    The axis Peiffer's laboratory data is converted onto (§I.1.4).
    """
    return (
        hc.WBGT_W_WET * wet_bulb_temp(temp_c, humidity_pct)
        + (hc.WBGT_W_GLOBE + hc.WBGT_W_DRY) * temp_c
    )


# ---------------------------------------------------------------------------
# §I.1.4 Heat effect on cycling power
# ---------------------------------------------------------------------------


def bike_heat_factor(wbgt_c: float) -> float:
    """Multiplier on target bike power. Linear between knots, **flat outside**.

    The flat clamp above the top knot is a deliberate refusal to extrapolate,
    and it is the safe direction: under-stating the bike penalty means the
    model plans slightly less power reduction, and the run model — anchored
    well past this range — carries the consequence.
    """
    knots = hc.BIKE_HEAT_KNOTS
    if wbgt_c <= knots[0][0]:
        return knots[0][1]
    if wbgt_c >= knots[-1][0]:
        return knots[-1][1]
    for (x0, y0), (x1, y1) in pairwise(knots):
        if x0 <= wbgt_c <= x1:
            span = x1 - x0
            return y0 + (y1 - y0) * (wbgt_c - x0) / span
    raise AssertionError("unreachable: knots are sorted and bracketed above")


# ---------------------------------------------------------------------------
# §I.1.5 Heat effect on running
# ---------------------------------------------------------------------------


def run_heat_coefficient(level: AthleteLevel) -> float:
    """``k_level = pct_at_15[level] / 15 ^ p_heat``.

    The 15 is the WBGT span Ely's anchors are quoted over — 10 °C to 25 °C —
    so this normalises the reported percentage onto the exponent's scale. It
    is a property of the source data rather than a tunable, which is why it is
    written here rather than added to the table.
    """
    span = hc.RUN_HEAT_PCT_AT_15[level]
    return float(span / (15.0**hc.RUN_HEAT_EXPONENT))


@dataclass(frozen=True)
class RunHeat:
    factor: float
    clamped: bool


def run_heat_factor(wbgt_c: float, level: AthleteLevel) -> RunHeat:
    """Multiplier on run pace (larger is slower).

    Anchored to Ely et al. (2007). When the clamp binds, the caller emits
    ``model:run_heat_clamp`` as the binding key and the plan should be treated
    as advisory: beyond +60% the model is far outside any data, and the honest
    output is a warning rather than a number.
    """
    excess = max(0.0, wbgt_c - hc.RUN_HEAT_WBGT_BASELINE_C)
    raw = float(1.0 + run_heat_coefficient(level) * excess**hc.RUN_HEAT_EXPONENT)
    if raw > hc.RUN_HEAT_FACTOR_MAX:
        return RunHeat(factor=hc.RUN_HEAT_FACTOR_MAX, clamped=True)
    return RunHeat(factor=raw, clamped=False)


# ---------------------------------------------------------------------------
# §I.1.6 Altitude
# ---------------------------------------------------------------------------


def alt_factor(elevation_m: float) -> float:
    """Aerobic derate at altitude. Continuous at the breakpoint by construction.

    Applied as a **multiplier** to ``bike_threshold_power`` and as a
    **divisor** to running speed. Uses the *mean elevation of the leg*, not
    per-segment: per-segment would imply the athlete's aerobic ceiling changes
    within a single climb, which is not how acclimatisation state works.
    """
    height = max(0.0, elevation_m)
    below = min(height, hc.ALT_BREAKPOINT_M) / 1000.0
    above = max(0.0, height - hc.ALT_BREAKPOINT_M) / 1000.0
    return 1.0 - hc.ALT_A1 * below - hc.ALT_A2 * above


# ---------------------------------------------------------------------------
# §I.1.7 Solar position — sunset and civil dusk
# ---------------------------------------------------------------------------


def _julian_day(year: int, month: int, day: int) -> float:
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1)) + day + b - 1524.5


def solar_event_minutes(
    year: int,
    month: int,
    day: int,
    lat: float,
    lng_east: float,
    utc_offset_hours: float,
    zenith_deg: float,
) -> float | None:
    """Local clock minutes of the evening solar event at *zenith_deg*.

    Standard NOAA solar-position algorithm; entirely deterministic and
    closed-form.

    **Longitude is positive east**, and *utc_offset_hours* is the offset in
    effect on the event date, including summer time. Getting either sign wrong
    silently returns sunrise instead of sunset — observed during modelling,
    which is why both hemispheres have an explicit test.

    Returns ``None`` when the sun does not reach that altitude on that date at
    that latitude (polar day or night). The caller then falls back to
    ``options.night_flag``. No RaceOS course is above the Arctic Circle, but
    the branch must exist rather than raise.
    """
    jd = _julian_day(year, month, day) + 0.5
    t = (jd - 2451545.0) / 36525.0

    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    m_rad = math.radians(mean_anom)
    centre = (
        math.sin(m_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m_rad) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m_rad) * 0.000289
    )

    omega = 125.04 - 1934.136 * t
    apparent_long = mean_long + centre - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    obliquity0 = (
        23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    )
    obliquity = obliquity0 + 0.00256 * math.cos(math.radians(omega))

    declination = math.asin(
        math.sin(math.radians(obliquity)) * math.sin(math.radians(apparent_long))
    )

    y_term = math.tan(math.radians(obliquity / 2.0)) ** 2
    l0_rad = math.radians(mean_long)
    eq_time = 4.0 * math.degrees(
        y_term * math.sin(2 * l0_rad)
        - 2 * eccentricity * math.sin(m_rad)
        + 4 * eccentricity * y_term * math.sin(m_rad) * math.cos(2 * l0_rad)
        - 0.5 * y_term * y_term * math.sin(4 * l0_rad)
        - 1.25 * eccentricity * eccentricity * math.sin(2 * m_rad)
    )

    solar_noon = 720.0 - 4.0 * lng_east - eq_time + utc_offset_hours * 60.0

    lat_rad = math.radians(lat)
    cos_ha = math.cos(math.radians(zenith_deg)) / (
        math.cos(lat_rad) * math.cos(declination)
    ) - math.tan(lat_rad) * math.tan(declination)

    if abs(cos_ha) > 1.0:
        return None

    hour_angle_deg = math.degrees(math.acos(cos_ha))
    return solar_noon + 4.0 * hour_angle_deg


def civil_dusk_minutes(
    year: int, month: int, day: int, lat: float, lng_east: float, utc_offset_hours: float
) -> float | None:
    """Civil dusk in local clock minutes. What the head-torch rule uses."""
    return solar_event_minutes(
        year, month, day, lat, lng_east, utc_offset_hours, hc.DUSK_ZENITH_DEG
    )


def sunset_minutes(
    year: int, month: int, day: int, lat: float, lng_east: float, utc_offset_hours: float
) -> float | None:
    """Sunset in local clock minutes. Reference only; Stage 6 uses civil dusk."""
    return solar_event_minutes(
        year, month, day, lat, lng_east, utc_offset_hours, hc.SUNSET_ZENITH_DEG
    )
