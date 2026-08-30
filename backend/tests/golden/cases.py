"""The fifteen golden case definitions. ``SOLVER_MODEL.md`` §B.

**Inputs are specified exactly by the document; expected outputs are captured
from the first correct implementation run and then frozen.** They are not
guessed here, because a guessed expectation that the implementation is then
tuned to match would test nothing.

§B's heading says "twelve cases" while §B.4 defines fifteen — ``G13``, ``G14``
and ``G15`` are marked *New* in that revision and all three appear in §F.8's
affected-cases table. The "twelve" is stale text from before the revision, and
dropping any of the three would leave a §F contract change untested: ``G14`` is
the only case that pins §F.5's earliest-missed-barrier change, and ``G15`` the
only one that exercises ``assumed_fields``.

Two cases are **definitional** rather than captured: ``G05-INFEASIBLE`` and
``G06-TIGHT``. Their inputs were chosen by running the model until they
produced the required verdict, so the stated verdict is a genuine expectation
and is asserted directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

from raceos.domain.enums import (
    AthleteLevel,
    BikePosition,
    ConstraintSource,
    HelmetType,
    RiskLevel,
)
from raceos.solver.models import (
    AthleteSnapshot,
    BikeSetup,
    ConstraintValue,
    EventSpec,
    ForecastSnapshot,
    GoalSpec,
    SolveOptions,
)

# ---------------------------------------------------------------------------
# §B.2 Athletes
#
# Constraint `source` values are fixed per athlete so the provenance-invariance
# test has something to permute. `A-M` is `tested` except weight/sweat/sodium/
# caffeine; `A-E` is all `measured`; `A-F`, `A-T`, `A-X`, `A-Y` are all
# `estimated`.
# ---------------------------------------------------------------------------

_M_SOURCES = {
    "weight": ConstraintSource.MEASURED,
    "sweat_rate": ConstraintSource.ESTIMATED,
    "sodium_loss": ConstraintSource.ESTIMATED,
    "caffeine_tolerance": ConstraintSource.MANUAL,
}

_UNITS = {
    "swim_threshold_pace": "/100m",
    "bike_threshold_power": "w",
    "run_threshold_pace": "/km",
    "weight": "kg",
    "sweat_rate": "L/hr",
    "sodium_loss": "mg/L",
    "gut_carb_ceiling": "g/hr",
    "caffeine_tolerance": "mg",
}


def _athlete(
    level: AthleteLevel,
    values: dict[str, float],
    position: BikePosition,
    helmet: HelmetType,
    default_source: ConstraintSource,
    overrides: dict[str, ConstraintSource] | None = None,
    *,
    with_setup: bool = True,
    sweat_temp_c: float | None = None,
) -> AthleteSnapshot:
    sources = dict(overrides or {})
    constraints = tuple(
        ConstraintValue(
            key=key,
            value=values[key],
            unit=_UNITS[key],
            source=sources.get(key, default_source),
            measured_at_temp_c=sweat_temp_c if key == "sweat_rate" else None,
        )
        # A fixed order, not a dict's, so the canonical input hash is stable.
        for key in _UNITS
    )
    return AthleteSnapshot(
        level=level,
        constraints=constraints,
        bike_setup=BikeSetup(position, helmet) if with_setup else None,
    )


A_M_VALUES = {
    "swim_threshold_pace": 105.0,
    "bike_threshold_power": 224.0,
    "run_threshold_pace": 282.0,
    "weight": 75.0,
    "sweat_rate": 1.1,
    "sodium_loss": 900.0,
    "gut_carb_ceiling": 75.0,
    "caffeine_tolerance": 300.0,
}
A_E_VALUES = {
    "swim_threshold_pace": 88.0,
    "bike_threshold_power": 285.0,
    "run_threshold_pace": 240.0,
    "weight": 68.0,
    "sweat_rate": 1.4,
    "sodium_loss": 1250.0,
    "gut_carb_ceiling": 95.0,
    "caffeine_tolerance": 400.0,
}
A_F_VALUES = {
    "swim_threshold_pace": 145.0,
    "bike_threshold_power": 155.0,
    "run_threshold_pace": 400.0,
    "weight": 82.0,
    "sweat_rate": 0.9,
    "sodium_loss": 700.0,
    "gut_carb_ceiling": 50.0,
    "caffeine_tolerance": 150.0,
}
A_T_VALUES = {
    "swim_threshold_pace": 133.0,
    "bike_threshold_power": 176.0,
    "run_threshold_pace": 363.0,
    "weight": 77.0,
    "sweat_rate": 1.0,
    "sodium_loss": 800.0,
    "gut_carb_ceiling": 55.0,
    "caffeine_tolerance": 200.0,
}
A_X_VALUES = {
    "swim_threshold_pace": 122.0,
    "bike_threshold_power": 195.0,
    "run_threshold_pace": 325.0,
    "weight": 73.0,
    "sweat_rate": 0.9,
    "sodium_loss": 700.0,
    "gut_carb_ceiling": 50.0,
    "caffeine_tolerance": 150.0,
}
A_Y_VALUES = {
    "swim_threshold_pace": 150.0,
    "bike_threshold_power": 150.0,
    "run_threshold_pace": 420.0,
    "weight": 84.0,
    "sweat_rate": 0.9,
    "sodium_loss": 700.0,
    "gut_carb_ceiling": 50.0,
    "caffeine_tolerance": 150.0,
}

#: baseline
A_M = _athlete(
    AthleteLevel.IMPROVER,
    A_M_VALUES,
    BikePosition.TT_BIKE,
    HelmetType.STANDARD,
    ConstraintSource.TESTED,
    _M_SOURCES,
)
#: short course
A_E = _athlete(
    AthleteLevel.EXPERIENCED,
    A_E_VALUES,
    BikePosition.TT_BIKE,
    HelmetType.AERO,
    ConstraintSource.MEASURED,
)
#: infeasible
A_F = _athlete(
    AthleteLevel.FIRST,
    A_F_VALUES,
    BikePosition.ROAD_CLIPONS,
    HelmetType.STANDARD,
    ConstraintSource.ESTIMATED,
)
#: tight margin
A_T = _athlete(
    AthleteLevel.FIRST,
    A_T_VALUES,
    BikePosition.ROAD_CLIPONS,
    HelmetType.STANDARD,
    ConstraintSource.ESTIMATED,
)
#: night finish
A_X = _athlete(
    AthleteLevel.FIRST,
    A_X_VALUES,
    BikePosition.ROAD_CLIPONS,
    HelmetType.STANDARD,
    ConstraintSource.ESTIMATED,
)
#: earliest-miss
A_Y = _athlete(
    AthleteLevel.FIRST,
    A_Y_VALUES,
    BikePosition.ROAD_CLIPONS,
    HelmetType.STANDARD,
    ConstraintSource.ESTIMATED,
)
#: `A-M` with no bike_setup, for G15's assumed-fields path.
A_M_BARE = _athlete(
    AthleteLevel.IMPROVER,
    A_M_VALUES,
    BikePosition.TT_BIKE,
    HelmetType.STANDARD,
    ConstraintSource.TESTED,
    _M_SOURCES,
    with_setup=False,
)

# ---------------------------------------------------------------------------
# §B.3 Forecasts
# ---------------------------------------------------------------------------

F_MILD = ForecastSnapshot(
    temp_c=22.0,
    humidity=60.0,
    wind_speed_ms=3.0,
    conditions="partly_cloudy",
    water_temp_c=21.0,
    pressure_hpa=1015.0,
    cloud_cover_pct=40.0,
)
F_HOT = ForecastSnapshot(
    temp_c=31.0,
    humidity=55.0,
    wind_speed_ms=3.0,
    conditions="clear",
    water_temp_c=22.5,
    pressure_hpa=1013.0,
    cloud_cover_pct=5.0,
)
F_COOL = ForecastSnapshot(
    temp_c=14.0,
    humidity=70.0,
    wind_speed_ms=5.0,
    conditions="cloudy",
    water_temp_c=15.5,
    pressure_hpa=1008.0,
    cloud_cover_pct=80.0,
)
F_WARMWATER = ForecastSnapshot(
    temp_c=27.0,
    humidity=65.0,
    wind_speed_ms=2.0,
    conditions="clear",
    water_temp_c=26.0,
    pressure_hpa=1012.0,
    cloud_cover_pct=10.0,
)
#: `F-MILD` with both new optional fields absent, for the assumed-fields case.
F_MILD_BARE = ForecastSnapshot(
    temp_c=22.0,
    humidity=60.0,
    wind_speed_ms=3.0,
    conditions="partly_cloudy",
    water_temp_c=21.0,
)

# ---------------------------------------------------------------------------
# §B.4 The fifteen cases
# ---------------------------------------------------------------------------

#: `2026-09-19`, `start_time_local 07:00` unless a case states otherwise.
DEFAULT_EVENT_DATE = date(2026, 9, 19)
DEFAULT_START = time(7, 0)

#: (lat, lng, tz, utc offset on the event date) per golden course.
COURSE_LOCATIONS: dict[str, tuple[float, float, str, float]] = {
    "C-TRAM": (39.85, 3.12, "Europe/Madrid", 2.0),
    "C-FLAT": (39.85, 3.12, "Europe/Madrid", 2.0),
    "C-HALF": (39.85, 3.12, "Europe/Madrid", 2.0),
    "C-OLY": (39.85, 3.12, "Europe/Madrid", 2.0),
    "C-SPR": (39.85, 3.12, "Europe/Madrid", 2.0),
    "C-ALTA": (46.52, 7.98, "Europe/Zurich", 2.0),
}


@dataclass(frozen=True)
class GoldenCase:
    case_id: str
    course_id: str
    athlete: AthleteSnapshot
    forecast: ForecastSnapshot
    goal: GoalSpec
    options: SolveOptions
    start_time: time = DEFAULT_START
    exercises: str = ""
    #: Set for the two definitional cases, asserted directly rather than frozen.
    expect_infeasible: bool = False

    def event(self) -> EventSpec:
        lat, lng, tz, offset = COURSE_LOCATIONS[self.course_id]
        return EventSpec(
            event_date=DEFAULT_EVENT_DATE,
            start_time_local=self.start_time,
            timezone=tz,
            lat=lat,
            lng=lng,
            utc_offset_hours=offset,
        )


def _goal(level: AthleteLevel) -> GoalSpec:
    """`goal_minutes = None`, `risk = balanced`, `first_timer = (level == first)`."""
    return GoalSpec(
        goal_minutes=None,
        risk=RiskLevel.BALANCED,
        first_timer=level is AthleteLevel.FIRST,
    )


CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        "G01-FULL",
        "C-TRAM",
        A_M,
        F_MILD,
        _goal(AthleteLevel.IMPROVER),
        SolveOptions(),
        exercises="Primary distance. Full-distance baseline.",
    ),
    GoldenCase(
        "G02-HALF",
        "C-HALF",
        A_M,
        F_MILD,
        _goal(AthleteLevel.IMPROVER),
        SolveOptions(),
        exercises="Primary distance. The if_ref half row; swim durability nearly inert at 1900 m.",
    ),
    GoldenCase(
        "G03-OLYMPIC",
        "C-OLY",
        A_E,
        F_MILD,
        _goal(AthleteLevel.EXPERIENCED),
        SolveOptions(),
        exercises=(
            "OUT OF PRIMARY SCOPE (§0.1b). Asserts the short-distance path runs and "
            "stays deterministic — NOT that the numbers are right."
        ),
    ),
    GoldenCase(
        "G04-SPRINT",
        "C-SPR",
        A_E,
        F_MILD,
        _goal(AthleteLevel.EXPERIENCED),
        SolveOptions(),
        exercises="OUT OF PRIMARY SCOPE. Shortest path; CSS gives pace faster than CSS.",
    ),
    GoldenCase(
        "G05-INFEASIBLE",
        "C-TRAM",
        A_F,
        F_MILD,
        _goal(AthleteLevel.FIRST),
        SolveOptions(),
        exercises="Definitional: expect feasibility = infeasible at the finish.",
        expect_infeasible=True,
    ),
    GoldenCase(
        "G06-TIGHT",
        "C-TRAM",
        A_T,
        F_MILD,
        _goal(AthleteLevel.FIRST),
        SolveOptions(),
        exercises="Definitional: expect margin_state = tight at the finish.",
    ),
    GoldenCase(
        "G07-HOT",
        "C-TRAM",
        A_M,
        F_HOT,
        _goal(AthleteLevel.IMPROVER),
        SolveOptions(),
        exercises="Bike heat clamped at the top knot; arm coolers included.",
    ),
    GoldenCase(
        "G08-FIRSTTIMER",
        "C-HALF",
        A_F,
        F_MILD,
        _goal(AthleteLevel.FIRST),
        SolveOptions(),
        exercises="First-timer bag set; `first` level tables throughout.",
    ),
    GoldenCase(
        "G09-CARBOVERRIDE",
        "C-TRAM",
        A_M,
        F_MILD,
        _goal(AthleteLevel.IMPROVER),
        SolveOptions(carb_override=95.0),
        exercises="overridden = true; 95 > ceiling 75, below carb_hard_max 120.",
    ),
    GoldenCase(
        "G10-NIGHT",
        "C-TRAM",
        A_X,
        F_MILD,
        _goal(AthleteLevel.FIRST),
        SolveOptions(),
        start_time=time(8, 0),
        exercises="Finish past civil dusk. Feasible, so Stage 6 runs and emits the torch.",
    ),
    GoldenCase(
        "G11-FLAT",
        "C-FLAT",
        A_M,
        F_MILD,
        _goal(AthleteLevel.IMPROVER),
        SolveOptions(),
        exercises="grade_mod ~ 1 throughout; VI ~ 1.000; smooth_asphalt Crr.",
    ),
    GoldenCase(
        "G12-MOUNTAIN",
        "C-ALTA",
        A_E,
        F_COOL,
        _goal(AthleteLevel.EXPERIENCED),
        SolveOptions(),
        exercises="The only case exercising alt_factor (mean 980 m); widest histogram.",
    ),
    GoldenCase(
        "G13-NOWETSUIT",
        "C-TRAM",
        A_M,
        F_WARMWATER,
        _goal(AthleteLevel.IMPROVER),
        SolveOptions(),
        exercises=(
            "Water 26.0 °C is permitted-but-not-award-eligible, so the model races "
            "WITHOUT a wetsuit and sets the warning flag. c_warm term active."
        ),
    ),
    GoldenCase(
        "G14-EARLIESTMISS",
        "C-TRAM",
        A_Y,
        F_MILD,
        _goal(AthleteLevel.FIRST),
        SolveOptions(),
        exercises=(
            "Pins §F.5. Two barriers missed; the EARLIEST is reported. A regression "
            "that reverted to 'tightest' would report the finish and fail loudly."
        ),
        expect_infeasible=True,
    ),
    GoldenCase(
        "G15-ASSUMED",
        "C-TRAM",
        A_M_BARE,
        F_MILD_BARE,
        _goal(AthleteLevel.IMPROVER),
        SolveOptions(),
        exercises=(
            "Expect assumed_fields to contain exactly the four documented paths, "
            "sorted. CdA falls back to road_clipons + improver = 0.280, so the bike "
            "split differs from G01."
        ),
    ),
)

CASES_BY_ID: dict[str, GoldenCase] = {case.case_id: case for case in CASES}
