"""Physical constants. ``SOLVER_MODEL.md`` §A → ``solver/tables/physics.py``.

The rule from §A is absolute: **nothing in this model may be a literal in a
conditional.** If a number appears anywhere in ``solver/`` that is not in one
of these tables, either the table is incomplete or the code is wrong.

Each value carries its source and, where the document assigns one, its
confidence. A constant labelled *estimated* in the document is labelled
estimated here.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Mechanics
# ---------------------------------------------------------------------------

#: Standard gravitational acceleration. Certain.
GRAVITY: Final[float] = 9.80665

# ---------------------------------------------------------------------------
# International Standard Atmosphere (§I.1.1). All four are ISA definitions and
# are certain; nothing would change them.
# ---------------------------------------------------------------------------

P0_PA: Final[float] = 101325.0
LAPSE_RATE_K_PER_M: Final[float] = 0.0065
ISA_EXPONENT: Final[float] = 5.25588
ISA_SEA_LEVEL_TEMP_K: Final[float] = 288.15

#: Specific gas constants, J·kg⁻¹·K⁻¹. Certain.
R_DRY_AIR: Final[float] = 287.058
R_WATER_VAPOUR: Final[float] = 461.495

#: Tetens saturation-vapour-pressure coefficients (§I.1.1). High confidence;
#: a switch to Magnus-Buck would move the result by under 0.1%.
TETENS_A: Final[float] = 610.78
TETENS_B: Final[float] = 17.27
TETENS_C: Final[float] = 237.3

#: Range outside which `forecast.pressure_hpa` is treated as absent (§I.1.1).
PRESSURE_HPA_MIN: Final[float] = 870.0
PRESSURE_HPA_MAX: Final[float] = 1085.0

# ---------------------------------------------------------------------------
# Cycling power–speed (§I.2.2), Martin et al. 1998
# ---------------------------------------------------------------------------

#: Drivetrain efficiency. Martin measured 97.7%; Kyle & Berto and Spicer give
#: 96–98% at 50–200 W. High confidence.
ETA_DRIVETRAIN: Final[float] = 0.976

#: Spoke drag area, m². Martin 1998. **Low confidence — ⚠ VERIFY (§E-4).**
#: About 1.6% of CdA, so an error here is small but unverified.
SPOKE_DRAG_AREA_FW: Final[float] = 0.0044

#: Wheel-bearing friction, `v·(c0 + c1·v)·10⁻³` watts. Martin 1998.
#: **Low confidence — ⚠ VERIFY (§E-4).** Contributes ~1 W; harmless if wrong.
BEARING_C0: Final[float] = 91.0
BEARING_C1: Final[float] = 8.7

# ---------------------------------------------------------------------------
# Speed solve (§I.2.4)
# ---------------------------------------------------------------------------

#: Bisection is **fixed-iteration, never tolerance-terminated** (§0.4): a
#: tolerance test is a platform-dependent branch and would break byte-identical
#: output. Sixty halvings of a 29.5 m·s⁻¹ bracket leave a residual interval of
#: 2.6e-17 m·s⁻¹, below float64 resolution at these magnitudes.
#:
#: Bisection rather than Newton is mandatory: Newton diverges on steep
#: descents, where gravity makes the power function non-monotonic near the
#: lower bound. Observed during modelling — Newton returned 1.8 km·h⁻¹ for a
#: −4.5% descent where bisection returns 61.8.
BISECTION_ITERATIONS: Final[int] = 60
SPEED_BRACKET_LO: Final[float] = 0.5
SPEED_BRACKET_HI: Final[float] = 30.0

#: 75 km·h⁻¹. A safety and realism ceiling: no plan should instruct an
#: age-grouper to hold 80 km·h⁻¹.
V_DESCENT_MAX: Final[float] = 20.83

# ---------------------------------------------------------------------------
# Stage 1 quadrature (§1.1)
# ---------------------------------------------------------------------------

#: Gradient histogram bin width. This is **quadrature, not smoothing**: no
#: node is discarded and no gradient is averaged away, so a 12% pitch stays a
#: 12% pitch and merely shares a bin with 11.9%. The only loss is ±0.00125 of
#: gradient resolution within a bin, worth under 0.1% of time on every terrain
#: type, against a 22× reduction in cost (§0.8).
GRADIENT_BIN_WIDTH: Final[float] = 0.0025

#: Node gradients are clamped to ±30% before use (§1.3). Not smoothing — a
#: guard against a single bad terrain sample producing a 400% gradient that
#: makes a segment unrideable.
NODE_GRADIENT_CLAMP: Final[float] = 0.30

#: If more than this fraction of a leg's nodes clamp, the elevation series is
#: not fit for purpose and the bundle is rejected.
NODE_CLAMP_FAIL_FRACTION: Final[float] = 0.02

# ---------------------------------------------------------------------------
# Input clamps (§I.1.1)
# ---------------------------------------------------------------------------

ELEVATION_MIN_M: Final[float] = -430.0
ELEVATION_MAX_M: Final[float] = 5000.0
TEMP_MIN_C: Final[float] = -20.0
TEMP_MAX_C: Final[float] = 55.0
HUMIDITY_MIN_PCT: Final[float] = 1.0
HUMIDITY_MAX_PCT: Final[float] = 100.0

#: Air density outside this band is a programming error, not an input error
#: (§I.1.1), so it is asserted rather than clamped.
AIR_DENSITY_MIN: Final[float] = 0.55
AIR_DENSITY_MAX: Final[float] = 1.50
