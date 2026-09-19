"""Request dependencies: database session, the current actor, authorization.

**Authorization is checked per request, never cached at token issuance**
(Part 8.7). Every repository read that touches athlete data takes an explicit
actor, so "who is asking" is a parameter rather than ambient state — that is
what makes a coach link or a support grant revocable *immediately*, including
on a page that is already open.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.errors import Forbidden, Unauthenticated, WarningCollector
from raceos.config import Settings, get_settings
from raceos.db.models import AdminRoleAssignment, User
from raceos.db.session import get_session_factory
from raceos.domain.enums import AccountState, AdminRole
from raceos.logging import actor_id_var
from raceos.services import rate_limit, security

#: Paths the global limiter leaves alone.
#:
#: Health checks are polled every few seconds by the platform and must never
#: be throttled; the payment webhook is retried by Stripe and is authenticated
#: by signature rather than by origin, so throttling it risks dropping a
#: payment event rather than an attack.
_UNMETERED_PATHS = frozenset({"/healthz", "/readyz", "/metrics", "/webhooks/payments"})


def get_db() -> Iterator[Session]:
    """A session scoped to the request.

    Committed by the caller, never here: a route that reads should not be able
    to commit by accident, and a route that writes says so explicitly.
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def get_config(request: Request) -> Settings:
    """The settings this app was built with.

    Read from ``app.state`` rather than from the process-wide
    :func:`get_settings`, because ``create_app(settings)`` accepts an explicit
    object and it has to be the one that is actually used — otherwise the
    parameter is decorative and a test (or a second app in one process) is
    silently running against a different configuration than it asked for.
    """
    configured = getattr(request.app.state, "settings", None)
    if isinstance(configured, Settings):
        return configured
    return get_settings()  # pragma: no cover - only when app.state is unset


def get_warnings(request: Request) -> WarningCollector:
    """The request's warning collector.

    Lets a service attach `STALE_DATA` or `PARTIAL_DATA` without knowing how
    the response is serialised.
    """
    collector = getattr(request.state, "warnings", None)
    if collector is None:  # pragma: no cover - middleware always sets it
        collector = WarningCollector()
        request.state.warnings = collector
    return collector


DbSession = Annotated[Session, Depends(get_db)]
Config = Annotated[Settings, Depends(get_config)]
Warnings = Annotated[WarningCollector, Depends(get_warnings)]


