"""Athlete-contributed findings about a course.

The admin side of this already existed: a report could be reviewed, held or
promoted into a bundle. What was missing was the way a report comes into
being. Without it the crowd-verified provenance tier could never fill itself —
the queue existed and nothing could ever arrive in it.

Three rules shape the whole module.

**One athlete is one observation.** The unique index on
``(crowd_report_id, user_id)`` is what makes that true in the data, and this
module respects it rather than working around it: submitting again replaces
your own upload instead of adding a second. Somebody who uploads the same
trace forty times still counts once, so confidence cannot be manufactured by
repetition.

**Findings group, they do not multiply.** A submission joins the open report
for that course and category if one exists. Ten athletes reporting the same
moved aid station must become one finding with ten uploads, not ten findings
with one each — the second shape looks like ten unrelated problems and never
reaches the promotion threshold.

**Nothing an athlete submits changes a course.** A submission only ever
produces a ``PENDING`` report. Promotion into a bundle stays an admin act,
because a course fact is something this system asserts, and a stranger's GPX
trace is evidence for that assertion rather than the assertion itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.errors import InvalidInput, NotFound
from raceos.config import Settings
from raceos.db.models import Course, CrowdReport, CrowdReportUpload, User
from raceos.domain.enums import CrowdCategory, CrowdConfidence, CrowdStatus
from raceos.ingest import gpx_course
from raceos.logging import get_logger
from raceos.storage.base import get_storage_backend

logger = get_logger(__name__)

#: A title long enough to say what changed, short enough to scan in a queue.
MAX_TITLE = 120
#: The body carries the detail. Longer than a title, shorter than an essay.
MAX_BODY = 2000


def _open_report(
    session: Session, *, course_id: UUID, category: CrowdCategory
) -> CrowdReport | None:
    """The report this finding should join, if there is one.

    Only ``PENDING`` reports are joined. A resolved one is a decision that was
    already taken, and re-opening it by adding an upload would erase the
    reviewer's judgement without anyone noticing.
    """
    return session.scalar(
        select(CrowdReport).where(
            CrowdReport.course_id == course_id,
            CrowdReport.category == category,
            CrowdReport.status == CrowdStatus.PENDING,
        )
    )


def _store_gpx(
    *, report_id: UUID, user_id: UUID, filename: str, data: bytes, settings: Settings
) -> dict[str, object]:
    """Validate and store one trace, returning what the upload should record.

    Parsed before it is stored, exactly as an athlete-submitted course is: the
    person who picked the wrong file finds out while the file picker is still
    in front of them, and a file that is not GPX never reaches storage.
    """
    if len(data) > settings.upload_max_bytes:
        raise InvalidInput(
            f"{filename} is {len(data) / 1_048_576:.1f} MB, and the limit is "
            f"{settings.upload_max_bytes / 1_048_576:.0f} MB. A route exported "
            f"without heart-rate and power channels is far smaller.",
            field="file",
        )
    try:
        points = gpx_course.parse_gpx(data, filename=filename)
    except gpx_course.GpxError as exc:
        raise InvalidInput(str(exc), field="file") from exc

    key = f"crowd-reports/{report_id}/{user_id}.gpx"
    get_storage_backend(settings).put(key, data, content_type="application/gpx+xml")
    return {"gpx_key": key, "gpx_filename": filename, "gpx_points": len(points)}


def submit(
    session: Session,
    *,
    user: User,
    course_ref: str,
    category: CrowdCategory,
    title: str,
    body: str,
    filename: str | None = None,
    data: bytes | None = None,
    settings: Settings,
) -> CrowdReport:
    """Record one athlete's finding about a course.

    Returns the report the finding joined or created — always ``PENDING``, and
    never a promise that anything will change.
    """
    title, body = title.strip(), body.strip()
    if not title:
        raise InvalidInput("Say what changed, in a few words.", field="title")
    if not body:
        raise InvalidInput(
            "Describe what you saw. A reviewer has to be able to act on it.", field="body"
        )

    course = session.scalar(select(Course).where(Course.slug == course_ref))
    if course is None:
        raise NotFound("Course not found.")

    report = _open_report(session, course_id=course.id, category=category)
    if report is None:
        report = CrowdReport(
            course_id=course.id,
            category=category,
            title=title[:MAX_TITLE],
            body=body[:MAX_BODY],
            status=CrowdStatus.PENDING,
            confidence=CrowdConfidence.LOW,
            upload_count=0,
            agreement_weight_pct=0.0,
            affected_plans_count=0,
        )
        session.add(report)
        session.flush()

    payload: dict[str, object] = {"note": body[:MAX_BODY]}
    if data is not None:
        payload |= _store_gpx(
            report_id=report.id,
            user_id=user.id,
            filename=filename or "route.gpx",
            data=data,
            settings=settings,
        )

    existing = session.scalar(
        select(CrowdReportUpload).where(
            CrowdReportUpload.crowd_report_id == report.id,
            CrowdReportUpload.user_id == user.id,
        )
    )
    if existing is not None:
        # Correcting your own submission, not adding a second voice.
        existing.payload = payload
        existing.submitted_at = datetime.now(UTC)
    else:
        session.add(
            CrowdReportUpload(
                crowd_report_id=report.id,
                user_id=user.id,
                payload=payload,
                submitted_at=datetime.now(UTC),
            )
        )
    session.flush()

    # Re-score from the uploads that now exist. `assess_crowd_report` counts
    # distinct users, so this stays honest whichever branch above ran.
    from raceos.services import admin_service

    verdict = admin_service.assess_crowd_report(session, report=report, settings=settings)
    report.upload_count = verdict.upload_count
    report.confidence = verdict.confidence
    session.flush()

    logger.info(
        "crowd.submitted",
        extra={
            "report_id": str(report.id),
            "course_slug": course.slug,
            "category": category.value,
            "uploads": verdict.upload_count,
            "with_gpx": data is not None,
        },
    )
    return report


def mine(session: Session, *, user: User) -> list[tuple[CrowdReport, CrowdReportUpload]]:
    """Everything this athlete has reported, and what became of it.

    Somebody who took the trouble to send a trace is owed the outcome; a
    contribution surface with no way to see what happened teaches people to
    stop contributing.
    """
    rows = session.execute(
        select(CrowdReport, CrowdReportUpload)
        .join(CrowdReportUpload, CrowdReportUpload.crowd_report_id == CrowdReport.id)
        .where(CrowdReportUpload.user_id == user.id)
        .order_by(CrowdReportUpload.submitted_at.desc())
    ).all()
    return [(report, upload) for report, upload in rows]
