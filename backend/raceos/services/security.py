"""Password hashing, tokens, and the primitives every other service leans on.

Three things live here because getting any of them subtly wrong is a security
incident rather than a bug, and because they must be identical everywhere:

* **argon2id** for passwords, tuned by config rather than by literals.
* **RS256 JWTs** signed with our own keypair. Not Supabase Auth — Supabase
  provides a database and an object store, and nothing else.
* **Opaque tokens** for refresh, reset, verification, invites and share links.
  Every one is stored hashed and compared in constant time.

The rule that shapes all of it: **a token is a bearer credential, so what we
store must be useless to whoever steals the database.** Only hashes are
persisted, and the raw value is returned exactly once at creation.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from raceos.config import Settings

TokenKind = Literal["access", "refresh"]


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def _hasher(settings: Settings) -> PasswordHasher:
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost_kib,
        parallelism=settings.argon2_parallelism,
    )


def hash_password(password: str, settings: Settings) -> str:
    return _hasher(settings).hash(password)


def verify_password(password: str, stored_hash: str | None, settings: Settings) -> bool:
    """Constant-time-ish verification that does not leak *why* it failed.

    A missing hash still runs a real verification against a dummy value, so an
    account that exists without a password takes the same time as one that
    does not exist at all. Without that, login timing distinguishes them.
    """
    hasher = _hasher(settings)
    if not stored_hash:
        # Burn equivalent work so absence is not detectable by timing.
        with contextlib.suppress(VerifyMismatchError, InvalidHashError):
            hasher.verify(hasher.hash("timing-equalisation"), password)
        return False
    try:
        hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def needs_rehash(stored_hash: str, settings: Settings) -> bool:
    """True when the stored hash used weaker parameters than we now require."""
    try:
        return bool(_hasher(settings).check_needs_rehash(stored_hash))
    except InvalidHashError:  # pragma: no cover - a corrupt hash
        return True


# ---------------------------------------------------------------------------
# Opaque tokens
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuedToken:
    """A token the caller must hand over now: the raw value is not stored."""

    raw: str
    hashed: str
    prefix: str


def issue_token(byte_length: int = 32) -> IssuedToken:
    """A URL-safe random token, with its hash and a short non-secret prefix.

    The prefix lets a link be identified in a list — "the one ending 7f3c" —
    without storing anything that could reconstruct it.
    """
    raw = secrets.token_urlsafe(byte_length)
    return IssuedToken(raw=raw, hashed=hash_token(raw), prefix=raw[:12])


def hash_token(raw: str) -> str:
    """SHA-256. Deliberately *not* argon2.

    Tokens carry full entropy from a CSPRNG, so there is nothing to brute
    force and no need for a slow KDF — unlike a password, which is
    low-entropy and human-chosen. Using argon2 here would add tens of
    milliseconds to every authenticated request for no security gain.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def tokens_match(raw: str, stored_hash: str) -> bool:
    """Constant-time comparison, so a timing side channel cannot walk the hash."""
    return hmac.compare_digest(hash_token(raw), stored_hash)


def secrets_match(presented: str, expected: str) -> bool:
    """Constant-time comparison of two shared secrets.

    Used by the ``/internal/jobs/*`` guard. A plain ``==`` on a secret leaks
    its prefix through timing, which for a value an external cron sends on
    every call is a real, repeatable measurement rather than a theoretical one.
    """
    return hmac.compare_digest(presented, expected)


def hash_ip(ip: str | None, settings: Settings) -> str | None:
    """An IP address is PII, so only a keyed hash is ever stored.

    Keyed with the session secret rather than plain SHA-256: the IPv4 space is
    small enough to enumerate exhaustively, so an unkeyed hash of an address is
    not an anonymisation at all.
    """
    if not ip:
        return None
    key = settings.session_cookie_secret.get_secret_value().encode("utf-8")
    return hmac.new(key, ip.encode("utf-8"), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# JWTs
# ---------------------------------------------------------------------------


class TokenError(Exception):
    """A token was absent, malformed, expired, or revoked."""


def _now() -> datetime:
    return datetime.now(UTC)


def create_token(
    *,
    user_id: UUID,
    kind: TokenKind,
    settings: Settings,
    issued_at: datetime | None = None,
    session_id: UUID | None = None,
) -> str:
    """Sign an RS256 token.

    ``iat`` is carried explicitly because every verification compares it
    against ``users.sessions_invalidated_before``. That single comparison is
    what makes global revocation possible without tracking every issued token
    — a password reset bumps one column and every outstanding token dies.
    """
    moment = issued_at or _now()
    ttl = settings.jwt_access_ttl_seconds if kind == "access" else settings.jwt_refresh_ttl_seconds
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "typ": kind,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(moment.timestamp()),
        "exp": int((moment + timedelta(seconds=ttl)).timestamp()),
    }
    if session_id is not None:
        claims["sid"] = str(session_id)
    return jwt.encode(claims, settings.jwt_private_key.get_secret_value(), algorithm="RS256")


def decode_token(token: str, *, kind: TokenKind, settings: Settings) -> dict[str, Any]:
    """Verify signature, expiry, issuer, audience **and type**.

    Checking ``typ`` matters: without it a refresh token — which is long-lived
    — would be accepted as an access token, and the fifteen-minute access
    window would be decorative.
    """
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_public_key,
            algorithms=["RS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "iat", "sub", "typ"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc

    if claims.get("typ") != kind:
        raise TokenError(f"expected a {kind} token, got {claims.get('typ')!r}")
    return claims


def token_is_live(claims: dict[str, Any], invalidated_before: datetime | None) -> bool:
    """Whether the token predates a global session invalidation."""
    if invalidated_before is None:
        return True
    issued = datetime.fromtimestamp(int(claims["iat"]), tz=UTC)
    # `>` not `>=`: a token issued in the same second as the invalidation is
    # treated as invalidated. Erring toward signing someone out is the right
    # direction on a password reset.
    return issued > invalidated_before
