"""Stage 6 — Pack the bags (~0.2 s). ``SOLVER_MODEL.md`` §6.

Exactly five bags, always, in a fixed order — **even when one is empty.** An
empty Run Special Needs bag is information, not an omission.

**Every item carries ``reason_constraint_key`` and ``reason_text``.** An item
with no upstream justification cannot be emitted, and that is asserted as a
stage postcondition rather than left as a convention. It is what makes
"Why this?" work on a bag item exactly as it works on a wattage target: the
reason key must name one of the eight athlete keys, a ``barrier:`` key, or a
``model:`` key — the same namespace ``bind()`` uses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from raceos.domain.enums import CONSTRAINT_KEYS, AthleteLevel, BagKey
from raceos.solver.environment import civil_dusk_minutes
from raceos.solver.models import Bag, BagItem, EventSpec, ForecastSnapshot
from raceos.solver.profile import RaceProfile
from raceos.solver.stages.s2_athlete import AthleteState
from raceos.solver.stages.s5_fuelling import FuellingResult
from raceos.solver.tables import bag_rules as bags_tbl
from raceos.solver.tables import precedence as prec_tbl


@dataclass(frozen=True)
class BagContext:
    """Everything a rule's reason template may interpolate."""

    temp_c: float
    conditions: str
    water_temp_c: float
    wetsuit_verdict: str
    wbgt: float
    swim_pace_label: str
    run_pace_label: str
    bike_watts: float
    bike_hours: float
    run_hours: float

    def as_fields(self) -> dict[str, object]:
        return {
            "temp_c": self.temp_c,
            "conditions": self.conditions,
            "water_temp_c": self.water_temp_c,
            "wetsuit_verdict": self.wetsuit_verdict,
            "wbgt": self.wbgt,
            "swim_pace_label": self.swim_pace_label,
            "run_pace_label": self.run_pace_label,
            "bike_watts": self.bike_watts,
            "bike_hours": self.bike_hours,
            "run_hours": self.run_hours,
        }


def _pace_label(seconds_per_unit: float) -> str:
    total = int(round(seconds_per_unit))
    return f"{total // 60}:{total % 60:02d}"


def build_context(profile: RaceProfile, forecast: ForecastSnapshot, wbgt_c: float) -> BagContext:
    if profile.swim.wetsuit:
        verdict = "wetsuit legal"
    elif profile.swim.wetsuit_warning:
        verdict = "wetsuit permitted but not award-eligible, so racing without"
    else:
        verdict = "wetsuit prohibited"

    return BagContext(
        temp_c=forecast.temp_c,
        conditions=forecast.conditions.replace("_", " "),
        water_temp_c=forecast.water_temp_c,
        wetsuit_verdict=verdict,
        wbgt=wbgt_c,
        swim_pace_label=f"{_pace_label(profile.swim.pace_sec_per_100m)}/100m",
        run_pace_label=f"{_pace_label(profile.run.pace_target_sec_per_km)}/km",
        bike_watts=profile.bike.average_power,
        bike_hours=profile.bike.minutes / 60.0,
        run_hours=profile.run.minutes / 60.0,
    )


def head_torch_required(
    profile: RaceProfile, event: EventSpec, night_flag: bool
) -> tuple[bool, float | None]:
    """§6.3. Computed from solar position, **never a fixed clock hour**.

    Returns ``(required, dusk_local_minutes)``. When ``civil_dusk`` returns
    ``None`` — polar day or night — the decision falls back to
    ``options.night_flag`` alone. No RaceOS course is above the Arctic Circle,
    but the branch must exist rather than raise.
    """
    dusk = civil_dusk_minutes(
        event.event_date.year,
        event.event_date.month,
        event.event_date.day,
        event.lat,
        event.lng,
        event.utc_offset_hours,
    )
    if dusk is None:
        return night_flag, None

    start = event.start_time_local.hour * 60.0 + event.start_time_local.minute
    finish = start + profile.total_minutes
    threshold = dusk - bags_tbl.DUSK_BUFFER_MIN
    return (finish > threshold or night_flag), dusk


def salt_capsule_count(sodium_mg_per_hr: float, leg_hours: float) -> int:
    """§6.3. Half the leg's need, since the rest is carried or taken en route.

    The reason key is ``sodium_loss``, which is correct and important: the
    quantity traces to the athlete's own measured sweat sodium concentration,
    and the drawer should say so rather than naming a model constant.
    """
    needed = sodium_mg_per_hr * leg_hours * bags_tbl.SN_FRACTION
    return max(0, math.ceil(needed / bags_tbl.SALT_MG_PER_CAPSULE))


