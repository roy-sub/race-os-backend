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

from raceos.db.session import session_scope
from raceos.ingest.bundle_loader import BundleValidationError, load_bundle_directory
from raceos.logging import configure_logging, get_logger

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE_DIR = REPO_ROOT / "pipelines" / "course-ingest" / "out" / "bundles"


def seed_courses(bundle_dir: Path | None = None) -> int:
    """Load every generated bundle. Returns how many were loaded."""
    directory = bundle_dir or BUNDLE_DIR
    if not directory.is_dir():
        logger.warning(
            "no bundle directory; skipping course seed",
            extra={"bundle_dir": str(directory)},
        )
        return 0

    with session_scope() as session:
        results = load_bundle_directory(session, directory)

    for result in results:
        logger.info(
            "seeded course",
            extra={
                "course_slug": result.slug,
                "bundle_version": result.version,
                "newly_created": result.created,
                "segments": result.segments,
                "barriers": result.barriers,
                "aid_stations": result.aid_stations,
            },
        )
    return len(results)


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
