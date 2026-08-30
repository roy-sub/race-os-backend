"""Signup, login, sessions, reset, verification. Build Spec Part 8.

Four behaviours here are security requirements rather than conveniences, and
each is written to be hard to undo:

**Forgot-password must not leak account existence.** The same 202 and the same
confirmation come back whether or not the address is registered. Anything else
turns the endpoint into an account enumerator.

**Login lockout counts per account, not per IP.** Per-IP alone lets an attacker
lock a stranger out from one request; per-account alone lets a botnet spread
attempts. So: a per-account counter *and* per-IP rate limiting as defence in
depth. The failure copy states attempts remaining and must stay accurate,
because the frontend renders that number.

**A password reset invalidates every other session**, by bumping one column
that every token verification already checks.

**Refresh tokens rotate, and reuse of a rotated token kills the family.** A
stolen refresh token is only useful until the legitimate holder next refreshes;
after that, the theft is detectable, and detection revokes everything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.errors import Forbidden, InvalidInput, NotFound, Unauthenticated
from raceos.config import Settings
from raceos.db.models import (
    EmailVerificationToken,
    NotificationPreference,
    PasswordResetToken,
    User,
)
from raceos.db.models import (
    Session as SessionRow,
)
from raceos.domain.enums import (
    CRITICAL_NOTIFICATION_TYPES,
    AccountState,
    AthleteLevel,
    DriftSensitivity,
    NotificationType,
    UnitSystem,
)
from raceos.email import templates
from raceos.email.sender import deliver
from raceos.logging import get_logger
from raceos.services import security

logger = get_logger(__name__)

#: Build Spec Part 4.7's default matrix. `digest` is off on every channel;
#: critical types cannot be fully disabled, which is enforced on write.
DEFAULT_PREFERENCES: dict[NotificationType, tuple[bool, bool, bool]] = {
    NotificationType.DRIFT: (True, True, True),
    NotificationType.WEEK: (True, True, True),
    NotificationType.CUTOFF: (True, True, True),
    NotificationType.BUNDLE: (True, False, True),
    NotificationType.ANALYSIS: (True, False, True),
    NotificationType.DIGEST: (False, False, False),
}

MIN_PASSWORD_LENGTH = 10


@dataclass(frozen=True)
class AuthResult:
    user: User
    access_token: str
    refresh_token: str
    session_id: UUID


#: How long a lapsed session row is kept after expiry. Refresh-token
#: reuse detection works by finding an already-rotated row; delete it
#: too eagerly and a stolen token looks like an unknown one.
SESSION_REUSE_GRACE_DAYS = 30


def _now() -> datetime:
    return datetime.now(UTC)


def _validate_password(password: str) -> None:
    """Length only, deliberately.

    Composition rules (a digit, a symbol, mixed case) push people toward
    `Password1!` and are worse than length. NIST dropped them for that reason.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidInput(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
            field="password",
        )


def seed_notification_preferences(session: Session, user: User) -> None:
    for type_key, (email, push, inapp) in DEFAULT_PREFERENCES.items():
        session.add(
            NotificationPreference(
                user_id=user.id,
                type_key=type_key,
                channel_email=email,
                channel_push=push,
                channel_inapp=inapp,
                drift_sensitivity=DriftSensitivity.BALANCED,
            )
        )


