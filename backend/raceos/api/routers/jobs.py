"""Scheduled jobs, called by an external cron.

V1 ships no scheduler, no Redis and no Celery: each job is a service function
behind one shared-secret guard. That secret is the only thing between the
public internet and every background job, so it is compared in constant time
and a missing configuration refuses rather than defaults open.

Every run is recorded — including failures. A job that failed silently is
indistinguishable from one the cron never called.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from raceos.api.deps import Config, DbSession, require_internal_secret
from raceos.db.models import JobRun
from raceos.services import job_service

router = APIRouter(
    prefix="/internal/jobs",
    tags=["internal"],
    dependencies=[Depends(require_internal_secret)],
    # Not in the public schema: these are operational endpoints, and listing
    # them in the docs invites someone to try the door.
    include_in_schema=False,
)


def _run_out(run: JobRun) -> dict[str, object]:
    return {
        "id": str(run.id),
        "job_name": run.job_name,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_ms": run.duration_ms,
        "succeeded": run.succeeded,
        "items_processed": run.items_processed,
        "result": run.result,
        "error": run.error,
    }


@router.get("", summary="Every job, its cadence, and when it last ran")
def list_jobs(session: DbSession) -> dict[str, object]:
    """The operator's index. Also the cron configuration, in one place."""
    jobs = []
    for name, job in sorted(job_service.registry().items()):
        last = job_service.last_run(session, name=name)
        jobs.append(
            {
                "name": name,
                "description": job.description,
                "suggested_cron": job.suggested_cron,
                "last_run": _run_out(last) if last else None,
            }
        )
    return {"jobs": jobs}


@router.get("/runs", summary="Recent runs across all jobs")
def list_runs(
    session: DbSession,
    name: Annotated[str | None, "Filter to one job"] = None,
    limit: int = 50,
) -> dict[str, object]:
    runs = job_service.recent_runs(session, name=name, limit=min(limit, 200))
    return {"runs": [_run_out(run) for run in runs]}


@router.post("/{name}", summary="Run one job now")
def run_job(name: str, request: Request, session: DbSession, settings: Config) -> dict[str, object]:
    run = job_service.run_job(
        session,
        name=name,
        settings=settings,
        request_id=getattr(request.state, "request_id", None),
    )
    return _run_out(run)