def pack_bags(
    profile: RaceProfile,
    athlete: AthleteState,
    fuelling: FuellingResult,
    forecast: ForecastSnapshot,
    event: EventSpec,
    *,
    wbgt_c: float,
    night_flag: bool,
) -> tuple[Bag, ...]:
    """Stage 6. Rules are evaluated in fixed declaration order.

    Each rule appends at most one item, and the postcondition below asserts
    that every emitted item carries a reason key from the ``bind()`` namespace.
    """
    context = build_context(profile, forecast, wbgt_c)
    fields = context.as_fields()
    contents: dict[BagKey, list[BagItem]] = {key: [] for key in bags_tbl.BAG_ORDER}

    def add(
        bag: BagKey,
        name: str,
        reason_key: str,
        reason_template: str,
        qty: str | None = None,
        note: str | None = None,
    ) -> None:
        contents[bag].append(
            BagItem(
                name=name,
                qty=qty,
                note=note,
                reason_constraint_key=reason_key,
                reason_text=reason_template.format(**fields),
            )
        )

    # --- the always-present set --------------------------------------
    for spec in bags_tbl.BASE_ITEMS:
        add(spec.bag, spec.name, spec.reason_constraint_key, spec.reason_template, spec.qty)

    # --- arm coolers (§6.3) ------------------------------------------
    # Keys off DRY-BULB temperature, not WBGT, because that is what the
    # contract says and what the athlete reads on a forecast.
    if forecast.temp_c > bags_tbl.ARM_COOLER_TEMP_C:
        add(
            BagKey.BIKE_T1,
            "Arm coolers",
            "model:arm_cooler_threshold",
            f"Forecast {{temp_c:.0f}} °C is above the "
            f"{bags_tbl.ARM_COOLER_TEMP_C:.0f} °C threshold.",
        )

    # --- head torch (§6.3) -------------------------------------------
    torch, dusk = head_torch_required(profile, event, night_flag)
    if torch:
        if dusk is None:
            reason = "Night finish flagged; civil dusk is undefined at this latitude."
        else:
            reason = (
                f"Projected finish {_clock(event, profile.total_minutes)} is within "
                f"{bags_tbl.DUSK_BUFFER_MIN:.0f} min of civil dusk "
                f"{_minutes_to_clock(dusk)}."
            )
        add(BagKey.RUN_SN, "Head torch", "model:dusk_buffer", reason)

    # --- salt capsules (§6.3) ----------------------------------------
    for bag, leg_hours, label in (
        (BagKey.BIKE_SN, profile.bike.minutes / 60.0, "bike"),
        (BagKey.RUN_SN, profile.run.minutes / 60.0, "run"),
    ):
        count = salt_capsule_count(fuelling.sodium_mg_per_hr, leg_hours)
        if count > 0:
            add(
                bag,
                "Salt capsules",
                "sodium_loss",
                f"{athlete.sodium_loss:.0f} mg·L⁻¹ sweat sodium at "
                f"{fuelling.sweat_effective_l_per_hr:.2f} L·h⁻¹ over "
                f"{leg_hours:.1f} h on the {label}.",
                qty=str(count),
            )

    # --- special-needs fuel ------------------------------------------
    for bag, leg_hours in (
        (BagKey.BIKE_SN, profile.bike.minutes / 60.0),
        (BagKey.RUN_SN, profile.run.minutes / 60.0),
    ):
        grams = fuelling.carb_g_per_hr * leg_hours * bags_tbl.SN_FRACTION
        add(
            bag,
            "Gels",
            "gut_carb_ceiling",
            f"{fuelling.carb_g_per_hr:.0f} g·h⁻¹ over {leg_hours:.1f} h, "
            f"half carried from here.",
            qty=f"{math.ceil(grams / 25.0)}",
            note=f"≈{grams:.0f} g of carbohydrate",
        )

    # --- first-timer set (§6.3) --------------------------------------
    if athlete.level is AthleteLevel.FIRST:
        for spec in bags_tbl.FIRST_TIMER_ITEMS:
            add(
                spec.bag,
                spec.name,
                spec.reason_constraint_key,
                spec.reason_template,
                spec.qty,
            )

    bags = tuple(
        Bag(
            key=key,
            name=bags_tbl.BAG_LABELS[key][0],
            when_label=bags_tbl.BAG_LABELS[key][1],
            items=tuple(contents[key]),
        )
        for key in bags_tbl.BAG_ORDER
    )

    _assert_every_item_has_a_reason(bags)
    return bags


def _clock(event: EventSpec, elapsed_minutes: float) -> str:
    start = event.start_time_local.hour * 60.0 + event.start_time_local.minute
    return _minutes_to_clock(start + elapsed_minutes)


def _minutes_to_clock(minutes: float) -> str:
    total = int(round(minutes)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _assert_every_item_has_a_reason(bags: tuple[Bag, ...]) -> None:
    """The stage postcondition (§6.1). Asserted, not assumed.

    Also checks the key is in the ``bind()`` namespace, so a typo in a rule
    fails here rather than reaching a user's drawer as a key nothing can
    explain.
    """
    if len(bags) != len(bags_tbl.BAG_ORDER):
        raise AssertionError(f"expected {len(bags_tbl.BAG_ORDER)} bags, got {len(bags)}")

    valid = set(CONSTRAINT_KEYS) | set(prec_tbl.MODEL_LIMIT_KEYS)
    for bag in bags:
        for item in bag.items:
            key = item.reason_constraint_key
            if not key:
                raise AssertionError(f"bag item {item.name!r} in {bag.key.value} has no reason key")
            if not (
                key in valid
                or key.startswith("barrier:")
                or key.startswith("model:")
                or key.startswith("options:")
            ):
                raise AssertionError(
                    f"bag item {item.name!r} carries reason key {key!r}, which is "
                    f"outside the bind() namespace"
                )
            if not item.reason_text.strip():
                raise AssertionError(f"bag item {item.name!r} has an empty reason text")
