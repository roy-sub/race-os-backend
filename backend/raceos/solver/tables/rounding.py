"""Output precision, and the one rounding helper. §A → ``rounding.py``.

§0.4: **rounding happens once, at ``SolveOutput`` construction, through a
single helper.** Never mid-computation, never via ``repr``.

The cross-stage consistency rule that follows from it: downstream stages
consume **unrounded** values. ``total_carb_g`` is computed from the unrounded
rate and unrounded duration, then rounded — never from the rounded rate.
Otherwise Stage 5's arithmetic-consistency invariant fails on long races.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

# ---------------------------------------------------------------------------
# Precisions (§0.4)
# ---------------------------------------------------------------------------

TARGET_WATTS_DP: Final[int] = 0  # 1 W
TARGET_PACE_SEC_PER_KM_DP: Final[int] = 0  # 1 s·km⁻¹
SWIM_PACE_DP: Final[int] = 0  # 1 s·(100 m)⁻¹
MINUTES_DP: Final[int] = 1  # 0.1 min
CARB_G_DP: Final[int] = 0  # 1 g
PERCENT_DP: Final[int] = 1  # 0.1

#: Quantities rounded to the nearest multiple rather than to a decimal place.
FLUID_ML_STEP: Final[float] = 10.0
SODIUM_MG_STEP: Final[float] = 10.0
CAFFEINE_MG_STEP: Final[float] = 5.0


def round_half_even(value: float, places: int) -> float:
    """Round *value* to *places* decimal places, half to even.

    Uses :class:`~decimal.Decimal` rather than the built-in :func:`round`.
    They agree on most inputs, but ``round()`` operates on the binary double,
    so a value that is *displayed* as an exact half may not *be* one — the
    classic ``round(2.675, 2) == 2.67``. Going through the decimal
    representation makes the result depend on the number as written rather
    than on its binary neighbourhood, which is what "byte-identical output on
    every platform" requires.
    """
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(repr(value)).quantize(quantum, rounding=ROUND_HALF_EVEN))


def round_to_step(value: float, step: float) -> float:
    """Round *value* to the nearest multiple of *step*, half to even."""
    if step <= 0:  # pragma: no cover - a misconfigured table
        raise ValueError(f"step must be positive, got {step}")
    scaled = Decimal(repr(value)) / Decimal(repr(step))
    rounded = scaled.quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return float(rounded * Decimal(repr(step)))
