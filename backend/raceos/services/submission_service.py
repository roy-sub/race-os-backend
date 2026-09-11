"""Athlete-submitted courses: upload, build, load, and the failures in between.

The product promise this implements is narrow and worth stating: **an athlete
racing an event we have not built yet is not blocked on our release
schedule.** They hold the route files already — the athlete guide's GPX, or a
line they traced from the published course map — and those files are enough.

What comes out the other end is not a lesser object. It is a real
:class:`~raceos.db.models.Course` with a real bundle, validated by
:mod:`raceos.ingest.bundle_loader` against the same invariants the official
catalogue is held to, solved by the same solver. Two things separate it from a
catalogue course, and both are honest rather than punitive:

* ``courses.submitted_by_user_id`` makes it **private to the athlete who added
  it**. They uploaded a file that may be an organiser's licensed course data;
  republishing it to everyone is not ours to do.
* Its provenance is ``ESTIMATED`` and its attribution says the route came from
  the athlete. Nothing claims it is an organiser's published course.

The build runs inline, on the request, and that is a deliberate choice rather
than a gap: V1 has no queue (Part 3.4), a 90 km route is a few seconds of
arithmetic plus a few dozen DEM tiles, and an athlete who has just uploaded
three files is still watching. A job that "will finish eventually" with no
queue to finish it would be worse in every way.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raceos.api.errors import Conflict, InvalidInput, NotFound
from raceos.config import Settings
from raceos.db.models import Course, CourseSubmission, User
from raceos.domain.enums import (
    CourseAvailability,
    CourseVisibility,
    DistanceType,
    Leg,
    SubmissionStatus,
)
from raceos.ingest import gpx_course
from raceos.ingest.bundle_loader import BundleValidationError, load_bundle_payload
from raceos.ingest.elevation import ElevationError, ElevationSource, TerrariumTiles
from raceos.logging import get_logger
from raceos.storage.base import get_storage_backend

logger = get_logger(__name__)

#: Storage key per leg, on the submission's own prefix.
FILE_KEY_FIELD: dict[Leg, str] = {
    Leg.SWIM: "swim_file_key",
    Leg.BIKE: "bike_file_key",
    Leg.RUN: "run_file_key",
}

#: How many courses one athlete may add. Not a paywall — a submission costs
#: DEM traffic and storage, and an account that has uploaded fifty is a script
#: rather than a triathlete.
MAX_SUBMISSIONS_PER_USER = 25


def _slugify(value: str) -> str:
    """``IRONMAN 70.3 Poreč`` -> ``ironman-70-3-porec``."""
    ascii_form = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_form).strip("-")
    return slug or "course"


def unique_slug(session: Session, base: str, user: User) -> str:
    """A slug nobody else's course already holds.

    Suffixed with a short piece of the athlete's id rather than a counter, so
    two athletes submitting the same event do not race each other for
    ``-2``, and neither can discover from a slug that the other exists.
    """
    candidate = f"{_slugify(base)}-{str(user.id)[:6]}"
    if session.scalar(select(Course).where(Course.slug == candidate)) is None:
        return candidate
    suffix = 2
    while session.scalar(select(Course).where(Course.slug == f"{candidate}-{suffix}")) is not None:
        suffix += 1
    return f"{candidate}-{suffix}"


# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------


def create(
    session: Session,
    *,
    user: User,
    name: str,
    place: str,
    timezone: str,
    distance_type: DistanceType,
    lat: float,
    lng: float,
    country: str | None = None,
    event_date: Any = None,
    start_time_local: Any = None,
    notes: str | None = None,
) -> CourseSubmission:
    existing = session.scalar(
        select(func.count())
        .select_from(CourseSubmission)
        .where(CourseSubmission.user_id == user.id)
    )
    if (existing or 0) >= MAX_SUBMISSIONS_PER_USER:
        raise Conflict(
            f"You have added {existing} courses, which is the limit. Delete "
            f"one you no longer need before adding another."
        )

    submission = CourseSubmission(
        user_id=user.id,
        status=SubmissionStatus.DRAFT,
        name=name.strip(),
        place=place.strip(),
        country=(country or "").strip().upper()[:2] or None,
        timezone=timezone.strip(),
        distance_type=distance_type,
        lat=lat,
        lng=lng,
        event_date=event_date,
        start_time_local=start_time_local,
        notes=(notes or "").strip() or None,
        file_names={},
        problems=[],
    )
    session.add(submission)
    session.flush()
    return submission


def owned(session: Session, *, user: User, submission_id: UUID) -> CourseSubmission:
    submission = session.get(CourseSubmission, submission_id)
    if submission is None or submission.user_id != user.id:
        raise NotFound("Submission not found.")
    return submission


def list_for(session: Session, *, user: User) -> list[CourseSubmission]:
    return list(
        session.scalars(
            select(CourseSubmission)
            .where(CourseSubmission.user_id == user.id)
            .order_by(CourseSubmission.created_at.desc())
        )
    )


def attach_file(
    session: Session,
    *,
    submission: CourseSubmission,
    leg: Leg,
    filename: str,
    data: bytes,
    settings: Settings,
) -> CourseSubmission:
    """Store one leg's route file against the submission.

    Parsed here, before it is stored, rather than at build time. An athlete who
    picked the wrong file finds out while they are still looking at the file
    picker, and a file that is not GPX never reaches storage at all.
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

    key = f"course-submissions/{submission.id}/{leg.value.lower()}.gpx"
    get_storage_backend(settings).put(key, data, content_type="application/gpx+xml")

    setattr(submission, FILE_KEY_FIELD[leg], key)
    names = dict(submission.file_names or {})
    names[leg.value] = filename
    submission.file_names = names
    # Replacing a file on a failed submission puts it back in the queue: the
    # old problems were about the old file.
    if submission.status in (SubmissionStatus.FAILED, SubmissionStatus.READY):
        submission.problems = []
    submission.status = (
        SubmissionStatus.QUEUED if _has_all_files(submission) else SubmissionStatus.DRAFT
    )
    session.flush()
    logger.info(
        "submission file attached",
        extra={
            "submission_id": str(submission.id),
            "leg": leg.value,
            "points": len(points),
        },
    )
    return submission


