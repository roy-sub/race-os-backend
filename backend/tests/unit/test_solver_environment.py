"""The environment and cycling models, against the document's own arithmetic.

``SOLVER_MODEL.md`` gives fully worked numeric examples precisely so an
implementer can check their code against them — §0.1: "a fully worked numeric
example whose arithmetic an implementer can check their code against". This
file is that check, turned into a regression test.

These are **not** the golden cases. The golden suite pins whole-solve outputs
captured from the first correct run; these pin the individual formulas against
figures the document states independently. A formula that drifts here is wrong
against the specification, not merely different from last time.
"""

from __future__ import annotations

import math

import pytest

from raceos.domain.enums import AthleteLevel, BikePosition, HelmetType
from raceos.solver.cycling import cda_for, solve_speed, total_wheel_power
from raceos.solver.environment import (
    air_density,
    alt_factor,
    bike_heat_factor,
    civil_dusk_minutes,
    run_heat_factor,
    sunset_minutes,
    wbgt,
    wbgt_indoor,
    wet_bulb_temp,
)
from raceos.solver.tables import heat_curve as hc
from raceos.solver.tables import physics as phys

# ---------------------------------------------------------------------------
# §I.1.2 — Stull wet-bulb temperature
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("temp_c", "humidity", "expected"),
    [
        (31.0, 55.0, 24.041),
        (20.0, 55.0, 14.36),
        (10.0, 60.0, 6.02),
    ],
)
def test_wet_bulb_matches_the_documents_worked_values(
    temp_c: float, humidity: float, expected: float
) -> None:
    assert wet_bulb_temp(temp_c, humidity) == pytest.approx(expected, abs=0.01)


def test_stull_atan_terms_are_in_radians() -> None:
    """Using degrees is the single most common error with this formula.

    It fails silently — the result is a plausible-looking number, not an
    exception — so it needs its own assertion rather than being caught by the
    value checks above passing "close enough".
    """
    # In radians, T_w(31, 55) = 24.04. In degrees it lands nowhere near.
    assert wet_bulb_temp(31.0, 55.0) == pytest.approx(24.041, abs=0.01)
    c1, c2, *_ = hc.STULL_COEFFS
    # The first term alone is ~1.31 rad; in degrees it would be ~75.
    assert math.atan(c1 * math.sqrt(55.0 + c2)) < math.pi / 2


# ---------------------------------------------------------------------------
# §I.1.3 — WBGT
# ---------------------------------------------------------------------------


def test_wbgt_reproduces_the_worked_example() -> None:
    """31 °C, 55% RH, clear -> T_w 24.041, T_g 39.0, WBGT 27.729."""
    assert wbgt(31.0, 55.0, "clear") == pytest.approx(27.729, abs=0.002)


def test_wbgt_is_well_below_dry_bulb() -> None:
    """WBGT is not dry-bulb temperature, and confusing them is consequential."""
    assert wbgt(31.0, 55.0, "clear") < 31.0


def test_cloud_cover_pct_overrides_the_categorical_mapping() -> None:
    """§F.3: supplying the numeric field removes the last discontinuity."""
    categorical = wbgt(25.0, 50.0, "partly_cloudy")
    numeric = wbgt(25.0, 50.0, "partly_cloudy", cloud_cover_pct=35.0)
    assert numeric == pytest.approx(categorical, abs=1e-9)
    # And an intermediate value that no category can express.
    between = wbgt(25.0, 50.0, "partly_cloudy", cloud_cover_pct=20.0)
    assert wbgt(25.0, 50.0, "cloudy") < between < wbgt(25.0, 50.0, "clear")


def test_unknown_conditions_are_refused_not_defaulted() -> None:
    with pytest.raises(ValueError, match="unknown forecast conditions"):
        wbgt(20.0, 50.0, "hurricane")


# ---------------------------------------------------------------------------
# §I.1.4 — bike heat curve
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ambient", "expected_wbgt"),
    [(17.0, 12.016), (22.0, 16.361), (27.0, 20.707), (32.0, 25.053)],
)
def test_peiffer_knots_sit_where_the_document_places_them(
    ambient: float, expected_wbgt: float
) -> None:
    """The knots are the lab data converted onto the WBGT axis at 40% RH."""
    assert wbgt_indoor(ambient, hc.PEIFFER_LAB_RH) == pytest.approx(expected_wbgt, abs=0.002)


def test_bike_heat_is_held_flat_above_the_top_knot() -> None:
    """A deliberate refusal to extrapolate (§I.1.4).

    A power law through the top two knots would predict −10.1% at WBGT 27.7,
    for which there is no evidence — the data stops at 32 °C ambient.
    """
    top_wbgt, top_factor = hc.BIKE_HEAT_KNOTS[-1]
    assert bike_heat_factor(27.729) == pytest.approx(top_factor)
    assert bike_heat_factor(top_wbgt + 50.0) == pytest.approx(top_factor)


