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

#: Ironman competition rules. High confidence — but these are **rules, not
#: physics**, and they change: ⚠ VERIFY (§E-12) against the current season's
#: rules annually. The thresholds are genuinely discontinuous (24.5 °C and
#: 24.6 °C produce different equipment, hence a ~4.5% pace step); that
#: discontinuity is in the rules, and smoothing it would be wrong.
WETSUIT_MANDATORY_BELOW_C: Final[float] = 16.0
WETSUIT_LEGAL_MAX_C: Final[float] = 24.5
WETSUIT_NON_AWARD_MAX_C: Final[float] = 28.77

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
