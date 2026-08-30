"""The error taxonomy, and its mapping onto HTTP.

Build Spec Part 13. Every failure the API can express is one of the codes in
:class:`ErrorCode`; the HTTP status is derived from the code rather than chosen
at each call site, so the same condition cannot return 409 in one router and
422 in another.

Two shapes, and the distinction is load-bearing:

* An **error** replaces the response. ``{"error": {...}}`` with a 4xx/5xx.
* A **warning** rides *alongside* a successful response, in a top-level
  ``warnings`` array. ``STALE_DATA`` and ``PARTIAL_DATA`` are warnings: a plan
  built on a six-month-old FTP is still a plan, and refusing to return it
  would be worse than returning it with the caveat attached.

``INFEASIBLE`` deserves its own note. It is a **successful solve with an
infeasible verdict**, not a server error, and it carries the diagnostic detail
the athlete needs to act (§F.5): the earliest missed barrier, how far past it
they are, and the one or two levers that would change the outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Machine-readable classification. The frontend maps these to its copy."""

    # --- validation and verdicts -------------------------------------
    INVALID_INPUT = "INVALID_INPUT"
    INFEASIBLE = "INFEASIBLE"
    OVER_CEILING = "OVER_CEILING"
    UPLOAD_FAILED = "UPLOAD_FAILED"

    # --- warnings (ride alongside a 200) -----------------------------
    STALE_DATA = "STALE_DATA"
    PARTIAL_DATA = "PARTIAL_DATA"

    # --- policy and authorization ------------------------------------
    FREEZE_WINDOW = "FREEZE_WINDOW"
    FORBIDDEN_STRUCTURAL = "FORBIDDEN_STRUCTURAL"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    #: Distinct from FORBIDDEN: the caller is who they say they are and is
    #: allowed to hold this resource — they have not paid for this action.
    #: The UI shows an upgrade path for one and an apology for the other,
    #: so collapsing them would produce the wrong screen.
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"

    # --- availability -------------------------------------------------
    SOLVER_TIMEOUT = "SOLVER_TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


#: Code to HTTP status. Single source of truth: a code cannot mean two
#: statuses depending on who raised it.
HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_INPUT: 422,
    ErrorCode.INFEASIBLE: 422,
    ErrorCode.UPLOAD_FAILED: 422,
    ErrorCode.OVER_CEILING: 409,
    ErrorCode.CONFLICT: 409,
    ErrorCode.FREEZE_WINDOW: 409,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.FORBIDDEN_STRUCTURAL: 403,
    ErrorCode.PAYMENT_REQUIRED: 402,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.SOLVER_TIMEOUT: 503,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    # Warnings never replace a response; if one is ever raised as an error
    # that is a programming mistake, and 500 makes it visible rather than
    # silently returning a plausible 200.
    ErrorCode.STALE_DATA: 500,
    ErrorCode.PARTIAL_DATA: 500,
}

#: Codes that are warnings, not errors. Asserted by the taxonomy test.
WARNING_CODES: frozenset[ErrorCode] = frozenset({ErrorCode.STALE_DATA, ErrorCode.PARTIAL_DATA})


@dataclass(frozen=True)
class ResponseWarning:
    """A non-blocking caveat travelling alongside a successful response."""

    code: ErrorCode
    message: str
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.field is not None:
            out["field"] = self.field
        return out


