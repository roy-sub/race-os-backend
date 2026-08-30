"""FastAPI application factory.

Everything the app needs is assembled here and nowhere else: settings are read
once, logging is configured once, middleware is ordered deliberately, and the
error taxonomy is bound to the exception handlers so a raised
:class:`~raceos.api.errors.RaceOSError` becomes its documented status and body
without any router repeating itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from raceos.api.errors import HTTP_STATUS, ErrorCode, RaceOSError, RateLimited
from raceos.api.middleware import (
    REQUEST_ID_HEADER,
    AccessLogMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from raceos.api.routers import (
    admin,
    admin_bundles,
    auth,
    billing,
    coach,
    constraints,
    courses,
    dashboard,
    drift,
    exports,
    health,
    jobs,
    plans,
    postrace,
    racemode,
    share,
)
from raceos.config import Settings, get_settings
from raceos.logging import configure_logging, get_logger

logger = get_logger(__name__)

API_PREFIX = "/api/v1"


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "unknown")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown.

    The resolved configuration is logged at startup with every secret
    redacted, per Build Spec Part 3, so a misconfigured deployment is
    diagnosable from the first line of its logs rather than from a later
    failure whose cause is three layers away.
    """
    settings: Settings = app.state.settings
    logger.info(
        "starting",
        extra={"event": "startup", "config": settings.redacted_dump()},
    )
    yield
    logger.info("stopping", extra={"event": "shutdown"})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="RaceOS API",
        version="1.0.0",
        description=(
            "Deterministic race-plan solver and API. All numbers in a plan come "
            "from the solver; the language model never computes one."
        ),
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs" if not settings.is_production else None,
        redoc_url=None,
        servers=[{"url": settings.api_base_url}],
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Middleware runs in reverse registration order, so the last registered is
    # outermost. The request id must be assigned first, hence registered last.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origin_list),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER, "Retry-After", "ETag", "Server-Timing"],
        max_age=600,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestContextMiddleware)

    _register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(courses.router)
    app.include_router(constraints.router)
    app.include_router(plans.router)
    app.include_router(plans.jobs_router)
    app.include_router(exports.router)
    app.include_router(billing.router)
    app.include_router(billing.webhook_router)
    app.include_router(dashboard.router)
    app.include_router(drift.router)
    app.include_router(admin_bundles.router)
    app.include_router(postrace.router)
    app.include_router(coach.router)
    app.include_router(share.router)
    app.include_router(racemode.router)
    app.include_router(admin.router)
    app.include_router(admin.athlete_router)
    app.include_router(jobs.router)

    return app


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RaceOSError)
    async def _raceos_error(request: Request, exc: RaceOSError) -> JSONResponse:
        request_id = _request_id(request)
        # A deliberate error is expected traffic, not a fault: log it at
        # warning with its code so it is countable, without a traceback.
        logger.warning(
            "request rejected",
            extra={
                "error_code": exc.code.value,
                "http_path": request.url.path,
                "http_status": exc.http_status,
            },
        )
        headers: dict[str, str] = {}
        if isinstance(exc, RateLimited):
            headers["Retry-After"] = str(exc.retry_after_seconds)
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.to_response(request_id),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Pydantic validation mapped onto the taxonomy's INVALID_INPUT.

        The first offending field is named, because the frontend's error copy
        attaches a message to a specific input.
        """
        errors = exc.errors()
        first_field: str | None = None
        if errors:
            location = [str(p) for p in errors[0].get("loc", ()) if p not in ("body", "query")]
            first_field = ".".join(location) or None
        return JSONResponse(
            status_code=HTTP_STATUS[ErrorCode.INVALID_INPUT],
            content={
                "error": {
                    "code": ErrorCode.INVALID_INPUT.value,
                    "message": errors[0]["msg"] if errors else "Invalid request.",
                    **({"field": first_field} if first_field else {}),
                    "details": {"errors": [{k: str(v) for k, v in e.items()} for e in errors]},
                    "request_id": _request_id(request),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Framework 404s and 405s, in the taxonomy's envelope."""
        code = {
            401: ErrorCode.UNAUTHENTICATED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
            409: ErrorCode.CONFLICT,
            429: ErrorCode.RATE_LIMITED,
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code.value,
                    "message": str(exc.detail),
                    "request_id": _request_id(request),
                }
            },
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Anything unexpected.

        The request id is attached to both the log line and the response so a
        user can quote it and it resolves to the traceback. The exception text
        itself is never returned: it is the most common way an internal detail
        or a credential escapes into a client.
        """
        request_id = _request_id(request)
        logger.exception(
            "unhandled exception",
            extra={"http_path": request.url.path, "error_type": type(exc).__name__},
        )
        return JSONResponse(
            status_code=HTTP_STATUS[ErrorCode.INTERNAL_ERROR],
            content={
                "error": {
                    "code": ErrorCode.INTERNAL_ERROR.value,
                    "message": "Something went wrong on our end.",
                    "request_id": request_id,
                }
            },
        )


def get_app() -> Any:
    """Entry point for gunicorn/uvicorn: ``raceos.api.main:get_app()``."""
    return create_app()


app = create_app()
