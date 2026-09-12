"""The athlete's side of crowd reports.

The admin side — review, hold, promote — already existed. This is the way a
report comes into being, which is what the crowd-verified provenance tier was
missing: a queue nothing could arrive in.

Nothing here changes a course. A submission produces a ``PENDING`` report and
stops; promotion into a bundle remains an admin act with its own audit trail,
because a course fact is something this system asserts and a stranger's trace
is evidence for that assertion rather than the assertion itself.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile, status

from raceos.api.deps import Config, CurrentUser, DbSession
from raceos.domain.enums import CrowdCategory
from raceos.services import crowd_service

router = APIRouter(prefix="/api/v1", tags=["crowd"])


def _out(report: object) -> dict[str, object]:
    return {
        "id": str(report.id),  # type: ignore[attr-defined]
        "category": report.category.value,  # type: ignore[attr-defined]
        "title": report.title,  # type: ignore[attr-defined]
        "status": report.status.value,  # type: ignore[attr-defined]
        "confidence": report.confidence.value,  # type: ignore[attr-defined]
        "upload_count": report.upload_count,  # type: ignore[attr-defined]
    }


@router.post(
    "/courses/{course_ref}/crowd-reports",
    status_code=status.HTTP_201_CREATED,
    summary="Report something about a course, with an optional GPX trace",
)
def submit_report(
    course_ref: str,
    session: DbSession,
    user: CurrentUser,
    settings: Config,
    category: Annotated[CrowdCategory, Form()],
    title: Annotated[str, Form(max_length=crowd_service.MAX_TITLE)],
    body: Annotated[str, Form(max_length=crowd_service.MAX_BODY)],
    file: Annotated[UploadFile | None, File()] = None,
) -> dict[str, object]:
    """Multipart, because the trace is the point.

    A finding joins the open report for this course and category if one
    exists, so ten athletes reporting the same moved aid station become one
    finding with ten uploads rather than ten findings with one each. Submitting
    again replaces your own upload: one athlete is one observation, however
    many times they press the button.
    """
    data = file.file.read() if file is not None else None
    report = crowd_service.submit(
        session,
        user=user,
        course_ref=course_ref,
        category=category,
        title=title,
        body=body,
        filename=file.filename if file is not None else None,
        data=data,
        settings=settings,
    )
    session.commit()
    return _out(report)


@router.get("/crowd-reports/mine", summary="What I have reported, and what became of it")
def my_reports(session: DbSession, user: CurrentUser) -> list[dict[str, object]]:
    """Somebody who sent a trace is owed the outcome."""
    return [
        _out(report) | {"submitted_at": upload.submitted_at.isoformat()}
        for report, upload in crowd_service.mine(session, user=user)
    ]
