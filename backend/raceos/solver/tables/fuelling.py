"""Fuelling constants. §A → ``fuelling.py``."""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Carbohydrate (§5.1) — Jeukendrup (2014), Sports Medicine 44(S1):S25–S33
#
# The duration target L(t) is piecewise-linear and continuous by construction:
# L(1) = 30, L(2) = 60, L(2.5) = 60, L(4) = 90.
#
# `duration_hours` is bike + run **moving time only** — not swim, not
# transitions. Carbohydrate is not ingested at a meaningful rate during a
# swim, and including it would inflate `total_carb_g` beyond what the plan
# actually asks the athlete to consume.
# ---------------------------------------------------------------------------

#: Knots as (hours, g·h⁻¹), interpolated linearly between and held flat
#: outside. ⚠ VERIFY (§E-6); corroborated across several independent
#: summaries, so lower risk than most.
CARB_DURATION_KNOTS: Final[tuple[tuple[float, float], ...]] = (
    (1.0, 30.0),
    (2.0, 60.0),
    (2.5, 60.0),
    (4.0, 90.0),
)

#: Above this a single carbohydrate source cannot be oxidised — SGLT1
#: saturates — so the plan must specify a glucose:fructose mix. This produces
#: an output *flag*, `requires_multiple_transportable`, not a numeric
#: adjustment. High confidence.
CARB_SINGLE_TRANSPORTER_MAX: Final[float] = 60.0

#: The literature target for ultra-duration efforts. High confidence.
CARB_ULTRA_TARGET: Final[float] = 90.0

#: The highest rate with published oxidation evidence (Podlogar et al. 2022,
#: 120 vs 90 g·h⁻¹ fructose–maltodextrin). Medium confidence — the authors call
#: the benefit above 90 "speculative".
#:
#: **An override cannot exceed it.** An override is a statement that the
#: athlete knows their gut better than the stored constraint; it is not a
#: statement that they have repealed intestinal transport.
CARB_HARD_MAX: Final[float] = 120.0

#: Jeukendrup 2014; GSSI SSE-108. High confidence.
GLUCOSE_FRUCTOSE_RATIO: Final[float] = 2.0

#: The tolerance on the arithmetic-consistency postcondition exists solely to
#: absorb the single rounding of `carb_g_per_hr` to 1 g. A failure larger than
#: this means a rounded value leaked into a computation — exactly the bug §0.4
#: exists to prevent.
CARB_CONSISTENCY_TOLERANCE_G: Final[float] = 0.5

# A reality check worth carrying into validation (§5.1): Pfeiffer et al. (2012)
# measured what Ironman athletes actually consume — 62 ± 26 g·h⁻¹ at IM Hawaii,
# 71 ± 25 at IM Germany. The literature *recommendation* of 90 sits at roughly
# the 80th percentile of observed behaviour. The model applies no level-based
# discount to reach those numbers: `gut_carb_ceiling` is the mechanism for
# athlete-specific limits, and a second discount would double-count and violate
# the "estimated constraints carry full weight" rule. If back-testing shows
# plans routinely prescribing 90 to athletes whose ceiling is `estimated`, the
# problem is in the onboarding estimator's default, not here.

# ---------------------------------------------------------------------------
# Fluid (§5.2)
# ---------------------------------------------------------------------------

#: **Estimate. Low confidence** — no clean published %-per-°C coefficient
#: found. Scales sweat rate with heat stress above the reference condition.
K_SWEAT: Final[float] = 0.030

#: **Estimate. Low confidence.** The assumed condition under which a sweat test
#: was run, in °C WBGT. **Fallback only**: used when
#: `sweat_rate.measured_at_temp_c` (§F.4) is absent, which then appears in
#: `assumed_fields`. This matters more than it looks — an athlete who tested on
#: a hot day and one who tested indoors in winter currently get identical
#: treatment from identical stored values, and the resulting fluid plan can
#: differ by 20% or more between those two readings of the same number.
W_SWEAT_REF: Final[float] = 15.0

#: Used to convert a supplied `measured_at_temp_c` onto the WBGT axis. A sweat
#: test records a temperature but almost never a humidity or a sky state, so
#: these two are themselves estimates — the gain is that the *temperature*, the
#: term that dominates, becomes a measurement instead of an assumption.
SWEAT_TEST_DEFAULT_RH: Final[float] = 55.0
SWEAT_TEST_DEFAULT_CONDITIONS: Final[str] = "partly_cloudy"

#: Targets ≤2% body-mass loss rather than full replacement. Medium confidence.
#: Deliberately **not** 1.0: full sweat replacement over a 9-hour race is both
#: unachievable (it would exceed gastric capacity) and dangerous —
#: exercise-associated hyponatraemia is a real cause of long-course medical
#: events, and it is caused by drinking too much, not too little.
REPLACE_FRAC: Final[float] = 0.75

#: Gastric emptying maximum ~15–20 mL·min⁻¹. Medium confidence. In heat this
#: is usually the binding limit, and naming it correctly is the point of
#: `bind()`: the athlete cannot absorb their own sweat rate, and the plan
#: should say so.
GASTRIC_CAP_ML: Final[float] = 1000.0

# ---------------------------------------------------------------------------
# Sodium (§5.3)
#
# Units matter here: `sodium_loss` is a **concentration** (mg·L⁻¹ of sweat), so
# it must be multiplied by sweat *volume* to yield a rate. Confusing the two is
# a plausible implementation error producing a number ~1.5× wrong, and it has
# its own unit test.
# ---------------------------------------------------------------------------

#: Sodium losses need not be fully replaced within a race. Medium confidence.
REPLACE_FRAC_NA: Final[float] = 0.80

#: ACSM Position Stand, *Exercise and Fluid Replacement* (2007). High
#: confidence. ⚠ VERIFY (§E-8).
ACSM_MIN_G_PER_L: Final[float] = 0.5

#: Practical floor. Medium confidence.
SODIUM_MIN: Final[float] = 300.0

#: Practical ceiling; above this is unpalatable and GI-provocative. **Low-Med
#: confidence.**
SODIUM_MAX: Final[float] = 1500.0

# ---------------------------------------------------------------------------
# Caffeine (§5.4)
# ---------------------------------------------------------------------------

#: The top of the ISSN's evidenced 3–6 mg·kg⁻¹ range (Guest et al. 2021, ISSN
#: Position Stand, JISSN). **High confidence**, and the position stand
#: explicitly supports 3–6 mg·kg⁻¹ for endurance exercise *in the heat*, which
#: is the case that matters here. ⚠ VERIFY (§E-10).
CAFFEINE_MG_PER_KG: Final[float] = 6.0

#: Deterministic dose schedule: pre-start, mid-bike, run start. Must sum to 1.0.
CAFFEINE_SCHEDULE: Final[tuple[float, float, float]] = (0.30, 0.35, 0.35)

#: The ISSN's "most commonly used timing of 60 min pre-exercise", pulled
#: forward to account for the swim.
CAFFEINE_PRE_START_MIN: Final[float] = 45.0

#: Where in the bike leg the second dose falls.
CAFFEINE_BIKE_FRACTION: Final[float] = 0.55