def test_bike_heat_is_held_flat_below_the_first_knot() -> None:
    first_wbgt, first_factor = hc.BIKE_HEAT_KNOTS[0]
    assert bike_heat_factor(first_wbgt - 20.0) == pytest.approx(first_factor)


def test_bike_heat_interpolates_linearly_between_knots() -> None:
    (x0, y0), (x1, y1) = hc.BIKE_HEAT_KNOTS[1], hc.BIKE_HEAT_KNOTS[2]
    midpoint = (x0 + x1) / 2.0
    assert bike_heat_factor(midpoint) == pytest.approx((y0 + y1) / 2.0)


# ---------------------------------------------------------------------------
# §I.1.5 — run heat curve
# ---------------------------------------------------------------------------


def test_run_heat_reproduces_the_worked_example() -> None:
    """W = 27.72875, improver -> 1.161550, i.e. +16.2%."""
    result = run_heat_factor(27.72875, AthleteLevel.IMPROVER)
    assert result.factor == pytest.approx(1.161550, abs=1e-5)
    assert result.clamped is False


def test_slower_athletes_are_hurt_more_by_heat() -> None:
    """Ely's finding, and the reason all three levels sit at or above 10%."""
    hot = 27.7
    first = run_heat_factor(hot, AthleteLevel.FIRST).factor
    improver = run_heat_factor(hot, AthleteLevel.IMPROVER).factor
    experienced = run_heat_factor(hot, AthleteLevel.EXPERIENCED).factor
    assert first > improver > experienced > 1.0


def test_no_heat_penalty_below_the_baseline() -> None:
    assert run_heat_factor(hc.RUN_HEAT_WBGT_BASELINE_C, AthleteLevel.FIRST).factor == 1.0
    assert run_heat_factor(0.0, AthleteLevel.FIRST).factor == 1.0


def test_run_heat_clamp_binds_and_says_so() -> None:
    """Beyond +60% the honest output is a warning, not a number."""
    result = run_heat_factor(60.0, AthleteLevel.FIRST)
    assert result.factor == hc.RUN_HEAT_FACTOR_MAX
    assert result.clamped is True


# ---------------------------------------------------------------------------
# §I.1.1 / §I.1.6
# ---------------------------------------------------------------------------


def test_air_density_reproduces_the_worked_example() -> None:
    """rho(31 °C, 55% RH, 120 m) = 1.13342 kg/m3."""
    assert air_density(31.0, 55.0, 120.0) == pytest.approx(1.13342, abs=2e-5)


def test_moist_air_is_less_dense_than_dry_air() -> None:
    """Small, free to include, and it moves against intuition."""
    assert air_density(31.0, 55.0, 0.0) < air_density(31.0, 5.0, 0.0)


def test_altitude_factor_is_continuous_at_the_breakpoint() -> None:
    """The two pieces meet at 1500 m by construction."""
    below = alt_factor(hc.ALT_BREAKPOINT_M - 1e-6)
    above = alt_factor(hc.ALT_BREAKPOINT_M + 1e-6)
    assert below == pytest.approx(above, abs=1e-9)


def test_altitude_factor_matches_the_worked_example() -> None:
    assert alt_factor(120.0) == pytest.approx(0.99880, abs=1e-9)


# ---------------------------------------------------------------------------
# §I.1.7 — solar position
# ---------------------------------------------------------------------------


def test_dusk_matches_the_worked_example_to_the_minute() -> None:
    """2026-09-19, 39.85 N, 3.12 E, UTC+2: sunset 19:50, civil dusk 20:17.

    The document cross-checks this against Mallorca in mid-September and calls
    it correct to the minute.
    """
    sunset = sunset_minutes(2026, 9, 19, 39.85, 3.12, 2.0)
    dusk = civil_dusk_minutes(2026, 9, 19, 39.85, 3.12, 2.0)
    assert sunset is not None
    assert dusk is not None
    assert round(sunset) == 19 * 60 + 50
    assert round(dusk) == 20 * 60 + 17


def test_dusk_works_in_the_southern_hemisphere() -> None:
    """Getting the longitude sign or the offset wrong returns *sunrise*.

    That was observed during modelling, and it fails silently — which is why
    both hemispheres get an explicit test rather than one.
    """
    # Puerto Varas, Chile in November: a long evening, well after noon.
    dusk = civil_dusk_minutes(2026, 11, 15, -41.32, -72.98, -3.0)
    assert dusk is not None
    assert 20 * 60 < dusk < 23 * 60


def test_polar_case_returns_none_rather_than_raising() -> None:
    """No RaceOS course is above the Arctic Circle, but the branch must exist."""
    assert civil_dusk_minutes(2026, 6, 21, 78.0, 15.0, 2.0) is None