def signup(
    session: Session,
    *,
    email: str,
    password: str,
    settings: Settings,
    name: str | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> AuthResult:
    """Create an account and sign it in.

    With ``REQUIRE_EMAIL_VERIFICATION=false`` the account is created **already
    verified**, because there is no way to deliver a verification message in
    V1 and gating on one would lock every user out of their own product. The
    token is still issued and the message still rendered, so the flow is
    exercised and flipping the flag needs no code change.
    """
    _validate_password(password)
    normalised = email.strip()
    if not normalised or "@" not in normalised:
        raise InvalidInput("Enter a valid email address.", field="email")

    existing = session.scalar(select(User).where(User.email == normalised))
    if existing is not None:
        # Signup *can* say the address is taken: the person is holding the
        # address in their hand, so this leaks nothing they cannot already
        # learn. Forgot-password is the endpoint that must stay silent.
        raise InvalidInput("That email address is already registered.", field="email")

    user = User(
        email=normalised,
        password_hash=security.hash_password(password, settings),
        name=name,
        units=UnitSystem.METRIC,
        level=AthleteLevel.FIRST,
        email_verified_at=None if settings.require_email_verification else _now(),
    )
    session.add(user)
    session.flush()
    seed_notification_preferences(session, user)

    token = security.issue_token()
    session.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=token.hashed,
            expires_at=_now() + timedelta(hours=settings.email_verification_ttl_hours),
            delivery_link=f"{settings.app_base_url.rstrip('/')}/verify-email/{token.raw}",
        )
    )
    deliver(
        session,
        templates.verification_email(
            to_address=user.email,
            name=user.name,
            token=token.raw,
            settings=settings,
            user_id=user.id,
        ),
        settings,
    )

    return _start_session(session, user, settings, user_agent=user_agent, ip=ip)


