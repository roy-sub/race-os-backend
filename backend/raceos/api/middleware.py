"""Request middleware: request ids, structured access logging, rate limiting.

Ordering matters and is set in :func:`raceos.api.main.create_app`. The request
id must be assigned before anything else so that every log line and every
error response carries it, including one produced by the rate limiter.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from raceos.api.errors import WarningCollector
from raceos.logging import actor_id_var, get_logger, request_id_var

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

#: Paths excluded from access logging. Health checks are polled every few
#: seconds by Render and would otherwise dominate the log volume without
#: telling anyone anything.
_QUIET_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})

Handler = Callable[[Request], Awaitable[Response]]


def new_request_id() -> str:
    """A sortable-ish, greppable request id."""
    return f"req_{uuid.uuid4().hex[:24]}"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, exposes it, and clears context afterwards.

    An inbound ``X-Request-ID`` is honoured so a trace can span the frontend
    and the API, but it is length-capped and stripped of anything that is not
    URL-safe: it ends up in log lines, and an unbounded caller-controlled
    value in a log line is a log-injection vector.
    """

    MAX_INBOUND_ID_LENGTH = 64

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        inbound = request.headers.get(REQUEST_ID_HEADER, "")
        cleaned = "".join(c for c in inbound if c.isalnum() or c in "-_")[
            : self.MAX_INBOUND_ID_LENGTH
        ]
        request_id = cleaned or new_request_id()

        token = request_id_var.set(request_id)
        actor_token = actor_id_var.set(None)
        request.state.request_id = request_id
        request.state.warnings = WarningCollector()

        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
            actor_id_var.reset(actor_token)

        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """One structured line per request, with duration and outcome."""

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "request failed",
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "duration_ms": round(duration_ms, 2),
                    "outcome": "exception",
                },
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        if request.url.path not in _QUIET_PATHS:
            logger.info(
                "request",
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_status": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                    "outcome": "ok" if response.status_code < 400 else "error",
                },
            )
        response.headers["Server-Timing"] = f"app;dur={duration_ms:.1f}"
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline security headers (Build Spec Part 16.1).

    The API returns JSON, never HTML, so the CSP is the most restrictive one
    that can be written: nothing may be loaded or framed at all. It exists so
    that a response reflected into a browser context cannot execute anything.
    """

    def __init__(self, app: object, *, hsts: bool) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._hsts = hsts

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )
        if self._hsts:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