# ---------------------------------------------------------------------------
# §I.2 — cycling power–speed
# ---------------------------------------------------------------------------


def test_cda_for_athlete_m() -> None:
    """tt_bike + improver + standard = 0.255."""
    assert cda_for(BikePosition.TT_BIKE, AthleteLevel.IMPROVER, HelmetType.STANDARD) == (
        pytest.approx(0.255)
    )


def test_cda_span_brackets_the_reported_range() -> None:
    """§I.2.3's sanity check: the table spans the right range at both ends."""
    best = cda_for(BikePosition.TT_BIKE, AthleteLevel.EXPERIENCED, HelmetType.AERO)
    worst = cda_for(BikePosition.ROAD_HOODS, AthleteLevel.FIRST, HelmetType.STANDARD)
    assert best == pytest.approx(0.225), "well-optimised age-grouper, reported 0.20-0.23"
    assert worst == pytest.approx(0.345), "nervous first-timer sitting up"


def test_coll_de_femenia_power_balance() -> None:
    """§4.2.4's fully worked climb, term by term.

    Athlete M on C-TRAM in hot conditions: 165.415 W at +5.8% gives
    2.962566 m/s, and the four power terms sum to P_wheel.
    """
    cda = cda_for(BikePosition.TT_BIKE, AthleteLevel.IMPROVER, HelmetType.STANDARD)
    density = air_density(31.0, 55.0, 120.0)
    power = 149.361 * (1 + 0.12 * math.tanh(0.058 / 0.04))

    speed = solve_speed(
        power,
        density=density,
        cda=cda,
        crr=0.005,
        mass_kg=85.0,
        gradient=0.058,
        wind_speed_ms=3.0,
    )
    assert speed == pytest.approx(2.962566, abs=1e-4)
    assert speed * 3.6 == pytest.approx(10.6652, abs=1e-3)

    balance = total_wheel_power(
        speed,
        density=density,
        cda=cda,
        crr=0.005,
        mass_kg=85.0,
        gradient=0.058,
        wind_speed_ms=3.0,
        wind_relative_rad=None,
    )
    assert balance == pytest.approx(power * phys.ETA_DRIVETRAIN, rel=1e-6)


def test_bisection_solves_the_descent_newton_diverges_on() -> None:
    """§0.4: Newton returned 1.8 km/h here; bisection returns 61.8.

    The document's §4.2.4 table gives 65.002 km/h for the Femenia descent at
    133.592 W, which is the same branch.
    """
    speed = solve_speed(
        133.592,
        density=air_density(31.0, 55.0, 120.0),
        cda=cda_for(BikePosition.TT_BIKE, AthleteLevel.IMPROVER, HelmetType.STANDARD),
        crr=0.005,
        mass_kg=85.0,
        gradient=-0.055,
        wind_speed_ms=3.0,
    )
    assert speed * 3.6 == pytest.approx(65.002, abs=0.01)


def test_descent_speed_is_capped_for_safety() -> None:
    """No plan should instruct an age-grouper to hold 80 km/h."""
    speed = solve_speed(
        300.0,
        density=1.2,
        cda=0.22,
        crr=0.004,
        mass_kg=80.0,
        gradient=-0.15,
        wind_speed_ms=0.0,
    )
    assert speed == pytest.approx(phys.V_DESCENT_MAX)


def test_unknown_wind_direction_always_costs_time() -> None:
    """Drag is quadratic, so a headwind costs more than the tailwind saves.

    A model that set unknown wind to zero would systematically under-predict
    every windy race. The `w²/2` term is precisely that asymmetry.
    """
    common = {
        "density": 1.2,
        "cda": 0.28,
        "crr": 0.005,
        "mass_kg": 85.0,
        "gradient": 0.0,
    }
    still = solve_speed(200.0, wind_speed_ms=0.0, **common)
    windy = solve_speed(200.0, wind_speed_ms=5.0, **common)
    assert windy < still


def test_negative_power_means_freewheeling_not_braking() -> None:
    """On a steep descent the modulated target may go negative (§I.2.4)."""
    speed = solve_speed(
        -50.0,
        density=1.2,
        cda=0.28,
        crr=0.005,
        mass_kg=85.0,
        gradient=-0.06,
        wind_speed_ms=0.0,
    )
    assert speed > 0.0


def test_speed_solve_is_bit_identical_across_repeats() -> None:
    """Fixed-iteration bisection: no tolerance, so no platform-dependent branch."""
    args = {
        "density": 1.13342,
        "cda": 0.2594,
        "crr": 0.005,
        "mass_kg": 85.0,
        "gradient": 0.058,
        "wind_speed_ms": 3.0,
    }
    first = solve_speed(165.415, **args)
    for _ in range(20):
        assert solve_speed(165.415, **args) == first