def _has_all_files(submission: CourseSubmission) -> bool:
    return all(getattr(submission, field) for field in FILE_KEY_FIELD.values())


def missing_legs(submission: CourseSubmission) -> list[Leg]:
    return [leg for leg, field in FILE_KEY_FIELD.items() if not getattr(submission, field)]


def _elevation_source(settings: Settings) -> ElevationSource:
    return TerrariumTiles(
        settings.elevation_tile_url,
        zoom=settings.elevation_sample_zoom,
        timeout_seconds=settings.elevation_request_timeout_seconds,
        attribution=settings.elevation_attribution,
    )


def process(
    session: Session,
    *,
    user: User,
    submission: CourseSubmission,
    settings: Settings,
    elevation: ElevationSource | None = None,
) -> CourseSubmission:
    """Build the bundle, validate it, and load it. Or fail with reasons.

    ``elevation`` is injectable so the suite can run this whole path offline
    with a real, deterministic implementation rather than reaching for the
    network — the same pattern payments and storage already use.
    """
    missing = missing_legs(submission)
    if missing:
        raise Conflict(
            "Upload a route file for "
            + ", ".join(leg.value.lower() for leg in missing)
            + " before submitting."
        )

    submission.status = SubmissionStatus.PROCESSING
    submission.problems = []
    session.flush()

    storage = get_storage_backend(settings)
    files: dict[Leg, tuple[str, bytes]] = {}
    names = dict(submission.file_names or {})
    for leg, field in FILE_KEY_FIELD.items():
        key = getattr(submission, field)
        files[leg] = (names.get(leg.value, f"{leg.value.lower()}.gpx"), storage.get(key))

    source = elevation or _elevation_source(settings)
    owns_source = elevation is None
    try:
        request = gpx_course.BuildRequest(
            slug=unique_slug(session, submission.name, user),
            name=submission.name,
            place=submission.place,
            timezone=submission.timezone,
            distance_type=submission.distance_type,
            lat=float(submission.lat),
            lng=float(submission.lng),
            season_year=(
                submission.event_date.year if submission.event_date else datetime.now(UTC).year
            ),
        )
        result = gpx_course.build_bundle(request, files, elevation=source)
    except ElevationError as exc:
        return _fail(session, submission, [str(exc)])
    finally:
        if owns_source:
            source.close()

    if not result.ok:
        return _fail(session, submission, result.problems)

    try:
        load = load_bundle_payload(session, result.payload, source=f"submission:{submission.id}")
    except BundleValidationError as exc:
        # The same validator the official bundles go through. An athlete's
        # course is held to the same standard, and the problems it found are
        # theirs to see.
        return _fail(session, submission, exc.problems)

    course = session.scalar(select(Course).where(Course.slug == load.slug))
    if course is None:  # pragma: no cover - the loader just wrote it
        return _fail(session, submission, ["The course could not be stored. Try again."])

    course.submitted_by_user_id = user.id
    course.visibility = CourseVisibility.CATALOGUE
    course.availability = CourseAvailability.AVAILABLE
    course.next_edition_date = submission.event_date
    course.official_event_name = submission.name

    submission.course_id = course.id
    submission.status = SubmissionStatus.READY
    submission.problems = []
    submission.processed_at = datetime.now(UTC)
    session.flush()

    logger.info(
        "athlete course submission built",
        extra={
            "submission_id": str(submission.id),
            "course_slug": course.slug,
            "legs": load.legs,
            "segments": load.segments,
        },
    )
    return submission


def _fail(session: Session, submission: CourseSubmission, problems: list[str]) -> CourseSubmission:
    submission.status = SubmissionStatus.FAILED
    submission.problems = problems
    submission.processed_at = datetime.now(UTC)
    session.flush()
    logger.info(
        "athlete course submission rejected",
        extra={"submission_id": str(submission.id), "problem_count": len(problems)},
    )
    return submission


def delete(session: Session, *, submission: CourseSubmission, settings: Settings) -> None:
    """Remove a submission, and the course it built if nothing depends on it.

    A course with a race entered against it stays: deleting it would orphan
    that race and every plan under it. The submission row goes either way, so
    the athlete's list is theirs to tidy.
    """
    from raceos.db.models import Race

    storage = get_storage_backend(settings)
    for field in FILE_KEY_FIELD.values():
        key = getattr(submission, field)
        if key:
            storage.delete(key)

    if submission.course_id is not None:
        entered = session.scalar(select(Race).where(Race.course_id == submission.course_id))
        course = session.get(Course, submission.course_id)
        if entered is not None and course is not None:
            course.visibility = CourseVisibility.RETIRED
        elif course is not None:
            for bundle in list(course.bundles):
                for leg in list(bundle.legs):
                    session.delete(leg)
                session.delete(bundle)
            session.flush()
            session.delete(course)

    session.delete(submission)
    session.flush()
