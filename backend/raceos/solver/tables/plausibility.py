"""Plausibility ranges for the eight athlete constraints. §A → ``plausibility.py``.

Shared with the API layer, which enforces them and returns ``INVALID_INPUT``
before a solve is ever attempted. The solver re-asserts them as a defensive
postcondition — the same table, checked twice, because a value that reaches
the numeric path out of range produces a plausible-looking plan rather than an
error.
"""

from __future__ import annotations

from typing import Final, NamedTuple


class Range(NamedTuple):
    minimum: float
    maximum: float
    unit: str
    basis: str


PLAUSIBILITY: Final[dict[str, Range]] = {
    "swim_threshold_pace": Range(
        60.0,
        240.0,
        "/100m",
        "60 = world-record territory; 240 = 4:00/100 m, slower than any cut-off permits",
    ),
    "bike_threshold_power": Range(
        80.0,
        500.0,
        "w",
        "500 W FTP is beyond any age-grouper",
    ),
    "run_threshold_pace": Range(180.0, 540.0, "/km", "3:00/km to 9:00/km"),
    "weight": Range(35.0, 200.0, "kg", ""),
    "sweat_rate": Range(
        0.3,
        3.0,
        "L/hr",
        "Baker 2017 reports ~0.5-2.0 L/hr; widened for tails",
    ),
    "sodium_loss": Range(
        200.0,
        2200.0,
        "mg/L",
        "Baker 2017: 10-90 mmol/L = 230-2070 mg/L",
    ),
    "gut_carb_ceiling": Range(
        20.0,
        120.0,
        "g/hr",
        "120 = the evidenced hard maximum",
    ),
    "caffeine_tolerance": Range(0.0, 600.0, "mg", ""),
}
