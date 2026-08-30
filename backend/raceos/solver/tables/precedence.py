"""Tie-breaking order for `bind()`, and binding-key resolution. §A.

§0.5: ties in :func:`~raceos.solver.bind.bind` break by **position in the
candidate tuple, lowest index wins** — a fixed, configured precedence list per
quantity, never the iteration order of a runtime collection.

That is not a hypothetical: a first-timer whose gut ceiling exactly equals the
duration-based carbohydrate target is a common case, and which key gets named
in the "Why this?" drawer must not depend on dictionary ordering.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Candidate order per bound quantity. Earlier entries win a tie.
# ---------------------------------------------------------------------------

CARB_PRECEDENCE: Final[tuple[str, ...]] = (
    "model:carb_duration_target",
    "gut_carb_ceiling",
)

FLUID_PRECEDENCE: Final[tuple[str, ...]] = (
    "sweat_rate",
    "model:gastric_emptying_cap",
)

SODIUM_PRECEDENCE: Final[tuple[str, ...]] = (
    "sodium_loss",
    "model:acsm_sodium_floor",
)

CAFFEINE_PRECEDENCE: Final[tuple[str, ...]] = (
    "model:caffeine_dose_per_kg",
    "caffeine_tolerance",
)

BINDING_PRECEDENCE: Final[dict[str, tuple[str, ...]]] = {
    "carb_g_per_hr": CARB_PRECEDENCE,
    "fluid_ml_per_hr": FLUID_PRECEDENCE,
    "sodium_mg_per_hr": SODIUM_PRECEDENCE,
    "caffeine_mg_total": CAFFEINE_PRECEDENCE,
}

# ---------------------------------------------------------------------------
# `SolveOutput.binding_constraint_key` resolution (§3.5)
#
# Fixed order:
#   1. If any gate's margin_minutes < margin_clear_min -> `barrier:<name>` of
#      the tightest gate. **A cut-off in play outranks everything**: it is what
#      the athlete needs to know.
#   2. Otherwise, the binding key of the quantity that determined the largest
#      leg by projected time — in practice bike_threshold_power or
#      run_threshold_pace, whichever leg is longer.
#   3. Otherwise `model:if_ceiling`.
# ---------------------------------------------------------------------------

BINDING_FALLBACK_KEY: Final[str] = "model:if_ceiling"

#: Every `model:` limit key the solver can emit. Collected here so a typo in a
#: call site fails a test rather than reaching a user's "Why this?" drawer as a
#: key nothing can explain.
MODEL_LIMIT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "model:acsm_sodium_floor",
        "model:arm_cooler_threshold",
        "model:caffeine_dose_per_kg",
        "model:carb_duration_target",
        "model:carb_hard_max",
        "model:dusk_buffer",
        "model:first_timer_set",
        "model:gastric_emptying_cap",
        "model:if_ceiling",
        "model:if_segment_ceiling",
        "model:run_heat_clamp",
        "model:vi_ceiling",
        "model:wetsuit_legality",
    }
)
