"""Bag rules as data. §A → ``bag_rules.py``.

§6.2: rules are **declarative, never nested conditionals**. Each is a
:class:`BagRule` evaluated in a fixed declaration order, appending at most one
item.

**On continuity.** Bag contents are inherently discrete — an athlete either
carries arm coolers or does not — so the model's continuity requirement cannot
apply here and does not. What *is* required is that the underlying quantities
be continuous, so a rule fires on a stable threshold rather than on a number
that itself jitters. Every threshold below is a config value for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from raceos.domain.enums import BagKey

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Contract-specified. Note it keys off **dry-bulb temperature**, not WBGT,
#: because that is what the contract says and what the athlete reads on a
#: forecast.
ARM_COOLER_TEMP_C: Final[float] = 28.0

#: Head torch fires when the projected finish is within this many minutes of
#: civil dusk. Dusk is **computed from solar position** (§I.1.7), never a fixed
#: clock hour.
DUSK_BUFFER_MIN: Final[float] = 15.0

#: Salt capsule sizing.
SALT_MG_PER_CAPSULE: Final[float] = 300.0

#: Half the leg's need goes in a special-needs bag, since the rest is carried
#: from T1 or taken at aid stations.
SN_FRACTION: Final[float] = 0.5

#: Exactly five bags, always, in this order — even when one is empty. An empty
#: Run Special Needs bag is information, not an omission.
BAG_ORDER: Final[tuple[BagKey, ...]] = (
    BagKey.MORNING,
    BagKey.BIKE_T1,
    BagKey.RUN_T2,
    BagKey.BIKE_SN,
    BagKey.RUN_SN,
)

BAG_LABELS: Final[dict[BagKey, tuple[str, str]]] = {
    BagKey.MORNING: ("Morning Clothes Bag", "Hand in before the swim start"),
    BagKey.BIKE_T1: ("T1 / Bike Bag", "Collected at swim exit"),
    BagKey.RUN_T2: ("T2 / Run Bag", "Collected at bike finish"),
    BagKey.BIKE_SN: ("Bike Special Needs", "Bike leg midpoint"),
    BagKey.RUN_SN: ("Run Special Needs", "Run leg midpoint"),
}


@dataclass(frozen=True)
class BagItemSpec:
    """One item a rule may append.

    ``reason_constraint_key`` is mandatory and must name one of the eight
    athlete keys, a ``barrier:`` key, or a ``model:`` key — the same namespace
    ``bind()`` uses. That is what makes "Why this?" work on a bag item exactly
    as it works on a wattage target, and Stage 6 asserts it as a postcondition
    rather than trusting it.
    """

    bag: BagKey
    name: str
    reason_constraint_key: str
    reason_template: str
    qty: str | None = None
    note: str | None = None


# ---------------------------------------------------------------------------
# The always-present set
# ---------------------------------------------------------------------------

BASE_ITEMS: Final[tuple[BagItemSpec, ...]] = (
    BagItemSpec(
        bag=BagKey.MORNING,
        name="Wetsuit",
        reason_constraint_key="model:wetsuit_legality",
        reason_template="Water {water_temp_c:.1f} °C: {wetsuit_verdict}.",
    ),
    BagItemSpec(
        bag=BagKey.MORNING,
        name="Goggles",
        reason_constraint_key="swim_threshold_pace",
        reason_template="Swim leg planned at {swim_pace_label}.",
    ),
    BagItemSpec(
        bag=BagKey.MORNING,
        name="Timing chip and race belt",
        reason_constraint_key="model:first_timer_set",
        reason_template="Required to record a finish.",
    ),
    BagItemSpec(
        bag=BagKey.BIKE_T1,
        name="Helmet",
        reason_constraint_key="model:if_ceiling",
        reason_template="Mandatory. Bike leg planned at {bike_watts:.0f} w.",
    ),
    BagItemSpec(
        bag=BagKey.BIKE_T1,
        name="Cycling shoes",
        reason_constraint_key="bike_threshold_power",
        reason_template="Bike target {bike_watts:.0f} w over {bike_hours:.1f} h.",
    ),
    BagItemSpec(
        bag=BagKey.BIKE_T1,
        name="Sunglasses",
        reason_constraint_key="model:arm_cooler_threshold",
        reason_template="Forecast {temp_c:.0f} °C, {conditions}.",
    ),
    BagItemSpec(
        bag=BagKey.RUN_T2,
        name="Running shoes",
        reason_constraint_key="run_threshold_pace",
        reason_template="Run target {run_pace_label} over {run_hours:.1f} h.",
    ),
    BagItemSpec(
        bag=BagKey.RUN_T2,
        name="Cap or visor",
        reason_constraint_key="model:run_heat_clamp",
        reason_template="Run in {wbgt:.1f} °C WBGT.",
    ),
)

# ---------------------------------------------------------------------------
# The first-timer set (§6.3)
#
# A defined set of items, not a generic checklist — each carries a reason of
# its own. Spare goggles are here because a goggle failure ends a first-timer's
# race, not because a checklist said "spares".
# ---------------------------------------------------------------------------

FIRST_TIMER_ITEMS: Final[tuple[BagItemSpec, ...]] = (
    BagItemSpec(
        bag=BagKey.MORNING,
        name="Spare goggles",
        reason_constraint_key="model:first_timer_set",
        reason_template="A goggle failure ends a first-timer's race.",
    ),
    BagItemSpec(
        bag=BagKey.BIKE_T1,
        name="Written transition sequence",
        reason_constraint_key="model:first_timer_set",
        reason_template="First race: the order is easy to lose under pressure.",
    ),
    BagItemSpec(
        bag=BagKey.BIKE_T1,
        name="Spare tube, levers and CO2",
        reason_constraint_key="model:first_timer_set",
        reason_template="No outside assistance is permitted on the bike leg.",
    ),
    BagItemSpec(
        bag=BagKey.RUN_T2,
        name="Anti-chafe balm",
        reason_constraint_key="model:first_timer_set",
        reason_template="Run leg planned at {run_hours:.1f} h.",
    ),
)
