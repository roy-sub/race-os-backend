"""Athlete constraints: read, write, history, estimators.

**The first structural guarantee lives here.** Every write goes through
:func:`write_constraint`, whose ``actor`` parameter is not advisory — it
raises :class:`ForbiddenStructural` when the actor is not the owning athlete,
whoever they are and whatever permissions they hold. There is no coach path, no
admin path and no bulk path around it, because there is no *other function*.

That is the difference between a guarantee and a permission: a permission is a
column somebody can set, and this is an absence.

Provenance (Law 2) travels with every value forever. Four sources exist, and
after Part 0.4 C1 removed device integrations there are exactly four inbound
routes: manual entry, file upload, a guided estimator, and post-race
calibration write-back. Every one stamps a ``source``; there is no
"unspecified" state once a value exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.errors import ErrorCode, ForbiddenStructural, InvalidInput, WarningCollector
from raceos.config import Settings
from raceos.db.models import Constraint, ConstraintHistory, User
from raceos.domain.enums import (
    CONSTRAINT_KEYS,
    CONSTRAINT_UNITS,
    AthleteLevel,
    ConstraintSource,
)
from raceos.solver.stages.s2_athlete import threshold_pace_from_race
from raceos.solver.tables import plausibility as plaus

#: Per-key staleness windows. Weight goes stale faster than FTP, which is why
#: this is a table rather than one global number (BACKENDREQUIREMENTS §17.5).
STALENESS_DAYS: dict[str, int] = {
    "weight": 60,
    "sweat_rate": 365,
    "sodium_loss": 365,
    "gut_carb_ceiling": 270,
    "caffeine_tolerance": 365,
}


def _now() -> datetime:
    return datetime.now(UTC)


def staleness_days(key: str, settings: Settings) -> int:
    return STALENESS_DAYS.get(key, settings.constraint_staleness_days_default)


def is_stale(constraint: Constraint, settings: Settings) -> bool:
    """Only ``tested`` and ``measured`` values go stale.

    A ``manual`` or ``estimated`` value was never a measurement, so calling it
    "six months old" would imply a precision it never had.
    """
    if constraint.source not in (ConstraintSource.TESTED, ConstraintSource.MEASURED):
        return False
    reference = constraint.tested_at or constraint.measured_at or constraint.updated_at
    if reference is None:  # pragma: no cover - updated_at is NOT NULL
        return False
    return reference < _now() - timedelta(days=staleness_days(constraint.key, settings))


def validate_value(key: str, value: float) -> None:
    """The same table the solver re-asserts (§2.2).

    Checked here so the athlete gets ``INVALID_INPUT`` with a useful message
    before a solve is ever attempted, rather than a solver exception after.
    """
    if key not in CONSTRAINT_KEYS:
        raise InvalidInput(f"{key!r} is not a constraint.", field="key")
    limits = plaus.PLAUSIBILITY[key]
    if not limits.minimum <= value <= limits.maximum:
        raise InvalidInput(
            f"{value:g} {limits.unit} is outside the plausible range "
            f"{limits.minimum:g}-{limits.maximum:g} {limits.unit}. "
            f"Check the units.",
            field=key,
            details={"min": limits.minimum, "max": limits.maximum, "unit": limits.unit},
        )


def list_constraints(session: Session, *, athlete_id: UUID) -> list[Constraint]:
    return list(
        session.scalars(
            select(Constraint).where(Constraint.user_id == athlete_id).order_by(Constraint.key)
        )
    )


def attach_staleness_warnings(
    constraints: list[Constraint], warnings: WarningCollector, settings: Settings
) -> None:
    """``STALE_DATA`` rides alongside a 200, never replacing it.

    A plan built on a six-month-old FTP is still a plan; refusing to return it
    would be worse than returning it with the caveat attached.
    """
    for constraint in constraints:
        if is_stale(constraint, settings):
            warnings.add(
                ErrorCode.STALE_DATA,
                f"Your {constraint.key.replace('_', ' ')} was last updated more than "
                f"{staleness_days(constraint.key, settings)} days ago.",
                constraint.key,
            )


def write_constraint(
    session: Session,
    *,
    athlete_id: UUID,
    actor: User,
    key: str,
    value: float,
    source: ConstraintSource,
    source_detail: str | None = None,
    confidence_pct: int | None = None,
    evidence_note: str | None = None,
    measured_at_temp_c: float | None = None,
    change_reason: str | None = None,
) -> Constraint:
    """**The only way a constraint value is ever written.**

    ``actor`` must be the owning athlete. Not "must have permission" — must
    *be* them. A coach with every permission granted, an admin, a support agent
    under a live grant and a bulk script all fail here identically, because
    none of them is the athlete.

    Raising :class:`ForbiddenStructural` rather than :class:`Forbidden` is
    deliberate: it is a distinct error code, so a test can assert the
    structural guarantee produced the rejection rather than an ordinary
    permission check that somebody could later loosen.
    """
    if actor.id != athlete_id:
        raise ForbiddenStructural(
            "An athlete's constraints can only be written by that athlete. "
            "No coach, admin or support role can write them, at any permission "
            "level.",
            field=key,
            details={"athlete_id": str(athlete_id), "actor_id": str(actor.id)},
        )

    validate_value(key, value)

    existing = session.scalar(
        select(Constraint).where(Constraint.user_id == athlete_id, Constraint.key == key)
    )

    if existing is not None:
        # Append-only history, written on every value or source change, before
        # the current row moves. Never updated, never deleted.
        if existing.value != value or existing.source != source:
            session.add(
                ConstraintHistory(
                    user_id=athlete_id,
                    key=existing.key,
                    value=existing.value,
                    unit=existing.unit,
                    source=existing.source,
                    source_detail=existing.source_detail,
                    confidence_pct=existing.confidence_pct,
                    evidence_note=existing.evidence_note,
                    tested_at=existing.tested_at,
                    measured_at=existing.measured_at,
                    measured_at_temp_c=existing.measured_at_temp_c,
                    superseded_at=_now(),
                    change_reason=change_reason,
                    changed_by_user_id=actor.id,
                )
            )
        constraint = existing
    else:
        constraint = Constraint(user_id=athlete_id, key=key)
        session.add(constraint)

    constraint.value = value
    constraint.unit = CONSTRAINT_UNITS[key]
    constraint.source = source
    constraint.source_detail = source_detail
    constraint.confidence_pct = confidence_pct
    constraint.evidence_note = evidence_note
    if measured_at_temp_c is not None:
        constraint.measured_at_temp_c = measured_at_temp_c
    if source is ConstraintSource.TESTED:
        constraint.tested_at = _now()
    if source is ConstraintSource.MEASURED:
        constraint.measured_at = _now()

    session.flush()
    return constraint


def get_value(session: Session, *, athlete_id: UUID, key: str) -> float | None:
    """One constraint's current value, or ``None`` if it has never been set.

    A read, deliberately unguarded: provenance and the write path are what
    matter for constraints, and every caller here already holds the athlete.
    """
    row = session.scalar(
        select(Constraint).where(Constraint.user_id == athlete_id, Constraint.key == key)
    )
    return float(row.value) if row is not None else None


def constraint_history(session: Session, *, athlete_id: UUID, key: str) -> list[ConstraintHistory]:
    return list(
        session.scalars(
            select(ConstraintHistory)
            .where(ConstraintHistory.user_id == athlete_id, ConstraintHistory.key == key)
            .order_by(ConstraintHistory.created_at.desc())
        )
    )


# ---------------------------------------------------------------------------
# Guided estimators (Part 9.1)
#
# Two plain-language questions per constraint, producing a value stamped
# `estimated`. An estimated value carries **full numeric weight** in the
# solver — §0.6 — and lower trust only in the UI. The estimator's job is to
# produce the athlete's real number as well as two questions can, not to
# produce a cautious one.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    key: str
    value: float
    unit: str
    confidence_pct: int
    evidence_note: str


ESTIMATOR_VERSION = "v1"


def estimate_run_threshold_pace(
    *, race_km: float, race_seconds: float, level: AthleteLevel
) -> Estimate:
    """From a recent race result, via the solver's own Riegel exponent.

    Deliberately calls into ``solver.stages.s2_athlete`` rather than
    reimplementing the conversion: a second copy of ``riegel_r`` would let the
    two drift, and every back-test would then silently mix two populations
    (§2.5).
    """
    pace = threshold_pace_from_race(race_km, race_seconds, level)
    return Estimate(
        key="run_threshold_pace",
        value=round(pace, 1),
        unit=CONSTRAINT_UNITS["run_threshold_pace"],
        # Confidence falls as the extrapolation lengthens: a 10 km result is a
        # short hop to one-hour pace, a marathon result is not.
        confidence_pct=80 if 8.0 <= race_km <= 25.0 else 60,
        evidence_note=f"From {race_km:g} km in {int(race_seconds // 60)} min",
    )


def estimate_bike_threshold_power(*, weight_kg: float, level: AthleteLevel) -> Estimate:
    """Watts per kilogram by level, when the athlete has never tested.

    These are population mid-points, and they are the crudest estimator here —
    hence the low confidence. FTP is the single largest lever on a bike split,
    so an athlete who can test should, and the UI says so.
    """
    watts_per_kg = {
        AthleteLevel.FIRST: 2.2,
        AthleteLevel.IMPROVER: 2.8,
        AthleteLevel.EXPERIENCED: 3.4,
    }[level]
    return Estimate(
        key="bike_threshold_power",
        value=round(weight_kg * watts_per_kg),
        unit=CONSTRAINT_UNITS["bike_threshold_power"],
        confidence_pct=45,
        evidence_note=f"{watts_per_kg} w/kg for a {level.value} athlete",
    )


def estimate_swim_threshold_pace(*, t400_seconds: float, t200_seconds: float) -> Estimate:
    """Critical Swim Speed from the standard 400/200 pair.

    ``CSS = 200 / (t400 − t200)`` metres per second, converted to seconds per
    100 m. This is a *derivation*, not really an estimate, which is why its
    confidence is high — the athlete performed the test.

    §D-12 notes that ``D′`` also falls straight out of this pair and the
    product currently discards both times. Persisting them would replace a
    population default with a measurement at zero extra cost to the athlete;
    it is recorded there as the obvious fifth input.
    """
    if t400_seconds <= t200_seconds:
        raise InvalidInput(
            "The 400 m time must be longer than the 200 m time.", field="t400_seconds"
        )
    speed = 200.0 / (t400_seconds - t200_seconds)
    pace = 100.0 / speed
    return Estimate(
        key="swim_threshold_pace",
        value=round(pace, 1),
        unit=CONSTRAINT_UNITS["swim_threshold_pace"],
        confidence_pct=85,
        evidence_note=f"CSS from {t400_seconds:g}s / {t200_seconds:g}s",
    )


def estimate_sweat_rate(
    *, weight_before_kg: float, weight_after_kg: float, fluid_ml: float, minutes: float
) -> Estimate:
    """The standard weigh-in/weigh-out test.

    ``(mass lost + fluid drunk) / duration``. The athlete should also record
    the *temperature* they tested at — §F.4's ``measured_at_temp_c`` — because
    without it the solver assumes 15 °C WBGT, and a test run on a hot day
    against one run indoors in winter can differ by 20% in the resulting
    fluid plan.
    """
    if minutes <= 0:
        raise InvalidInput("Duration must be positive.", field="minutes")
    lost_litres = (weight_before_kg - weight_after_kg) + (fluid_ml / 1000.0)
    rate = lost_litres / (minutes / 60.0)
    return Estimate(
        key="sweat_rate",
        value=round(rate, 2),
        unit=CONSTRAINT_UNITS["sweat_rate"],
        confidence_pct=75,
        evidence_note=f"{lost_litres:.2f} L over {minutes:g} min",
    )


def estimate_weight(*, weight_kg: float) -> Estimate:
    """Not really an estimate — but it keeps every route through one door."""
    return Estimate(
        key="weight",
        value=round(weight_kg, 1),
        unit=CONSTRAINT_UNITS["weight"],
        confidence_pct=95,
        evidence_note="Entered directly",
    )


def estimate_sodium_loss(*, salty_sweater: bool, level: AthleteLevel) -> Estimate:
    """Two questions, because a sweat sodium test needs a laboratory.

    Baker 2017 reports 10-90 mmol/L (230-2070 mg/L) across athletes. Without a
    test, "do you taste salt / get white marks on your kit" is the only signal
    available, and the confidence says so.
    """
    value = 1200.0 if salty_sweater else 800.0
    return Estimate(
        key="sodium_loss",
        value=value,
        unit=CONSTRAINT_UNITS["sodium_loss"],
        confidence_pct=35,
        evidence_note="Self-reported salty sweater" if salty_sweater else "Typical range",
    )


def estimate_gut_carb_ceiling(*, trained_gut: bool, level: AthleteLevel) -> Estimate:
    """What the athlete has actually tolerated in training.

    Pfeiffer et al. measured Ironman athletes consuming 62 ± 26 g/h, so the
    untrained default sits near that observed centre rather than at the
    literature's 90 g/h recommendation. §5.1 is explicit that the onboarding
    default is where an over-prescription problem would originate, not in the
    solver.
    """
    value = 90.0 if trained_gut else 60.0
    return Estimate(
        key="gut_carb_ceiling",
        value=value,
        unit=CONSTRAINT_UNITS["gut_carb_ceiling"],
        confidence_pct=50,
        evidence_note=(
            "Practised high-carb fuelling" if trained_gut else "Untrained gut, typical range"
        ),
    )


def estimate_caffeine_tolerance(*, daily_cups: float, weight_kg: float) -> Estimate:
    """Habituation, capped at the ISSN's evidenced ceiling.

    6 mg/kg is the top of the 3-6 mg/kg range the ISSN supports; a habitual
    drinker tolerates the top of it, an abstainer should not start on race day.
    """
    ceiling = 6.0 * weight_kg
    fraction = min(1.0, 0.4 + 0.2 * daily_cups)
    return Estimate(
        key="caffeine_tolerance",
        value=round(min(ceiling, ceiling * fraction) / 5) * 5,
        unit=CONSTRAINT_UNITS["caffeine_tolerance"],
        confidence_pct=55,
        evidence_note=f"{daily_cups:g} caffeinated drinks a day",
    )
