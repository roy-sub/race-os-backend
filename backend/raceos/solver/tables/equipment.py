"""CdA, mass and rolling resistance. ``SOLVER_MODEL.md`` §A → ``equipment.py``.

CdA is the single largest lever on the bike split. §I.2.3 measures the span
across the plausible age-group range at **21.2 minutes over 180 km** — larger
than the 20-minute `clear`/`tight` margin band, which means the CdA assumption
alone can flip a feasibility verdict. That is why ``bike_setup`` is a captured
input (§F.2) rather than something inferred from ``athlete.level``.
"""

from __future__ import annotations

from typing import Final

from raceos.domain.enums import AthleteLevel, BikePosition, HelmetType, SurfaceQuality

# ---------------------------------------------------------------------------
# CdA (§I.2.3)
#
# Confidence: Medium on the position bases — industry and coaching sources,
# mutually corroborating, none peer-reviewed. Low on the modifiers, which are
# reasoned rather than measured.
# ---------------------------------------------------------------------------

CDA_BASE: Final[dict[BikePosition, float]] = {
    # Measured road position ≈ 0.316; rounded up for a non-racer.
    BikePosition.ROAD_HOODS: 0.325,
    # Measured ≈ 0.296.
    BikePosition.ROAD_DROPS: 0.300,
    # Reported band 0.26–0.30. Also the fallback position.
    BikePosition.ROAD_CLIPONS: 0.280,
    # Age-group TT band 0.20–0.23 optimised; 0.255 for a typical un-fitted setup.
    BikePosition.TT_BIKE: 0.255,
}

CDA_LEVEL_ADJ: Final[dict[AthleteLevel, float]] = {
    # Less able to hold position over hours.
    AthleteLevel.FIRST: 0.020,
    AthleteLevel.IMPROVER: 0.000,
    # Holds position; likely fitted.
    AthleteLevel.EXPERIENCED: -0.020,
}

CDA_HELMET_ADJ: Final[dict[HelmetType, float]] = {
    HelmetType.AERO: -0.010,
    HelmetType.STANDARD: 0.000,
}

CDA_MIN: Final[float] = 0.19
CDA_MAX: Final[float] = 0.38

#: Used when ``bike_setup`` is absent, which adds ``athlete.bike_setup`` to
#: ``assumed_fields`` (§0.5b). The fallback can sit up to 0.045 m² from an
#: athlete's true value — about 15 minutes over 180 km — so supplying
#: ``bike_setup`` later will frequently cross the drift thresholds. That is
#: correct: it is new information that genuinely moves the plan.
CDA_FALLBACK_POSITION: Final[BikePosition] = BikePosition.ROAD_CLIPONS
CDA_FALLBACK_HELMET: Final[HelmetType] = HelmetType.STANDARD

# ---------------------------------------------------------------------------
# Bike and kit mass (§I.2.3)
#
# Low confidence, low stakes: ±1 kg on an 85 kg system is 1.2%, and it only
# bites on climbs (≈40 s over 2100 m of ascent).
# ---------------------------------------------------------------------------

BIKE_KIT_MASS_KG: Final[dict[AthleteLevel, float]] = {
    AthleteLevel.FIRST: 11.0,
    AthleteLevel.IMPROVER: 10.0,
    AthleteLevel.EXPERIENCED: 9.0,
}

# ---------------------------------------------------------------------------
# Rolling resistance (§I.2.2)
#
# A property of the **course surface**, from the bundle — not of the athlete.
# ---------------------------------------------------------------------------

CRR: Final[dict[SurfaceQuality, float]] = {
    # Roller and coast-down tests report 0.0027–0.0040 for clinchers on
    # smooth asphalt. Medium confidence.
    SurfaceQuality.SMOOTH_ASPHALT: 0.0040,
    # Standard bicycle-on-asphalt figure. Medium confidence. The default.
    SurfaceQuality.TYPICAL_ROAD: 0.0050,
    # **Extrapolated beyond the published 0.0025–0.005 band. Low confidence —
    # an estimate.** An inference from "rough paved = 0.005" plus a margin,
    # not a measurement. On a 180 km leg, moving from 0.0050 to 0.0065 costs
    # about eight minutes, so it should not be assigned to a course casually.
    SurfaceQuality.ROUGH_CHIPSEAL: 0.0065,
}