def login(
    session: Session,
    *,
    email: str,
    password: str,
    settings: Settings,
    user_agent: str | None = None,
    ip: str | None = None,
) -> AuthResult:
    """Authenticate, counting failures per account."""
    user = session.scalar(select(User).where(User.email == email.strip()))

    if user is None:
        # Still do the work, so a missing account and a wrong password take
        # comparable time.
        security.verify_password(password, None, settings)
        raise Unauthenticated("Email or password is incorrect.")

    if user.locked_until and user.locked_until > _now():
        remaining = int((user.locked_until - _now()).total_seconds() // 60) + 1
        raise Forbidden(
            f"Too many failed attempts. Try again in {remaining} minutes.",
            details={"locked_until": user.locked_until.isoformat()},
        )

    if user.account_state is AccountState.ERASED:
        raise Unauthenticated("Email or password is incorrect.")

    if not security.verify_password(password, user.password_hash, settings):
        user.failed_login_count += 1
        attempts_left = settings.login_max_attempts - user.failed_login_count
        if attempts_left <= 0:
            user.locked_until = _now() + timedelta(minutes=settings.login_lock_minutes)
            user.failed_login_count = 0
            session.flush()
            raise Forbidden(
                f"Too many failed attempts. Try again in " f"{settings.login_lock_minutes} minutes."
            )
        session.flush()
        # The copy states attempts remaining and must be accurate: the
        # frontend renders this number.
        raise Unauthenticated(
            f"Email or password is incorrect. "
            f"{attempts_left} attempt{'s' if attempts_left != 1 else ''} remaining.",
            details={"attempts_remaining": attempts_left},
        )

    user.failed_login_count = 0
    user.locked_until = None
    user.last_seen_at = _now()

    if user.password_hash and security.needs_rehash(user.password_hash, settings):
        # Opportunistic upgrade: we have the plaintext exactly here and
        # nowhere else, so this is the only moment a rehash is possible.
        user.password_hash = security.hash_password(password, settings)

    session.flush()
    return _start_session(session, user, settings, user_agent=user_agent, ip=ip)


def _start_session(
    session: Session,
    user: User,
    settings: Settings,
    *,
    user_agent: str | None,
    ip: str | None,
) -> AuthResult:
    issued = _now()
    refresh = security.issue_token()
    row = SessionRow(
        user_id=user.id,
        refresh_token_hash=refresh.hashed,
        issued_at=issued,
        expires_at=issued + timedelta(seconds=settings.jwt_refresh_ttl_seconds),
        user_agent=user_agent,
        ip_hash=security.hash_ip(ip, settings),
    )
    session.add(row)
    session.flush()

    access = security.create_token(
        user_id=user.id,
        kind="access",
        settings=settings,
        issued_at=issued,
        session_id=row.id,
    )
    return AuthResult(user=user, access_token=access, refresh_token=refresh.raw, session_id=row.id)


def refresh_session(
    session: Session,
    *,
    refresh_token: str,
    settings: Settings,
    user_agent: str | None = None,
    ip: str | None = None,
) -> AuthResult:
    """Rotate the refresh token, and treat reuse as theft.

    A rotated token is kept, not deleted, precisely so its reuse is
    detectable. When one is presented after rotation the only safe reading is
    that it was captured, so **every session for that user is revoked** — the
    legitimate holder is signed out too, which is the correct trade against an
    attacker holding a valid refresh token.
    """
    hashed = security.hash_token(refresh_token)
    row = session.scalar(select(SessionRow).where(SessionRow.refresh_token_hash == hashed))
    if row is None:
        raise Unauthenticated("Session is no longer valid. Please sign in again.")

    if row.rotated_to_session_id is not None:
        logger.warning(
            "refresh token reuse detected; revoking the session family",
            extra={"user_id_": str(row.user_id), "session_id": str(row.id)},
        )
        _revoke_all_sessions(session, row.user_id)
        raise Unauthenticated("Session is no longer valid. Please sign in again.")

    if row.revoked_at is not None or row.expires_at <= _now():
        raise Unauthenticated("Session has expired. Please sign in again.")

    user = session.get(User, row.user_id)
    if user is None or user.account_state is AccountState.ERASED:
        raise Unauthenticated("Session is no longer valid. Please sign in again.")

    if (
        user.sessions_invalidated_before is not None
        and row.issued_at <= user.sessions_invalidated_before
    ):
        raise Unauthenticated("Session was ended. Please sign in again.")

    result = _start_session(session, user, settings, user_agent=user_agent, ip=ip)
    row.revoked_at = _now()
    row.rotated_to_session_id = result.session_id
    session.flush()
    return result


def logout(session: Session, *, refresh_token: str | None, user_id: UUID) -> None:
    """Revoke the presented session, or all of them when none is presented."""
    if refresh_token:
        hashed = security.hash_token(refresh_token)
        row = session.scalar(
            select(SessionRow).where(
                SessionRow.refresh_token_hash == hashed, SessionRow.user_id == user_id
            )
        )
        if row is not None and row.revoked_at is None:
            row.revoked_at = _now()
            session.flush()
            return
    _revoke_all_sessions(session, user_id)


def _revoke_all_sessions(session: Session, user_id: UUID) -> None:
    for row in session.scalars(
        select(SessionRow).where(SessionRow.user_id == user_id, SessionRow.revoked_at.is_(None))
    ):
        row.revoked_at = _now()
    session.flush()


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


def request_password_reset(session: Session, *, email: str, settings: Settings) -> None:
    """Always succeeds from the caller's point of view.

    **Hard requirement (Part 8.5): this must not leak account existence.** The
    router returns 202 unconditionally; this function simply does nothing when
    the address is unknown.
    """
    user = session.scalar(select(User).where(User.email == email.strip()))
    if user is None or user.account_state is AccountState.ERASED:
        logger.info("password reset requested for an unknown address")
        return

    token = security.issue_token()
    link = f"{settings.app_base_url.rstrip('/')}/reset-password?token={token.raw}"
    session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=token.hashed,
            expires_at=_now() + timedelta(minutes=settings.password_reset_ttl_minutes),
            # Retained ONLY so support can hand it over while email delivery is
            # a no-op. Exposed by an admin-only endpoint, never a public one.
            delivery_link=link,
        )
    )
    deliver(
        session,
        templates.password_reset_email(
            to_address=user.email,
            name=user.name,
            token=token.raw,
            settings=settings,
            user_id=user.id,
        ),
        settings,
    )


