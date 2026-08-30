"""Rate limiting and idempotency, database-backed. V1 has no Redis.

**Per-instance-safe because there is one instance** — but the note runs the
other way too, and that is worth stating: counters are rows in the shared
database updated by an atomic upsert, so they stay correct if a second web
instance is ever added. An in-process counter would not. The V1 caveat is
conservative rather than limiting.

Windows are fixed rather than sliding. A sliding window is more accurate at
the boundary and costs a row per request; a fixed window costs one row per
subject per window and lets a burst of at most 2x the limit straddle a
boundary. For login protection that is immaterial — the account lockout, not
the rate limiter, is what stops credential stuffing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from raceos.api.errors import Conflict, RateLimited
from raceos.config import Settings
from raceos.db.models import IdempotencyKey, RateLimitCounter


@dataclass(frozen=True)
class RateLimitVerdict:
    allowed: bool
    remaining: int
    retry_after_seconds: int


def _window_start(now: datetime) -> datetime:
    return now.replace(second=0, microsecond=0)


def check_rate_limit(
    session: Session,
    *,
    subject: str,
    bucket: str,
    limit: int,
    settings: Settings,
    now: datetime | None = None,
) -> RateLimitVerdict:
    """Count one request against ``(subject, bucket)`` for the current minute.

    The upsert is atomic, so two concurrent requests cannot both read the same
    count and both decide they are under the limit.
    """
    if not settings.rate_limit_enabled:
        return RateLimitVerdict(allowed=True, remaining=limit, retry_after_seconds=0)

    moment = now or datetime.now(UTC)
    window = _window_start(moment)

    statement = (
        insert(RateLimitCounter)
        .values(subject=subject, bucket=bucket, window_start=window, count=1)
        .on_conflict_do_update(
            index_elements=["subject", "bucket", "window_start"],
            set_={"count": RateLimitCounter.__table__.c.count + 1},
        )
        .returning(RateLimitCounter.__table__.c.count)
    )
    count = int(session.execute(statement).scalar_one())

    remaining = max(0, limit - count)
    retry_after = int((window + timedelta(minutes=1) - moment).total_seconds()) + 1
    return RateLimitVerdict(
        allowed=count <= limit, remaining=remaining, retry_after_seconds=retry_after
    )


def enforce_rate_limit(
    session: Session,
    *,
    subject: str,
    bucket: str,
    limit: int,
    settings: Settings,
) -> None:
    """Raise :class:`RateLimited` when the caller is over the limit."""
    verdict = check_rate_limit(
        session, subject=subject, bucket=bucket, limit=limit, settings=settings
    )
    if not verdict.allowed:
        raise RateLimited(
            "Too many requests. Please wait a moment and try again.",
            retry_after_seconds=verdict.retry_after_seconds,
        )


def purge_expired_rate_limits(session: Session, *, older_than_minutes: int = 60) -> int:
    """Housekeeping for the expiry sweep job."""
    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    result = session.execute(delete(RateLimitCounter).where(RateLimitCounter.window_start < cutoff))
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayedResponse:
    status_code: int
    body: dict[str, object]


def lookup_idempotent(
    session: Session, *, key: str, endpoint: str, request_hash: str
) -> ReplayedResponse | None:
    """Return the first recorded response for *key*, if there is one.

    A key reused against a **different** request body is a client bug, and
    returning the first response would silently discard the second request.
    So it is a conflict, not a replay.
    """
    record = session.scalar(select(IdempotencyKey).where(IdempotencyKey.key == key))
    if record is None:
        return None
    if record.expires_at <= datetime.now(UTC):
        return None
    if record.endpoint != endpoint or record.request_hash != request_hash:
        raise Conflict(
            "This Idempotency-Key was already used for a different request.",
            details={"key": key},
        )
    return ReplayedResponse(status_code=record.status_code, body=record.response_body)


def record_idempotent(
    session: Session,
    *,
    key: str,
    endpoint: str,
    request_hash: str,
    status_code: int,
    body: dict[str, object],
    user_id: object | None,
    settings: Settings,
) -> None:
    session.add(
        IdempotencyKey(
            key=key,
            user_id=user_id,
            endpoint=endpoint,
            request_hash=request_hash,
            response_body=body,
            status_code=status_code,
            expires_at=datetime.now(UTC) + timedelta(hours=settings.idempotency_key_ttl_hours),
        )
    )


def purge_expired_idempotency_keys(session: Session) -> int:
    result = session.execute(
        delete(IdempotencyKey).where(IdempotencyKey.expires_at < datetime.now(UTC))
    )
    return int(result.rowcount or 0)
