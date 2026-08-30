"""Liveness and readiness.

``/healthz`` answers "is this process alive" and touches nothing external. It
is what Render's health check polls; if it depended on the database, a brief
database blip would make Render kill and restart healthy instances, turning a
recoverable outage into a worse one.

``/readyz`` answers "can this process actually serve traffic", and therefore
does check its dependencies: the database (including PostGIS, without which
the schema cannot work) and object storage. It is for humans and for
deployment gating, not for the liveness probe.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request, Response, status

from raceos.config import Settings, get_settings
from raceos.db.session import check_database, describe_connection, normalise_database_url
from raceos.logging import get_logger
from raceos.storage.base import get_storage_backend

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    """Alive. Deliberately checks nothing external."""
    return {"status": "ok"}


def _check(name: str, fn: Any) -> dict[str, Any]:
    """Run one readiness check, timing it and never letting it raise."""
    started = time.perf_counter()
    try:
        detail = fn()
        return {
            "name": name,
            "ok": True,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            **({"detail": detail} if detail else {}),
        }
    except Exception as exc:  # readiness reports failures, it never raises
        logger.warning(
            "readiness check failed", extra={"check": name, "error_type": type(exc).__name__}
        )
        return {
            "name": name,
            "ok": False,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            # The exception text can carry a connection string; the logging
            # redaction covers the log line, and this covers the response.
            "error": type(exc).__name__,
        }


@router.get("/readyz", summary="Readiness probe")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    """Ready to serve: database with PostGIS, and storage reachable.

    Returns 503 with the failing check named when it is not. PostGIS being
    absent counts as not ready even though the connection succeeded, because
    every geometry column in the schema depends on it and the failure it
    otherwise produces appears much later and much less clearly.
    """
    settings: Settings = get_settings()

    def database() -> dict[str, Any]:
        info = check_database()
        if not info["postgis_enabled"]:
            raise RuntimeError("PostGIS extension is not enabled on this database")
        return info

    def storage() -> dict[str, Any]:
        return get_storage_backend(settings).health()

    checks = [_check("database", database), _check("storage", storage)]
    ready = all(c["ok"] for c in checks)

    description = describe_connection(
        normalise_database_url(settings.database_url.get_secret_value())
    )

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if ready else "not_ready",
        "request_id": getattr(request.state, "request_id", None),
        "environment": settings.app_env.value,
        "connection": description.as_log_fields(),
        "checks": checks,
    }
