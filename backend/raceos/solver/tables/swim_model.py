"""Swim constants. §A → ``swim_model.py``.

``swim_threshold_pace`` is **Critical Swim Speed**, an *asymptote* — the slope
of the distance–time line — not the pace at any particular distance. §4.4
models it as such, and that distinction is load-bearing: an earlier draft
applied a Riegel-form decay anchored at 2000 m, which made every race distance
*slower* than CSS. That is the right shape for a one-hour anchor and the wrong
shape for an asymptote, and it was wrong by about 2.3% at full distance. That
model is withdrawn.

Maximal swim pace at any finite race distance is therefore **faster** than CSS,
not slower.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

from raceos.domain.enums import AthleteLevel

# ---------------------------------------------------------------------------
# Critical-speed model (§4.4.1)
# ---------------------------------------------------------------------------

#: D′, the intercept of the distance–time line in metres: the finite distance
#: available above the asymptote, the swimming analogue of W′.
#:
#: **Estimate, low confidence — but genuinely low-stakes**, which is why a
#: population default is acceptable: across the plausible range [10, 25] m,
#: planned pace moves 0.4% at 3800 m and 2.0% at 750 m, an order of magnitude
#: less than the constants beside it. It matters most where it matters least,
#: on the short-course distances that are out of primary scope.
#:
#: It is nonetheless **recoverable and should be recovered** (D-12): it falls
#: straight out of the test the athlete already performed,
#: `D′ = 400 − CSS·t₄₀₀ = 200 − CSS·t₂₀₀`. The product computes CSS from that
#: pair and then discards both.
D_PRIME_M: Final[float] = 15.0

#: The critical-speed model is a *maximal-effort* model and holds only over the
#: duration range its underlying test spans — conventionally about 30 minutes.
#: Beyond that, real sustainable speed falls below CSS rather than approaching
#: it from above, so a durability term engages. **Low-Med confidence.**
CSS_VALIDITY_MIN: Final[float] = 30.0

#: **Estimate. Low confidence.** Calibrated so a 3800 m swim lands at
#: ≈ CSS + 4 s·(100 m)⁻¹. Back-testing swim splits is the single most useful
#: swim calibration, and this carries almost all the distance dependence.
K_SWIM_DUR: Final[float] = 0.0012

# ---------------------------------------------------------------------------
# Wetsuit (§4.4.3)
# ---------------------------------------------------------------------------

#: Chatard & Wilson: ~6–7% over 400 m for triathletes, 14% drag reduction at
#: 1.25 m·s⁻¹; discounted here for sustained long-course pace. Medium
#: confidence. The 400 m figure is a max-effort short-distance result.
WETSUIT_FACTOR: Final[float] = 0.955


@dataclass(frozen=True)
class WetsuitRuleset:
    """One federation's wetsuit thresholds, for one season.

    **These are rules, not physics.** Every other constant in this package is a
    claim about how a body behaves and is wrong only if the science is wrong.
    These are wrong the moment a federation publishes a new competition rulebook
    — which happens annually, and which no amount of back-testing will detect.

    So they carry a ``review_by`` date, and a test fails once it passes. That is
    the update mechanism: not a note asking someone to remember, but a red build
    that names the ruleset, the page it came from and what to do about it.
    """

    #: The body that publishes these rules.
    federation: str
    #: The competition season the values were read for.
    season: int
    #: When the build starts failing. The point is that it *should* fail: a
    #: season has turned over and nobody has confirmed the numbers.
    review_by: date
    #: Where to go to confirm them. Named so the check is twenty minutes, not
    #: an afternoon of searching.
    source: str

    #: Below this, a wetsuit is required.
    mandatory_below_c: float
    #: Up to and including this, a wetsuit is permitted and award-eligible.
    legal_max_c: float
    #: Above ``legal_max_c`` and up to this, permitted but not award-eligible.
    non_award_max_c: float


#: Every ruleset this build knows. Adding a federation is an entry here and
#: nothing else — the solver reads whichever one the caller selects.
#:
#: Only one is populated, deliberately. World Triathlon publishes a different
#: table, keyed by swim distance as well as temperature, and the numbers are
#: not in front of me. Transcribing them from memory would put a figure in a
#: rulebook table that no rulebook supports, which is exactly the failure this
#: structure exists to prevent. The shape is ready for it; the values wait for
#: someone with the document open.
WETSUIT_RULESETS: Final[dict[str, WetsuitRuleset]] = {
    "ironman": WetsuitRuleset(
        federation="Ironman",
        season=2026,
        review_by=date(2027, 2, 1),
        source="Ironman Competition Rules, Swim section, current season PDF",
        # The thresholds are genuinely discontinuous — 24.5 °C and 24.6 °C
        # produce different equipment, hence a ~4.5% pace step. That
        # discontinuity is in the rules, and smoothing it would be wrong.
        mandatory_below_c=16.0,
        legal_max_c=24.5,
        non_award_max_c=28.77,
    ),
}

#: Used when a course does not name one. Ironman, because every course in the
#: catalogue is an Ironman-family event.
DEFAULT_WETSUIT_RULESET: Final[str] = "ironman"


def wetsuit_ruleset(name: str | None = None) -> WetsuitRuleset:
    """The named ruleset, or the default.

    An unknown name falls back rather than raising: a course bundle carrying a
    federation this build has not been taught about should still solve, under
    rules that are stated, rather than refuse to produce a plan.
    """
    return WETSUIT_RULESETS.get(name or DEFAULT_WETSUIT_RULESET, WETSUIT_RULESETS["ironman"])


#: Kept as module constants so existing call sites and tests read unchanged.
#: They are the default ruleset's values, not a second source of truth.
WETSUIT_MANDATORY_BELOW_C: Final[float] = WETSUIT_RULESETS["ironman"].mandatory_below_c
WETSUIT_LEGAL_MAX_C: Final[float] = WETSUIT_RULESETS["ironman"].legal_max_c
WETSUIT_NON_AWARD_MAX_C: Final[float] = WETSUIT_RULESETS["ironman"].non_award_max_c

# ---------------------------------------------------------------------------
# Open-water overhead (§4.4.3)
#
# Coaching consensus 5–15 s·(100 m)⁻¹; sighting alone 3–5. **Low confidence**
# throughout. Additive time that a wetsuit does not reduce, which is why the
# order of operations in §4.4.1 is deliberate: the wetsuit multiplies swimming
# pace, then sighting is added.
# ---------------------------------------------------------------------------

OW_OVERHEAD: Final[dict[AthleteLevel, float]] = {
    AthleteLevel.FIRST: 12.0,
    AthleteLevel.IMPROVER: 8.0,
    AthleteLevel.EXPERIENCED: 5.0,
}

# ---------------------------------------------------------------------------
# Water temperature (§4.4.3) — and this is an honest gap.
#
# **The direct effect of water temperature on swim speed within the
# triathlon-legal 16–28 °C range is essentially unstudied.** Everything
# findable is cold-water work at 10–16 °C, concerned with core temperature and
# hypothermia rather than pace. The dominant water-temperature effect in the
# legal range is the wetsuit legality step above, which IS well documented.
#
# These two coefficients are placeholders that produce a small, monotonic,
# correctly-signed effect. They should be treated as such (D-10).
# ---------------------------------------------------------------------------

C_COLD: Final[float] = 0.8
COLD_THRESHOLD_C: Final[float] = 18.0
C_WARM: Final[float] = 1.0
WARM_THRESHOLD_C: Final[float] = 26.0

#: `T_water` is clamped to this band before the adjustment is computed.
WATER_TEMP_CLAMP_MIN: Final[float] = 12.0
WATER_TEMP_CLAMP_MAX: Final[float] = 40.0

# Drafting is **deliberately not modelled** (D-11). The effect is large and
# well documented (15–25% energy saving on feet), but the solver cannot know
# whether an athlete will find feet, and modelling it would mean inventing an
# input. Its absence makes swim projections systematically slightly slow for
# strong swimmers who draft well — a known, directional, documented bias
# rather than a hidden one.
