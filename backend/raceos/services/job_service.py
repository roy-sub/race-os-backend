"""The scheduled-job registry.

V1 ships **no Redis and no Celery**. Every background job is an ordinary
service function exposed at ``/internal/jobs/{name}`` and called by an
external cron. This module is the registry and the run recorder: what ran,
when, how long, what it touched, and what failed.

Recording every run is what makes the jobs observable without a task queue's
result backend — and what lets a job be idempotent in practice, because a run
can look up what the previous one covered.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.errors import NotFound
from raceos.config import Settings
from raceos.db.models import JobRun
from raceos.logging import get_logger

logger = get_logger(__name__)

#: A job takes the session and settings and returns its own counters.
JobFn = Callable[[Session, Settings], dict[str, Any]]


@dataclass(frozen=True)
class Job:
    name: str
    description: str
    run: JobFn
    #: The cadence an operator should configure. Documentation, not a
    #: scheduler — nothing here runs itself.
    suggested_cron: str


_REGISTRY: dict[str, Job] = {}


def register(name: str, *, description: str, suggested_cron: str) -> Callable[[JobFn], JobFn]:
    def decorator(fn: JobFn) -> JobFn:
        _REGISTRY[name] = Job(
            name=name, description=description, run=fn, suggested_cron=suggested_cron
        )
        return fn

    return decorator


def registry() -> dict[str, Job]:
    return dict(_REGISTRY)


def get_job(name: str) -> Job:
    job = _REGISTRY.get(name)
    if job is None:
        raise NotFound(f"No job named {name!r}. Known jobs: {', '.join(sorted(_REGISTRY))}.")
    return job


def run_job(
    session: Session, *, name: str, settings: Settings, request_id: str | None = None
) -> JobRun:
    """Execute a job and record the run, **including when it fails**.

    A job that failed silently is indistinguishable from one the cron never
    called, so the failure row is written on the way out and the exception is
    re-raised for the caller's status code.
    """
    job = get_job(name)
    started = datetime.now(UTC)
    clock = time.perf_counter()

    run = JobRun(
        job_name=name,
        started_at=started,
        items_processed=0,
        result={},
        request_id=request_id,
    )
    session.add(run)
    session.flush()

    try:
        result = job.run(session, settings)
    except Exception as error:
        run.finished_at = datetime.now(UTC)
        run.duration_ms = int((time.perf_counter() - clock) * 1000)
        run.succeeded = False
        # The type and message, never a traceback: this row is read by an
        # operator in a list view, and the traceback is already in the log.
        run.error = f"{type(error).__name__}: {error}"
        session.commit()
        logger.exception("job.failed", extra={"job_name": name})
        raise

    run.finished_at = datetime.now(UTC)
    run.duration_ms = int((time.perf_counter() - clock) * 1000)
    run.succeeded = True
    run.result = result
    run.items_processed = int(result.get("items_processed", 0) or _total(result))
    session.commit()
    logger.info(
        "job.finished",
        extra={"job_name": name, "duration_ms": run.duration_ms, **_loggable(result)},
    )
    return run


def _total(result: dict[str, Any]) -> int:
    """Sum the integer counters a job returned.

    A job that reports `{"checked": 40, "raised": 3}` has processed 43 things
    in no useful sense, so this is only a fallback for jobs that do not name
    their own `items_processed`.
    """
    return sum(value for value in result.values() if isinstance(value, int))


def _loggable(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in result.items() if isinstance(value, int | float | str | bool)
    }


def recent_runs(session: Session, *, name: str | None = None, limit: int = 50) -> list[JobRun]:
    query = select(JobRun).order_by(JobRun.started_at.desc()).limit(limit)
    if name is not None:
        query = query.where(JobRun.job_name == name)
    return list(session.scalars(query))


def last_run(session: Session, *, name: str) -> JobRun | None:
    return session.scalar(
        select(JobRun).where(JobRun.job_name == name).order_by(JobRun.started_at.desc())
    )


# ---------------------------------------------------------------------------
# The jobs
# ---------------------------------------------------------------------------


@register(
    "drift-sweep",
    description=(
        "Shadow-recompute every active plan whose race is inside the forecast "
        "horizon, and raise a pending drift event where something material moved."
    ),
    suggested_cron="0 */6 * * *",
)
def _drift_sweep(session: Session, settings: Settings) -> dict[str, Any]:
    from raceos.services import drift_service

    return drift_service.sweep_forecasts(session, settings=settings)


@register(
    "purge-forecast-cache",
    description="Drop expired forecast cache rows.",
    suggested_cron="30 * * * *",
)
def _purge_forecast_cache(session: Session, settings: Settings) -> dict[str, Any]:
    from raceos.services import weather_service

    return {"items_processed": weather_service.purge_expired_cache(session)}


@register(
    "purge-expired-tokens",
    description=(
        "Delete spent and expired verification, reset and refresh tokens. "
        "An expired credential left in the table is a credential."
    ),
    suggested_cron="15 3 * * *",
)
def _purge_expired_tokens(session: Session, settings: Settings) -> dict[str, Any]:
    from raceos.services import auth_service

    return auth_service.purge_expired(session, settings=settings)


@register(
    "purge-idempotency-keys",
    description="Drop idempotency records past their TTL.",
    suggested_cron="45 3 * * *",
)
def _purge_idempotency_keys(session: Session, settings: Settings) -> dict[str, Any]:
    from raceos.services import rate_limit

    return {"items_processed": rate_limit.purge_expired_idempotency_keys(session)}


@register(
    "purge-rate-limit-counters",
    description="Drop rate-limit windows that have rolled past.",
    suggested_cron="*/30 * * * *",
)
def _purge_rate_limit_counters(session: Session, settings: Settings) -> dict[str, Any]:
    from raceos.services import rate_limit

    return {"items_processed": rate_limit.purge_expired_rate_limits(session)}


@register(
    "kpi-snapshot",
    description=(
        "Aggregate yesterday's KPIs from real rows: solver percentiles from "
        "solve_timings, account counts from accounts. Idempotent per date."
    ),
    suggested_cron="20 2 * * *",
)
def _kpi_snapshot(session: Session, settings: Settings) -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta

    from raceos.services import admin_service

    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    row = admin_service.snapshot_kpis(session, on_date=yesterday)
    return {
        "date": row.date.isoformat(),
        "plans_solved": row.plans_solved,
        "items_processed": row.plans_solved,
    }


@register(
    "service-health",
    description="Probe each dependency and write service_health. Never hand-set.",
    suggested_cron="*/15 * * * *",
)
def _service_health(session: Session, settings: Settings) -> dict[str, Any]:
    from raceos.services import admin_service

    rows = admin_service.refresh_service_health(session, settings=settings)
    return {
        "items_processed": len(rows),
        "degraded": sum(1 for row in rows if row.status.value != "nominal"),
    }


@register(
    "expire-support-grants",
    description=(
        "Close support grants past their hour. Belt and braces: expiry is "
        "already enforced on every read."
    ),
    suggested_cron="*/10 * * * *",
)
def _expire_support_grants(session: Session, settings: Settings) -> dict[str, Any]:
    from raceos.services import admin_service

    return admin_service.expire_support_grants(session)


@register(
    "expire-share-links",
    description="Retire lapsed share links and correct the plans' shared flags.",
    suggested_cron="5 * * * *",
)
def _expire_share_links(session: Session, settings: Settings) -> dict[str, Any]:
    from raceos.services import share_service

    return share_service.purge_expired(session)


@register(
    "media-asset-audit",
    description=(
        "Check every referenced course image still resolves in storage. A "
        "course card with a broken hero is the first thing a new user sees."
    ),
    suggested_cron="0 4 * * 1",
)
def _media_asset_audit(session: Session, settings: Settings) -> dict[str, Any]:
    from sqlalchemy import select as sa_select

    from raceos.db.models import Course
    from raceos.storage.base import get_storage_backend

    storage = get_storage_backend(settings)
    missing: list[dict[str, str]] = []
    checked = 0
    for course in session.scalars(sa_select(Course)):
        for field in ("media_hero_path", "media_card_path"):
            key = getattr(course, field)
            if not key:
                continue
            checked += 1
            if not storage.exists(key, public=True):
                missing.append({"course": course.slug, "field": field, "key": key})
    return {"items_processed": checked, "missing": missing, "missing_count": len(missing)}


@register(
    "purge-cache-entries",
    description="Drop cache rows past their TTL.",
    suggested_cron="50 * * * *",
)
def _purge_cache_entries(session: Session, settings: Settings) -> dict[str, Any]:
    from datetime import UTC, datetime

    from sqlalchemy import delete

    from raceos.db.models import CacheEntry

    removed = session.execute(
        delete(CacheEntry).where(CacheEntry.expires_at <= datetime.now(UTC))
    ).rowcount
    return {"items_processed": int(removed or 0)}


@register(
    "race-status-rollover",
    description=(
        "Mark yesterday's races completed so the dashboard stops calling them "
        "upcoming and post-race analysis can be offered."
    ),
    suggested_cron="0 5 * * *",
)
def _race_status_rollover(session: Session, settings: Settings) -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select as sa_select

    from raceos.db.models import Race
    from raceos.domain.enums import RaceStatus

    # A day's grace: a full-distance athlete can still be on course at
    # midnight local time, and several timezones are behind UTC.
    cutoff = (datetime.now(UTC) - timedelta(days=1)).date()
    rolled = 0
    for race in session.scalars(
        sa_select(Race).where(Race.status == RaceStatus.UPCOMING, Race.event_date < cutoff)
    ):
        race.status = RaceStatus.COMPLETED
        rolled += 1
    return {"items_processed": rolled}


@register(
    "notification-digest",
    description=(
        "Weekly digest for athletes who asked for one. In-app only while "
        "email delivery is a no-op."
    ),
    suggested_cron="0 8 * * 1",
)
def _notification_digest(session: Session, settings: Settings) -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select as sa_select

    from raceos.db.models import NotificationPreference, Race, User
    from raceos.domain.enums import (
        NotificationSeverity,
        NotificationType,
        RaceStatus,
    )
    from raceos.services import notification_service

    today = datetime.now(UTC).date()
    horizon = today + timedelta(days=14)
    sent = 0

    for preference in session.scalars(
        sa_select(NotificationPreference).where(
            NotificationPreference.type_key == NotificationType.DIGEST,
            NotificationPreference.channel_inapp.is_(True),
        )
    ):
        user = session.get(User, preference.user_id)
        if user is None:  # pragma: no cover - FK CASCADE
            continue
        upcoming = list(
            session.scalars(
                sa_select(Race).where(
                    Race.user_id == user.id,
                    Race.status == RaceStatus.UPCOMING,
                    Race.event_date >= today,
                    Race.event_date <= horizon,
                )
            )
        )
        if not upcoming:
            # No digest about nothing. An empty weekly email is how people
            # learn to filter the ones that matter.
            continue
        nearest = min(upcoming, key=lambda race: race.event_date)
        days = (nearest.event_date - today).days
        result = notification_service.notify(
            session,
            user=user,
            settings=settings,
            type_key=NotificationType.DIGEST,
            severity=NotificationSeverity.INFO,
            title=(
                f"{len(upcoming)} race{'s' if len(upcoming) != 1 else ''} in the "
                f"next two weeks."
            ),
            body=f"The nearest is in {days} day{'s' if days != 1 else ''}.",
            tag="DIGEST",
            race_id=nearest.id,
            cta_label="Open your dashboard",
            cta_href="/dashboard",
        )
        if result.delivered_inapp:
            sent += 1
    return {"items_processed": sent}
