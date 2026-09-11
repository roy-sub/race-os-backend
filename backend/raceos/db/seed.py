"""Seed data. Idempotent, re-runnable: ``make seed``.

Seeds the generated course bundles, then people, plans, drift events, coach
links, share links, invoices in three currencies, crowd reports, incidents and
thirty days of KPI history.

**Every seeded plan is produced by the real solver.** Nothing is hand-written
into ``plans`` or its children: a fixture with fabricated splits would let a
solver regression pass a demo, which is exactly backwards.

**No secret is seeded.** Development passwords are generated at random per run
and printed once to the operator's terminal. Nothing is written to a file, and
re-running produces new ones — there is no default password in this
repository.

**No geometry is fabricated.** The bundles are loaded from
``pipelines/course-ingest/out/`` exactly as generated; six further courses have
finished specs marked ``status: pending`` and are deliberately not built, so
the directory shows three until those are generated.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.db.catalogue import CATALOGUE, SHIPPING_BUNDLE_SLUGS
from raceos.db.models import Course
from raceos.db.session import session_scope
from raceos.domain.enums import CourseAvailability, CourseVisibility
from raceos.ingest.bundle_loader import BundleValidationError, load_bundle_file
from raceos.logging import configure_logging, get_logger

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE_DIR = REPO_ROOT / "pipelines" / "course-ingest" / "out" / "bundles"


def seed_courses(bundle_dir: Path | None = None) -> int:
    """Bring the course directory in line with the declared catalogue.

    Three steps, in this order, because each depends on the one before:

    1. Load the bundles named by the catalogue — and only those. Loading the
       whole directory would put test fixtures on the live site.
    2. Write the catalogue row for every event, bundled or not, so a
       coming-soon race is listed honestly rather than missing.
    3. Retire anything in ``courses`` that the catalogue no longer names.
       Retired rather than deleted: a course with a solved plan against it
       cannot be removed without orphaning that plan, so it is made invisible
       instead and the athlete keeps everything they paid for.

    Returns how many catalogue rows are now present.
    """
    directory = bundle_dir or BUNDLE_DIR
    if not directory.is_dir():
        logger.warning(
            "no bundle directory; skipping course seed",
            extra={"bundle_dir": str(directory)},
        )
        return 0

    with session_scope() as session:
        for slug in SHIPPING_BUNDLE_SLUGS:
            path = directory / f"{slug}.bundle.json"
            if not path.is_file():
                logger.warning(
                    "catalogue names a bundle that is not generated",
                    extra={"course_slug": slug, "path": str(path)},
                )
                continue
            result = load_bundle_file(session, path)
            logger.info(
                "seeded course bundle",
                extra={
                    "course_slug": result.slug,
                    "bundle_version": result.version,
                    "newly_created": result.created,
                    "segments": result.segments,
                    "barriers": result.barriers,
                    "aid_stations": result.aid_stations,
                },
            )

        applied = apply_catalogue(session)
        retired = retire_uncatalogued(session)

    logger.info(
        "catalogue applied",
        extra={"courses": applied, "retired": retired},
    )
    return applied


def apply_catalogue(session: Session) -> int:
    """Write every catalogue row, creating the ones with no bundle.

    A bundled course already has its geometry-derived facts from the loader,
    so only the catalogue's own columns are written over it: availability,
    visibility and the announced date. An unbundled course is created whole
    from the manifest — it is a listing, and a listing is all it claims to be.
    """
    for entry in CATALOGUE:
        course = session.scalar(select(Course).where(Course.slug == entry.slug))
        if course is None:
            course = Course(slug=entry.slug)
            session.add(course)
        if not entry.has_bundle:
            course.name = entry.name
            course.place = entry.place
            course.distance_type = entry.distance_type
            course.difficulty = entry.difficulty
            course.timezone = entry.timezone
            course.lat = entry.lat
            course.lng = entry.lng
            course.tone_color = entry.tone_color
        course.official_event_name = entry.name
        course.availability = entry.availability
        course.visibility = entry.visibility
        course.next_edition_date = entry.event_date
        course.is_fictional = entry.visibility is CourseVisibility.SHOWCASE
        course.submitted_by_user_id = None
    session.flush()
    return len(CATALOGUE)


def retire_uncatalogued(session: Session) -> int:
    """Hide every course the catalogue does not name.

    Athlete-submitted courses are left alone: they are not part of the
    official catalogue and were never meant to be.
    """
    named = {entry.slug for entry in CATALOGUE}
    retired = 0
    for course in session.scalars(select(Course).where(Course.submitted_by_user_id.is_(None))):
        if course.slug in named:
            continue
        if course.visibility is CourseVisibility.RETIRED:
            continue
        course.visibility = CourseVisibility.RETIRED
        course.availability = CourseAvailability.COMING_SOON
        retired += 1
        logger.info("retired course not in the catalogue", extra={"course_slug": course.slug})
    session.flush()
    return retired


def seed_all() -> dict[str, Any]:
    """Courses first, then everything that depends on them."""
    from raceos.db.seed_people import seed_people

    courses = seed_courses()
    with session_scope() as session:
        summary = seed_people(session)
    return {"courses_loaded": courses, **summary}


def main() -> int:
    """``--courses-only`` seeds the course library and nothing else.

    That is the production mode. The full seed also creates thirteen example
    athletes with solved plans, which is exactly right for a laptop and
    exactly wrong for a live database — real users would be sharing a course
    directory with Elena Marsh.
    """
    parser = argparse.ArgumentParser(description="Seed the RaceOS database.")
    parser.add_argument(
        "--courses-only",
        action="store_true",
        help="Load course bundles only. Use this on a live deployment.",
    )
    args = parser.parse_args()

    configure_logging(service="raceos-seed")
    try:
        if args.courses_only:
            count = seed_courses()
            logger.info("seed complete", extra={"courses_loaded": count})
            return 0
        summary = seed_all()
    except BundleValidationError as exc:
        # A bundle that fails validation is a stop, not a warning: seeding a
        # bad bundle would put geometry the solver rejects into the database
        # and the failure would resurface hours later, on an athlete.
        logger.error("bundle failed validation", extra={"source": exc.source})
        for problem in exc.problems:
            logger.error("  %s", problem)
        return 1
    passwords: dict[str, str] = summary.pop("new_passwords", {})
    logger.info("seed complete", extra=summary)

    if passwords:
        # Printed, never logged and never written to a file: a structured log
        # is shipped somewhere, and a credential in a log is a credential in a
        # log aggregator. These are throwaway values for a local database.
        print("\nDevelopment sign-ins (generated fresh this run, not stored):")
        for email, password in sorted(passwords.items()):
            print(f"  {email:34s} {password}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