def reset_password(session: Session, *, token: str, new_password: str, settings: Settings) -> User:
    """Single-use, time-bounded, and it signs every other session out."""
    _validate_password(new_password)

    record = session.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == security.hash_token(token)
        )
    )
    if record is None or record.used_at is not None or record.expires_at <= _now():
        raise InvalidInput("That reset link is invalid or has expired.", field="token")

    user = session.get(User, record.user_id)
    if user is None:  # pragma: no cover - FK guarantees it
        raise NotFound("Account not found.")

    user.password_hash = security.hash_password(new_password, settings)
    # One column bump ends every outstanding token, because every
    # verification already compares `iat` against it.
    user.sessions_invalidated_before = _now()
    user.failed_login_count = 0
    user.locked_until = None
    record.used_at = _now()
    _revoke_all_sessions(session, user.id)
    session.flush()
    return user


# ---------------------------------------------------------------------------
# Email verification (flag-gated, fully built)
# ---------------------------------------------------------------------------


def verify_email(session: Session, *, token: str) -> User:
    record = session.scalar(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == security.hash_token(token)
        )
    )
    if record is None or record.used_at is not None or record.expires_at <= _now():
        raise InvalidInput("That verification link is invalid or has expired.", field="token")

    user = session.get(User, record.user_id)
    if user is None:  # pragma: no cover
        raise NotFound("Account not found.")

    user.email_verified_at = user.email_verified_at or _now()
    record.used_at = _now()
    session.flush()
    return user


def resend_verification(session: Session, *, user: User, settings: Settings) -> None:
    if user.email_verified_at is not None:
        return
    token = security.issue_token()
    session.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=token.hashed,
            expires_at=_now() + timedelta(hours=settings.email_verification_ttl_hours),
            delivery_link=f"{settings.app_base_url.rstrip('/')}/verify-email/{token.raw}",
        )
    )
    deliver(
        session,
        templates.verification_email(
            to_address=user.email,
            name=user.name,
            token=token.raw,
            settings=settings,
            user_id=user.id,
        ),
        settings,
    )


def require_verified_for_solve(user: User, settings: Settings) -> None:
    """The verification gate (Part 8.3), applied before the first plan solve.

    Browsing the race directory and course recon stay open — that is the
    "skip, just browse courses" path. Only solving is gated, and only when the
    flag is on.
    """
    if not settings.require_email_verification:
        return
    if user.email_verified_at is None:
        raise Forbidden(
            "Confirm your email address before solving a plan.",
            details={"reason": "email_unverified"},
        )


def notification_preferences_are_valid(
    type_key: NotificationType, *, email: bool, push: bool, inapp: bool
) -> bool:
    """Critical types cannot be fully disabled: in-app is the floor.

    The user chooses the channel; they do not choose whether a cut-off warning
    exists. Enforced server-side because the frontend mock does not enforce it.
    """
    if type_key in CRITICAL_NOTIFICATION_TYPES:
        return inapp
    return True


def purge_expired(session: Session, *, settings: Settings) -> dict[str, int]:
    """Delete spent and expired credentials.

    An expired token row is still a credential: it names a user, and a leaked
    table of them is a leaked table of them whether or not the code path would
    honour one. So they are deleted rather than left to accumulate.

    Sessions are kept for a grace period past expiry rather than deleted the
    moment they lapse, because refresh-token **reuse detection** works by
    finding an already-rotated row — delete it and a stolen token becomes
    indistinguishable from an unknown one.
    """
    from datetime import timedelta

    from sqlalchemy import delete

    now = _now()
    reuse_grace = now - timedelta(days=SESSION_REUSE_GRACE_DAYS)

    verification = session.execute(
        delete(EmailVerificationToken).where(EmailVerificationToken.expires_at <= now)
    ).rowcount
    resets = session.execute(
        delete(PasswordResetToken).where(PasswordResetToken.expires_at <= now)
    ).rowcount
    sessions = session.execute(
        delete(SessionRow).where(SessionRow.expires_at <= reuse_grace)
    ).rowcount

    return {
        "verification_tokens": int(verification or 0),
        "password_reset_tokens": int(resets or 0),
        "sessions": int(sessions or 0),
        "items_processed": int(verification or 0) + int(resets or 0) + int(sessions or 0),
    }
