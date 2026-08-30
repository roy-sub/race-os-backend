"""Athlete constraints, and the estimators that fill them in.

Every write goes through ``constraint_service.write_constraint``, which takes
the actor explicitly and refuses anyone who is not the owning athlete. There is
no coach-facing variant of any endpoint here, and there is no admin one.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from raceos.api.deps import Config, CurrentUser, DbSession, Warnings
from raceos.api.errors import InvalidInput
from raceos.api.schemas.constraint import (
    ConstraintHistoryOut,
    ConstraintOut,
    ConstraintWrite,
    EstimateOut,
    EstimateRequest,
)
from raceos.config import Settings
from raceos.db.models import Constraint
from raceos.domain.enums import CONSTRAINT_KEYS, ConstraintSource
from raceos.services import constraint_service

router = APIRouter(prefix="/api/v1/constraints", tags=["constraints"])

ConstraintKey = Annotated[str, Path(description="One of the eight canonical keys")]


def _serialise(constraint: Constraint, settings: Settings) -> ConstraintOut:
    out = ConstraintOut.model_validate(constraint)
    out.stale = constraint_service.is_stale(constraint, settings)
    return out


@router.get("", summary="Current values with provenance")
def list_constraints(
    session: DbSession, settings: Config, user: CurrentUser, warnings: Warnings
) -> list[ConstraintOut]:
    rows = constraint_service.list_constraints(session, athlete_id=user.id)
    constraint_service.attach_staleness_warnings(rows, warnings, settings)
    return [_serialise(row, settings) for row in rows]


@router.put("/{key}", summary="Set a constraint value")
def put_constraint(
    key: ConstraintKey,
    payload: ConstraintWrite,
    session: DbSession,
    settings: Config,
    user: CurrentUser,
) -> ConstraintOut:
    constraint = constraint_service.write_constraint(
        session,
        athlete_id=user.id,
        actor=user,
        key=key,
        value=payload.value,
        source=payload.source,
        evidence_note=payload.evidence_note,
        measured_at_temp_c=payload.measured_at_temp_c,
        change_reason="athlete edit",
    )
    session.commit()
    return _serialise(constraint, settings)


@router.get("/{key}/history", summary="Every previous value for one key")
def get_history(
    key: ConstraintKey, session: DbSession, user: CurrentUser
) -> list[ConstraintHistoryOut]:
    return [
        ConstraintHistoryOut.model_validate(row)
        for row in constraint_service.constraint_history(session, athlete_id=user.id, key=key)
    ]


@router.post("/{key}/estimate", summary="Estimate a constraint from two questions")
def estimate(
    key: ConstraintKey,
    payload: EstimateRequest,
    session: DbSession,
    settings: Config,
    user: CurrentUser,
) -> EstimateOut:
    """Produces a value stamped ``estimated`` and writes it.

    An estimated value carries **full numeric weight** in the solver (§0.6).
    The estimator's job is to produce the athlete's real number as well as two
    questions can, not to produce a cautious one.
    """
    if key not in CONSTRAINT_KEYS:
        raise InvalidInput(f"{key!r} is not a constraint.", field="key")

    answers = payload.answers
    try:
        if key == "run_threshold_pace":
            result = constraint_service.estimate_run_threshold_pace(
                race_km=float(answers["race_km"]),
                race_seconds=float(answers["race_seconds"]),
                level=user.level,
            )
        elif key == "bike_threshold_power":
            result = constraint_service.estimate_bike_threshold_power(
                weight_kg=float(answers["weight_kg"]), level=user.level
            )
        elif key == "swim_threshold_pace":
            result = constraint_service.estimate_swim_threshold_pace(
                t400_seconds=float(answers["t400_seconds"]),
                t200_seconds=float(answers["t200_seconds"]),
            )
        elif key == "sweat_rate":
            result = constraint_service.estimate_sweat_rate(
                weight_before_kg=float(answers["weight_before_kg"]),
                weight_after_kg=float(answers["weight_after_kg"]),
                fluid_ml=float(answers["fluid_ml"]),
                minutes=float(answers["minutes"]),
            )
        elif key == "weight":
            result = constraint_service.estimate_weight(weight_kg=float(answers["weight_kg"]))
        elif key == "sodium_loss":
            result = constraint_service.estimate_sodium_loss(
                salty_sweater=bool(answers.get("salty_sweater", False)), level=user.level
            )
        elif key == "gut_carb_ceiling":
            result = constraint_service.estimate_gut_carb_ceiling(
                trained_gut=bool(answers.get("trained_gut", False)), level=user.level
            )
        else:
            result = constraint_service.estimate_caffeine_tolerance(
                daily_cups=float(answers.get("daily_cups", 0)),
                weight_kg=float(answers.get("weight_kg", 70)),
            )
    except KeyError as exc:
        raise InvalidInput(
            f"The {key} estimator needs {exc.args[0]!r}.", field=str(exc.args[0])
        ) from exc
    except (TypeError, ValueError) as exc:
        raise InvalidInput(str(exc), field=key) from exc

    constraint_service.write_constraint(
        session,
        athlete_id=user.id,
        actor=user,
        key=result.key,
        value=result.value,
        source=ConstraintSource.ESTIMATED,
        source_detail=f"estimator {constraint_service.ESTIMATOR_VERSION}",
        confidence_pct=result.confidence_pct,
        evidence_note=result.evidence_note,
        change_reason="guided estimator",
    )
    session.commit()
    return EstimateOut(
        key=result.key,
        value=result.value,
        unit=result.unit,
        confidence_pct=result.confidence_pct,
        evidence_note=result.evidence_note,
        applied=True,
    )
