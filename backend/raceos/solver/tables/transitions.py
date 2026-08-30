"""Transition durations. §A → ``transitions.py``.

**This is the lowest-confidence table in the entire model** (§4.5), and the
document is unusually blunt about why. Public data on age-group transition
times is not merely imprecise, it is mutually contradictory: three sources
describing the same population reported "6–12 min combined for a 12-hour
finisher", "17 min combined", and "26–29 min combined". None specifies whether
the figure includes the run from swim exit to the change tent, which at a large
race is several minutes on its own.

These values are **reasoned estimates constrained to lie inside that
contradictory range**, not measurements.

They are also the **first thing to recalibrate** from real race data, because
unlike every other constant here they can be read directly off a results page
with no modelling assumptions at all.
"""

from __future__ import annotations

from typing import Final

from raceos.domain.enums import AthleteLevel, SolverDistance

T1_BASE_MIN: Final[dict[SolverDistance, dict[AthleteLevel, float]]] = {
    SolverDistance.FULL: {
        AthleteLevel.FIRST: 9.0,
        AthleteLevel.IMPROVER: 6.5,
        AthleteLevel.EXPERIENCED: 4.5,
    },
    SolverDistance.HALF: {
        AthleteLevel.FIRST: 6.5,
        AthleteLevel.IMPROVER: 4.5,
        AthleteLevel.EXPERIENCED: 3.0,
    },
    SolverDistance.OLYMPIC: {
        AthleteLevel.FIRST: 3.5,
        AthleteLevel.IMPROVER: 2.5,
        AthleteLevel.EXPERIENCED: 1.6,
    },
    SolverDistance.SPRINT: {
        AthleteLevel.FIRST: 2.6,
        AthleteLevel.IMPROVER: 1.8,
        AthleteLevel.EXPERIENCED: 1.2,
    },
}

T2_BASE_MIN: Final[dict[SolverDistance, dict[AthleteLevel, float]]] = {
    SolverDistance.FULL: {
        AthleteLevel.FIRST: 8.0,
        AthleteLevel.IMPROVER: 6.0,
        AthleteLevel.EXPERIENCED: 4.0,
    },
    SolverDistance.HALF: {
        AthleteLevel.FIRST: 5.0,
        AthleteLevel.IMPROVER: 4.0,
        AthleteLevel.EXPERIENCED: 2.5,
    },
    SolverDistance.OLYMPIC: {
        AthleteLevel.FIRST: 2.5,
        AthleteLevel.IMPROVER: 2.0,
        AthleteLevel.EXPERIENCED: 1.2,
    },
    SolverDistance.SPRINT: {
        AthleteLevel.FIRST: 2.0,
        AthleteLevel.IMPROVER: 1.5,
        AthleteLevel.EXPERIENCED: 1.0,
    },
}

#: Added to T1 only when the athlete actually swam in a wetsuit.
WETSUIT_REMOVAL_MIN: Final[dict[AthleteLevel, float]] = {
    AthleteLevel.FIRST: 3.0,
    AthleteLevel.IMPROVER: 2.5,
    AthleteLevel.EXPERIENCED: 2.0,
}
