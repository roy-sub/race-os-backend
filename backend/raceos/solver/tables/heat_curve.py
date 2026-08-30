"""The environment model's constants. ``SOLVER_MODEL.md`` §A → ``heat_curve.py``.

**Read `docs/LAUNCH_BLOCKERS.md` before trusting anything in this file.**
``SOLVER_MODEL.md`` §0.2 states plainly that *not one constant in that document
was verified against its primary source by its author* — the research
environment's egress policy blocked every publisher host, and it blocks them
here too. The values below are transcribed verbatim and are tunable without a
deploy, which is exactly why they live in a table.

Two entries are Tier-1 verification items:

* :data:`STULL_COEFFS` — if these are wrong, **every heat number in the model
  is wrong**, because both heat curves are expressed on the WBGT axis this
  feeds.
* the bike heat curve's assumed laboratory humidity, :data:`PEIFFER_LAB_RH`.
"""

from __future__ import annotations

from typing import Final

from raceos.domain.enums import AthleteLevel

# ---------------------------------------------------------------------------
# Psychrometric wet-bulb temperature (§I.1.2)
#
# Stull (2011), J Appl Meteor Climatol 50:2267–2269. A single-expression
# empirical fit, mean absolute error < 0.3 °C, valid for RH 5–99% and
# T −20 to +50 °C. Chosen because it is closed-form, continuous, deterministic
# and needs only the two variables the forecast actually carries.
#
# ⚠ VERIFY (§E-1): all six digits, and that every `atan` is in RADIANS. Using
# degrees is the single most common implementation error with this formula.
# ---------------------------------------------------------------------------

STULL_COEFFS: Final[tuple[float, float, float, float, float, float]] = (
    0.151977,  # inside the first sqrt term
    8.313659,  # RH offset inside that sqrt
    1.676331,  # subtracted from RH in the third atan
    0.00391838,  # coefficient of the RH^1.5 term
    0.023101,  # inside the fourth atan
    4.686035,  # the constant subtracted at the end
)
STULL_RH_EXPONENT: Final[float] = 1.5

# ---------------------------------------------------------------------------
# Wet Bulb Globe Temperature (§I.1.3)
# ---------------------------------------------------------------------------

#: Standard WBGT definition. Certain; nothing would change these.
WBGT_W_WET: Final[float] = 0.7
WBGT_W_GLOBE: Final[float] = 0.2
WBGT_W_DRY: Final[float] = 0.1

#: **Estimate. Low confidence, and the single highest-leverage low-confidence
#: constant in the environment model** — it contributes 1.6 °C to WBGT under
#: clear sky, roughly 5 s·km⁻¹ of mid-level run pace. The first thing to
#: calibrate. A course-side globe measurement, or a forecast carrying solar
#: irradiance, would close it.
GLOBE_OFFSET_CLEAR_C: Final[float] = 8.0

#: **Estimate, same basis. Low confidence.**
GLOBE_OFFSET_OVERCAST_C: Final[float] = 1.5

#: `conditions` is categorical, so it maps to a cloud fraction. The mapping is
#: a **linear interpolation in cloud fraction** from the outset, precisely so
#: that supplying `forecast.cloud_cover_pct` (§F.3) is an adapter change and
#: not a model change — the categories are simply the points this curve is
#: sampled at when nothing better is available.
CLOUD_FRACTION: Final[dict[str, float]] = {
    "clear": 0.00,
    "partly_cloudy": 0.35,
    "cloudy": 0.75,
    "overcast": 0.95,
    "rain": 1.00,
}

# ---------------------------------------------------------------------------
# Heat effect on cycling power (§I.1.4)
#
# Peiffer & Abbiss (2011), IJSPP 6(2):208–220 — mean 40 km time-trial power at
# four ambient temperatures, converted onto the WBGT axis assuming laboratory
# conditions of 40% RH and no radiant load.
#
# **Confidence: Medium-Low, and the weakest quantitative link in the model.**
# Three reasons, all live:
#   1. The lab humidity is assumed, not known (⚠ VERIFY §E-2). At 60% RH the
#      knots shift ~2 °C and §0.7's reconciliation changes.
#   2. A 40 km time trial is ~60 minutes; a full-distance bike leg is 4.5–7
#      hours, and thermal strain accumulates. See D-1 in LAUNCH_BLOCKERS.md.
#   3. An indoor result transferred to an outdoor WBGT axis; the 8 °C
#      clear-sky globe offset was not present in the laboratory.
# ---------------------------------------------------------------------------

PEIFFER_LAB_RH: Final[float] = 40.0

