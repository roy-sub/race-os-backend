"""Run pace constants. §A → ``run_model.py``.

The pace chain is multiplicative and each factor is independently sourced and
independently inspectable — which is what makes the "Why this?" drawer
possible at all::

    pace_target = run_threshold_pace × D_dist × D_bike × D_heat ÷ alt_factor
    pace_i      = pace_target × D_grade(g_i)          # per segment
"""

from __future__ import annotations

from typing import Final

from raceos.domain.enums import AthleteLevel, SolverDistance

# ---------------------------------------------------------------------------
# Distance decay (§4.3.1) — Riegel (1977), anchored on the one-hour threshold
# distance from §2.3.
#
# The level split is an inference from Vickers & Vertosick (2016, n = 2303),
# who found Riegel's 1.06 optimistic at marathon for about half of recreational
# runners — not a published table. **Low-Med confidence.** ⚠ VERIFY (§E-11):
# the same exponent is used by the §2.5.2 threshold conversion, so an error
# here moves both the anchor and the extrapolation.
# ---------------------------------------------------------------------------

RIEGEL_R: Final[dict[AthleteLevel, float]] = {
    AthleteLevel.FIRST: 1.08,
    AthleteLevel.IMPROVER: 1.07,
    AthleteLevel.EXPERIENCED: 1.06,
}

#: Below 3 km the Riegel form is unreliable; a race shorter than this is
#: refused rather than converted (§2.5.2).
RIEGEL_MIN_RACE_KM: Final[float] = 3.0

# ---------------------------------------------------------------------------
# Bike coupling (§4.3.1)
#
# **This is the weakest-evidenced part of the model and it is load-bearing.**
# No peer-reviewed dose–response relating triathlon run pace to bike intensity
# factor exists; what exists is coaching-derived ("exceed target IF by
# 0.03–0.05 and you run materially slower"). The *mechanism* is not in doubt —
# the bike sets the rate of glycogen depletion, and long-course run failure is
# overwhelmingly a fuelling-and-pacing failure rather than a running-fitness
# one — but the *coefficient* is an estimate.
#
# c₀ at full distance was chosen so the model's cool-conditions triathlon run
# lands at +8% over its own predicted open-marathon pace, consistent with the
# low end of the widely quoted "10–15% slower than an equivalent open
# marathon" band. c₁ = 1.6 makes a +0.05 IF overshoot cost +8% of run pace.
#
# Both are `estimated`. Both are top calibration targets. Neither should ever
# be described to a user as though it were measured.
# ---------------------------------------------------------------------------

BIKE_COUPLING_C0: Final[dict[SolverDistance, float]] = {
    SolverDistance.FULL: 0.08,
    SolverDistance.HALF: 0.05,
    SolverDistance.OLYMPIC: 0.03,
    SolverDistance.SPRINT: 0.02,
}

#: All distances. Also sets where the free IF optimum sits — see
#: `intensity.py`'s module docstring.
BIKE_COUPLING_C1: Final[float] = 1.6

# ---------------------------------------------------------------------------
# Gradient (§4.3.2)
#
# Minetti et al. (2002), J Appl Physiol 93:1039–1046 — metabolic cost of
# running as a 5th-order polynomial in gradient, J·kg⁻¹·m⁻¹, valid
# −0.45 ≤ i ≤ 0.45.
#
# **Partially self-verified**, which is unusual in this document: the
# polynomial reproduces two of the paper's own reported measurements to within
# 1 SD (Cr(+0.45) = 19.43 against 18.93 ± 1.74; Cr(−0.20) = 1.800 against the
# reported minimum 1.73 ± 0.36). Confidence here is higher than elsewhere.
# ⚠ VERIFY (§E-5) remains open for the coefficients themselves.
#
# Note Cr(0) = 3.6 is the polynomial's intercept while the measured level cost
# was 3.40. Because D_grade is a *ratio* to Cr(0), that discrepancy cancels
# exactly and affects no output.
# ---------------------------------------------------------------------------

#: Highest order first: 155.4·i⁵ − 30.4·i⁴ − 43.3·i³ + 46.3·i² + 19.5·i + 3.6
MINETTI_COEFFS: Final[tuple[float, float, float, float, float, float]] = (
    155.4,
    -30.4,
    -43.3,
    46.3,
    19.5,
    3.6,
)
MINETTI_VALID_MIN: Final[float] = -0.45
MINETTI_VALID_MAX: Final[float] = 0.45

# **Cost ratio is not a pace ratio.** At i = −0.20 the metabolic cost halves,
# but nobody runs a marathon descent at double pace — eccentric loading and
# biomechanical speed limits intervene. So the conversion is damped
# asymmetrically: D_grade(i) = clamp((Cr(i)/Cr(0))^α, min, max).

#: At constant effort, uphill metabolic cost converts near-fully to pace.
#: Medium confidence.
ALPHA_UP: Final[float] = 1.00

#: **Estimate. Low confidence** — no published damping coefficient found.
ALPHA_DN: Final[float] = 0.50

#: **Estimate. Low confidence** — a biomechanical descent limit.
D_GRADE_MIN: Final[float] = 0.85

#: **Estimate. Low confidence.** A modelling convenience, not physiology:
#: above roughly +13% most long-course athletes walk, which has a different
#: cost curve (Minetti gives one) and is **not modelled** (D-9).
D_GRADE_MAX: Final[float] = 2.00