def client_ip(request: Request, settings: Settings) -> str | None:
    """The caller's address, as well as it can be known.

    Behind a proxy the socket address is the proxy's, so every caller would
    otherwise share one rate-limit bucket and one attacker could exhaust the
    quota for everybody. `X-Forwarded-For` carries the real chain, and its
    leftmost entry is the originating client — but only when something
    upstream is guaranteed to overwrite a caller-supplied header, which is
    why this is gated on `TRUST_PROXY_HEADERS` rather than simply preferred.
    Off, an attacker sets their own address and defeats every per-IP limit.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("X-Forwarded-For", "")
        first = forwarded.split(",")[0].strip()
        if first:
            # Bounded: it reaches a rate-limit key and a hash, and an
            # unbounded caller-controlled value in either is a liability.
            return first[:64]
    return request.client.host if request.client else None


def enforce_default_rate_limit(
    request: Request,
    session: DbSession,
    settings: Config,
) -> None:
    """A ceiling on every request, whoever is asking.

    `RATE_LIMIT_DEFAULT_PER_MINUTE` existed as configuration for a long time
    without anything reading it: only three auth routes and the share-code
    lookup were limited, so everything else — search, recon, the solver — was
    unmetered. This is the floor beneath the stricter per-route limits, which
    still apply on top of it.

    Keyed by account when there is one, so a signed-in user on a shared or
    carrier-grade NAT address is not throttled by their neighbours, and by
    address otherwise.
    """
    if request.method == "OPTIONS" or request.url.path in _UNMETERED_PATHS:
        return

    subject = f"ip:{client_ip(request, settings)}"
    authorization = request.headers.get("Authorization")
    if authorization and authorization.lower().startswith("bearer "):
        try:
            claims = security.decode_token(
                authorization.split(" ", 1)[1].strip(), kind="access", settings=settings
            )
        except security.TokenError:
            pass
        else:
            subject = f"user:{claims['sub']}"

    rate_limit.enforce_rate_limit(
        session,
        subject=subject,
        bucket="global",
        limit=settings.rate_limit_default_per_minute,
        settings=settings,
    )


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthenticated("Sign in to continue.")
    return authorization.split(" ", 1)[1].strip()


def current_user(
    request: Request,
    session: DbSession,
    settings: Config,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """The authenticated athlete, or 401.

    Three checks, and the third is the one people forget: the signature must
    verify, the account must be usable, and the token must postdate any global
    session invalidation. Without the third, a password reset would not
    actually sign anyone out until their access token happened to expire.
    """
    token = _bearer(authorization)
    try:
        claims = security.decode_token(token, kind="access", settings=settings)
    except security.TokenError as exc:
        raise Unauthenticated("Your session has expired. Please sign in again.") from exc

    # A signed token with a non-UUID `sub` cannot be forged without the
    # private key, but parsing it unguarded turns a malformed credential into
    # a 500 instead of a 401, and `optional_user` would propagate that out of
    # an endpoint that is supposed to tolerate a bad token.
    try:
        subject = UUID(claims["sub"])
    except (TypeError, ValueError) as exc:
        raise Unauthenticated("Your session is no longer valid.") from exc

    user = session.get(User, subject)
    if user is None or user.account_state is AccountState.ERASED:
        raise Unauthenticated("Your session is no longer valid.")

    if not security.token_is_live(claims, user.sessions_invalidated_before):
        raise Unauthenticated("Your session was ended. Please sign in again.")

    request.state.actor = user
    actor_id_var.set(str(user.id))
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def optional_user(
    request: Request,
    session: DbSession,
    settings: Config,
    authorization: Annotated[str | None, Header()] = None,
) -> User | None:
    """For endpoints that are public but richer when signed in."""
    if not authorization:
        return None
    try:
        return current_user(request, session, settings, authorization)
    except Unauthenticated:
        return None


OptionalUser = Annotated[User | None, Depends(optional_user)]


def admin_roles(session: Session, user: User) -> set[AdminRole]:
    return {
        row.role
        for row in session.scalars(
            select(AdminRoleAssignment).where(AdminRoleAssignment.user_id == user.id)
        )
    }


def require_roles(*allowed: AdminRole) -> Callable[[Session, User], User]:
    """RBAC by role, **never a boolean** (Part 6.9).

    Support cannot see bundle publish controls or the refunds workspace, and
    that is expressed by not holding the role rather than by a UI condition.
    ``ADMIN`` implies the others: an admin is not locked out of an ops screen.
    """

    def dependency(session: DbSession, user: CurrentUser) -> User:
        held = admin_roles(session, user)
        if AdminRole.ADMIN in held or held & set(allowed):
            return user
        raise Forbidden(
            "You do not have access to this area.",
            details={"required_role": [role.value for role in allowed]},
        )

    return dependency


def require_internal_secret(
    settings: Config,
    x_internal_job_secret: Annotated[str | None, Header()] = None,
) -> None:
    """The shared-secret guard on ``/internal/jobs/*``.

    V1 ships no scheduler: an external cron calls these endpoints. This secret
    is the only thing between the public internet and every background job, so
    the comparison is constant-time and a missing configuration refuses rather
    than defaults open.
    """
    expected = settings.internal_job_secret.get_secret_value()
    if not expected:
        raise Forbidden("Internal job endpoints are not configured.")
    if not x_internal_job_secret or not security.secrets_match(x_internal_job_secret, expected):
        raise Forbidden("Invalid internal job secret.")