#: (WBGT_indoor °C, factor). Interpolation between knots is **linear in
#: WBGT**; below the first and above the last the value is **held flat, not
#: extrapolated**.
BIKE_HEAT_KNOTS: Final[tuple[tuple[float, float], ...]] = (
    (12.016, 1.01543),  # 17 °C ambient
    (16.361, 1.00000),  # 22 °C — the reference
    (20.707, 0.99383),  # 27 °C
    (25.053, 0.95370),  # 32 °C
)

#: The flat clamp above the top knot is a **deliberate refusal to
#: extrapolate**, and it matters. A power law through the top two knots gives
#: an exponent of 2.9; extrapolating that to WBGT 27.7 predicts −10.1%, or
#: −15.8 W, for which there is no evidence — the data simply stops at 32 °C.
#: Holding the last measured value errs toward under-stating the bike penalty,
#: which is the safe direction: the run model, anchored well past this range,
#: carries the consequence.
BIKE_HEAT_EXTRAPOLATE: Final[bool] = False

# ---------------------------------------------------------------------------
# Heat effect on running (§I.1.5)
#
# Ely et al. (2007), MSSE — 140 race-years of marathon field data across a wide
# ability range.
#
# Note the level mapping: Ely's "3-hour marathoner" is a strong age-grouper,
# which maps to this product's `experienced`, not to its middle tier. That is
# why all three levels sit at or above 10% — every RaceOS level is at or below
# Ely's reference runner in ability, and Ely's finding is that slower is worse.
# Ely's elite 2% figure is outside this product's user base and is not used.
# ---------------------------------------------------------------------------

#: Ely: slowing begins above WBGT 5–10 °C. Medium-High confidence.
RUN_HEAT_WBGT_BASELINE_C: Final[float] = 10.0

#: **Estimate. Low confidence — the constant that most needs a real fit.**
#: Chosen to reproduce the reported non-linearity ("50→70 °F costs far less
#: than 70→90 °F"). It controls behaviour in the extrapolated region above
#: WBGT 25 — exactly where hot races live. Ely's own model is quadratic but the
#: coefficients could not be read (⚠ VERIFY §E-3).
RUN_HEAT_EXPONENT: Final[float] = 1.3

RUN_HEAT_PCT_AT_15: Final[dict[AthleteLevel, float]] = {
    # Ely's "slower runners suffer more", extrapolated. Low-Med confidence.
    AthleteLevel.FIRST: 0.16,
    # As above. Low-Med confidence.
    AthleteLevel.IMPROVER: 0.13,
    # Ely, 3-hour marathoner, directly anchored. Med-High confidence.
    AthleteLevel.EXPERIENCED: 0.10,
}

#: Beyond +60% the model is far outside any data and the honest output is a
#: warning, not a number. If this binds, the binding key is
#: `model:run_heat_clamp` and the plan should be treated as advisory. In
#: practice it binds around WBGT 32 °C for a first-timer — conditions at which
#: races are cancelled.
RUN_HEAT_FACTOR_MAX: Final[float] = 1.60

# ---------------------------------------------------------------------------
# Altitude (§I.1.6)
#
# Applied as a multiplier to `bike_threshold_power` and as a divisor to running
# speed. The elevation used is the **mean elevation of the leg**, not
# per-segment: per-segment would imply the athlete's aerobic ceiling changes
# within a single climb, which is not how acclimatisation state works. Air
# density, by contrast, IS per-segment, because that is a property of the air.
#
# Acclimatisation is **not modelled** (D-6): the effect is large, highly
# individual, and the product captures nothing about where the athlete lives.
# ---------------------------------------------------------------------------

#: ~1% VO₂max loss per 1000 m below the breakpoint. Medium confidence; the
#: effect is small.
ALT_A1: Final[float] = 0.010

#: **Sources disagree three ways: 6.3%, 8.1% and 9.2% per 1000 m are all
#: reported. 7.0% is the midpoint — a compromise, not a finding. Low-Med
#: confidence.** It only matters above the breakpoint, which no currently
#: seeded course except `C-ALTA` reaches.
ALT_A2: Final[float] = 0.070

#: The two pieces meet here by construction, so the function is continuous.
ALT_BREAKPOINT_M: Final[float] = 1500.0

# ---------------------------------------------------------------------------
# Solar position (§I.1.7)
# ---------------------------------------------------------------------------

#: Includes refraction and solar radius. Reference only — Stage 6 uses civil
#: twilight, not sunset.
SUNSET_ZENITH_DEG: Final[float] = 90.833

#: Civil twilight. **This is the one Stage 6 uses** for the head-torch rule.
DUSK_ZENITH_DEG: Final[float] = 96.0