class RaceOSError(Exception):
    """Base for every failure the API expresses deliberately.

    Anything else reaching the handler is an unexpected exception and becomes
    ``INTERNAL_ERROR`` with a request id, having been logged with its
    traceback.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.details = details or {}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]

    def to_response(self, request_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "request_id": request_id,
        }
        if self.field is not None:
            body["field"] = self.field
        if self.details:
            body["details"] = self.details
        return {"error": body}


class InvalidInput(RaceOSError):
    code = ErrorCode.INVALID_INPUT


class Unauthenticated(RaceOSError):
    code = ErrorCode.UNAUTHENTICATED


class Forbidden(RaceOSError):
    code = ErrorCode.FORBIDDEN


class ForbiddenStructural(Forbidden):
    """An action that must be impossible, not merely unauthorized.

    Raised where the *architecture* forbids something regardless of caller —
    a coach writing an athlete's constraints being the canonical case. It is a
    distinct code so that a test can assert the structural guarantee produced
    it, rather than an ordinary permission check that someone could later
    loosen.
    """

    code = ErrorCode.FORBIDDEN_STRUCTURAL


class PaymentRequired(RaceOSError):
    """An entitlement the caller does not hold.

    Carries ``required_tiers`` and ``purchasable_per_race`` in ``details``
    so the client can offer the *right* upgrade — buying one race is
    cheaper than a season, and a generic paywall hides that.
    """

    code = ErrorCode.PAYMENT_REQUIRED


class NotFound(RaceOSError):
    code = ErrorCode.NOT_FOUND


class Conflict(RaceOSError):
    code = ErrorCode.CONFLICT


class FreezeWindow(Conflict):
    code = ErrorCode.FREEZE_WINDOW


class OverCeiling(Conflict):
    code = ErrorCode.OVER_CEILING


class UploadFailed(RaceOSError):
    code = ErrorCode.UPLOAD_FAILED


class RateLimited(RaceOSError):
    code = ErrorCode.RATE_LIMITED

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message, details={"retry_after_seconds": retry_after_seconds})
        self.retry_after_seconds = retry_after_seconds


class SolverTimeout(RaceOSError):
    code = ErrorCode.SOLVER_TIMEOUT


class ServiceUnavailable(RaceOSError):
    code = ErrorCode.SERVICE_UNAVAILABLE


@dataclass(frozen=True)
class InfeasibleDetails:
    """The payload of an ``INFEASIBLE`` response (SOLVER_MODEL.md §F.5).

    ``barrier`` is the **earliest missed** barrier, not the tightest. The
    distinction is the whole point of §F.5: told they miss the finish by 132
    minutes, an athlete concludes the race is out of reach; told they miss the
    bike cut-off by 10, they learn the truth. The tightest pair is carried
    too, for the admin blast-radius view, and the user-facing message must be
    built from ``barrier``/``miss_minutes`` — never from the tightest pair.
    """

    barrier: str
    miss_minutes: float
    levers: tuple[str, ...]
    tightest_barrier: str
    tightest_miss_minutes: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "barrier": self.barrier,
            "miss_minutes": self.miss_minutes,
            "levers": list(self.levers),
            "tightest_barrier": self.tightest_barrier,
            "tightest_miss_minutes": self.tightest_miss_minutes,
        }


class Infeasible(RaceOSError):
    """A successful solve whose verdict is that the goal cannot be met."""

    code = ErrorCode.INFEASIBLE

    def __init__(self, message: str, details: InfeasibleDetails) -> None:
        super().__init__(message, field="goal_minutes", details=details.to_dict())


# --- solver-domain errors --------------------------------------------------
# Defined in `raceos.solver.errors` and re-exported here, so the dependency
# runs one way: the API imports the solver, never the reverse. They are
# translated onto the taxonomy at the boundary and carry no HTTP knowledge of
# their own.


__all_solver_errors__ = (
    "BundleIncomplete",
    "ImplausibleConstraint",
    "MissingConstraint",
    "SolverInputError",
)


@dataclass
class WarningCollector:
    """Accumulates warnings during request handling.

    Held on the request state so a service can attach ``STALE_DATA`` without
    knowing how the response is serialised.
    """

    items: list[ResponseWarning] = field(default_factory=list)

    def add(self, code: ErrorCode, message: str, field_name: str | None = None) -> None:
        if code not in WARNING_CODES:
            raise ValueError(f"{code.value} is an error code, not a warning; raise it instead")
        self.items.append(ResponseWarning(code=code, message=message, field=field_name))

    def to_list(self) -> list[dict[str, Any]]:
        return [w.to_dict() for w in self.items]

    def __bool__(self) -> bool:
        return bool(self.items)
