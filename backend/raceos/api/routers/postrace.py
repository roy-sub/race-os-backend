"""Post-race: upload a file, get an analysis, calibrate.

The analysis compares against **the plan version that was live at race
time**. Calibration proposals are accepted or dismissed one at a time — a
bulk "apply all" would let one bad derivation ride in behind four good ones.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, UploadFile, status

from raceos.api.deps import Config, CurrentUser, DbSession
from raceos.api.errors import NotFound
from raceos.api.schemas.postrace import (
    AnalyseRequest,
    AnalysisOut,
    CalibrationOut,
    RaceFileOut,
)
from raceos.db.models import AnalysisCalibration, Plan
from raceos.domain.entitlements import EntitlementAction
from raceos.services import billing_service, postrace_service

router = APIRouter(prefix="/api/v1/post-race", tags=["post-race"])


@router.post(
    "/files",
    status_code=status.HTTP_201_CREATED,
    summary="Upload a race file",
)
async def upload_file(
    session: DbSession,
    user: CurrentUser,
    settings: Config,
    file: Annotated[UploadFile, File(description="A .fit, .gpx or .tcx export")],
    plan_id: Annotated[UUID | None, Form()] = None,
) -> RaceFileOut:
    """Parsed on the way in so a bad file is refused with a specific reason.

    The bytes are stored before analysis so a file we cannot read today is
    still there when the parser improves — the athlete does not have to find
    it again.
    """
    plan = session.get(Plan, plan_id) if plan_id else None
    if plan_id is not None and plan is None:
        raise NotFound("Plan not found.")

    data = await file.read()
    record = postrace_service.upload(
        session,
        user=user,
        plan=plan,
        filename=file.filename or "activity",
        data=data,
        settings=settings,
    )
    session.commit()
    return RaceFileOut.model_validate(record)


@router.post("/analyses", summary="Analyse an uploaded file against a plan")
def create_analysis(
    payload: AnalyseRequest,
    session: DbSession,
    user: CurrentUser,
    settings: Config,
) -> AnalysisOut:
    record = postrace_service.get_file(session, file_id=payload.race_file_id, user=user)

    if payload.plan_id is not None:
        plan = session.get(Plan, payload.plan_id)
        if plan is None or plan.user_id != user.id:
            raise NotFound("Plan not found.")
    elif payload.race_id is not None:
        plan = postrace_service.plan_for_race(session, race_id=payload.race_id, user=user)
    elif record.plan_id is not None:
        plan = session.get(Plan, record.plan_id)
        if plan is None:  # pragma: no cover - SET NULL on delete
            raise NotFound("Plan not found.")
    else:
        raise NotFound("Name a plan or a race for this file to be compared against.")

    billing_service.require(
        session,
        user=user,
        action=EntitlementAction.POST_RACE_ANALYSIS,
        race_id=plan.race_id,
    )

    analysis = postrace_service.analyse(
        session, record=record, plan=plan, user=user, settings=settings
    )
    session.commit()
    return AnalysisOut.model_validate(analysis)


@router.get("/analyses", summary="Every analysis this athlete has")
def list_analyses(session: DbSession, user: CurrentUser) -> list[AnalysisOut]:
    return [
        AnalysisOut.model_validate(analysis)
        for analysis in postrace_service.list_analyses(session, user=user)
    ]


@router.get("/analyses/{analysis_id}", summary="One analysis in full")
def get_analysis(analysis_id: UUID, session: DbSession, user: CurrentUser) -> AnalysisOut:
    return AnalysisOut.model_validate(
        postrace_service.get_analysis(session, analysis_id=analysis_id, user=user)
    )


def _calibration(session: DbSession, calibration_id: UUID) -> AnalysisCalibration:
    calibration = session.get(AnalysisCalibration, calibration_id)
    if calibration is None:
        raise NotFound("Calibration not found.")
    return calibration


@router.post(
    "/calibrations/{calibration_id}/apply",
    summary="Write the derived value into your constraints",
)
def apply_calibration(
    calibration_id: UUID, session: DbSession, user: CurrentUser, settings: Config
) -> CalibrationOut:
    """The one path that writes ``source = 'measured'``.

    It goes through the constraint write guard, which requires the actor to
    **be** the athlete — so this cannot be applied on someone's behalf.
    """
    calibration = _calibration(session, calibration_id)
    analysis = postrace_service.get_analysis(
        session, analysis_id=calibration.analysis_id, user=user
    )
    plan = session.get(Plan, analysis.plan_id)
    billing_service.require(
        session,
        user=user,
        action=EntitlementAction.CONSTRAINT_CALIBRATION,
        race_id=plan.race_id if plan else None,
    )

    postrace_service.apply_calibration(session, calibration=calibration, user=user)
    session.commit()
    return CalibrationOut.model_validate(calibration)


@router.post("/calibrations/{calibration_id}/dismiss", summary="Keep the value you have")
def dismiss_calibration(
    calibration_id: UUID, session: DbSession, user: CurrentUser
) -> CalibrationOut:
    calibration = _calibration(session, calibration_id)
    postrace_service.get_analysis(session, analysis_id=calibration.analysis_id, user=user)
    postrace_service.dismiss_calibration(session, calibration=calibration, user=user)
    session.commit()
    return CalibrationOut.model_validate(calibration)
