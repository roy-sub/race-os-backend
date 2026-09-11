"""Adding a race the catalogue does not carry yet.

Four calls, in the order an athlete meets them: describe the event, upload one
route file per leg, submit, then enter the race like any other. The upload is a
separate call per leg on purpose — three large files in one multipart body is a
single timeout away from having to re-pick all three, and a leg that fails
validation should not take the other two down with it.

Everything here is scoped to the caller. A submission belongs to the athlete
who made it, and so does the course it produces: see
:mod:`raceos.services.submission_service` for why an athlete's uploaded route
is not republished to everyone.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Path, UploadFile, status

from raceos.api.deps import Config, CurrentUser, DbSession
from raceos.api.schemas.submission import (
    SubmissionCreate,
    SubmissionOut,
    SubmissionUpdate,
)
from raceos.domain.enums import Leg
from raceos.services import submission_service

router = APIRouter(prefix="/api/v1/course-submissions", tags=["course submissions"])


def _out(submission: object) -> SubmissionOut:
    out = SubmissionOut.model_validate(submission)
    out.missing_legs = [leg.value for leg in submission_service.missing_legs(submission)]  # type: ignore[arg-type]
    return out


@router.post("", status_code=status.HTTP_201_CREATED, summary="Start adding a race")
def create_submission(
    payload: SubmissionCreate, session: DbSession, user: CurrentUser
) -> SubmissionOut:
    """Describe the event. The route files follow, one call per leg."""
    submission = submission_service.create(
        session,
        user=user,
        name=payload.name,
        place=payload.place,
        country=payload.country,
        timezone=payload.timezone,
        distance_type=payload.distance_type,
        lat=payload.lat,
        lng=payload.lng,
        event_date=payload.event_date,
        start_time_local=payload.start_time_local,
        notes=payload.notes,
    )
    session.commit()
    return _out(submission)


@router.get("", summary="Races this athlete has added")
def list_submissions(session: DbSession, user: CurrentUser) -> list[SubmissionOut]:
    return [_out(row) for row in submission_service.list_for(session, user=user)]


@router.get("/{submission_id}", summary="One submission, with its problems")
def get_submission(submission_id: UUID, session: DbSession, user: CurrentUser) -> SubmissionOut:
    return _out(submission_service.owned(session, user=user, submission_id=submission_id))


@router.patch("/{submission_id}", summary="Correct the details before submitting")
def update_submission(
    submission_id: UUID, payload: SubmissionUpdate, session: DbSession, user: CurrentUser
) -> SubmissionOut:
    submission = submission_service.owned(session, user=user, submission_id=submission_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(submission, field, value)
    session.commit()
    return _out(submission)


@router.put(
    "/{submission_id}/files/{leg}",
    summary="Upload the route file for one leg",
)
def upload_leg_file(
    submission_id: UUID,
    leg: Annotated[Leg, Path(description="SWIM, BIKE or RUN")],
    session: DbSession,
    user: CurrentUser,
    settings: Config,
    file: Annotated[UploadFile, File(description="A GPX file for this leg")],
) -> SubmissionOut:
    """One leg's GPX.

    Validated before it is stored, so an athlete who picked the wrong file is
    told while the file picker is still in front of them rather than after all
    three uploads and a build.
    """
    submission = submission_service.owned(session, user=user, submission_id=submission_id)
    data = file.file.read()
    submission_service.attach_file(
        session,
        submission=submission,
        leg=leg,
        filename=file.filename or f"{leg.value.lower()}.gpx",
        data=data,
        settings=settings,
    )
    session.commit()
    return _out(submission)


@router.post("/{submission_id}/submit", summary="Build the course")
def submit(
    submission_id: UUID, session: DbSession, user: CurrentUser, settings: Config
) -> SubmissionOut:
    """Run the ingest: resample, sample terrain, segment, and load.

    Returns ``200`` whether the build succeeded or failed — a submission that
    was rejected is a *state of the submission*, with its reasons attached,
    not a failed request. The athlete's files are still there and replacing one
    and re-submitting is the recovery path.
    """
    submission = submission_service.owned(session, user=user, submission_id=submission_id)
    submission_service.process(session, user=user, submission=submission, settings=settings)
    session.commit()
    return _out(submission)


@router.delete(
    "/{submission_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Remove a submission",
)
def delete_submission(
    submission_id: UUID, session: DbSession, user: CurrentUser, settings: Config
) -> None:
    submission = submission_service.owned(session, user=user, submission_id=submission_id)
    submission_service.delete(session, submission=submission, settings=settings)
    session.commit()
