"""Authentication. Email and password only.

``GET /auth/providers`` returns an empty list and there is no OAuth code
anywhere behind it — the endpoint exists so the frontend can hide those
buttons rather than render dead ones.

The refresh token travels as an **httpOnly, secure, sameSite cookie** and is
never in a response body: a body-borne refresh token is readable by any script
on the page, which defeats the point of rotating it.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Cookie, Header, Request, Response, status

from raceos.api import deps
from raceos.api.deps import Config, CurrentUser, DbSession
from raceos.api.schemas.auth import (
    AuthResponse,
    ErasureImpactOut,
    ErasureRequest,
    ForgotPasswordRequest,
    LoginRequest,
    ProfileUpdate,
    ResetPasswordRequest,
    SignupRequest,
    UserOut,
)
from raceos.config import AppEnv, Settings
from raceos.services import auth_service
from raceos.services.auth_service import AuthResult
from raceos.services.rate_limit import enforce_rate_limit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _client_ip(request: Request, settings: Settings) -> str | None:
    """Delegates to the shared resolver, which honours the proxy chain.

    Taking the socket address directly put every caller behind Render's proxy
    into one rate-limit bucket, which made the per-IP limits on these three
    routes a shared quota an attacker could exhaust for everyone.
    """
    return deps.client_ip(request, settings)


def _cookie_policy(settings: Settings) -> tuple[bool, Literal["lax", "none"]]:
    """``(secure, samesite)`` for the refresh cookie, per environment.

    Outside development the frontend and this API are served from different
    registrable domains — a static site on one host, this service on another —
    so **every** call the browser makes to us is cross-site. A ``SameSite=Lax``
    cookie is not sent on a cross-site request, which means ``POST
    /auth/refresh`` would never see one: sessions would die at the end of the
    access token's TTL and would not survive a page reload.

    ``SameSite=None`` is therefore correct in staging and production, and it
    requires ``Secure``, which is already set there. Development keeps ``Lax``:
    ``localhost:3000`` and ``localhost:8000`` are same-site, so ``Lax`` works,
    and it avoids requiring ``Secure`` over plain http.
    """
    if settings.app_env is AppEnv.DEVELOPMENT:
        return False, "lax"
    return True, "none"


def _set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    secure, samesite = _cookie_policy(settings)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.jwt_refresh_ttl_seconds,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/api/v1/auth",
        domain=settings.session_cookie_domain or None,
    )


def _auth_response(result: AuthResult, response: Response, settings: Settings) -> AuthResponse:
    _set_refresh_cookie(response, result.refresh_token, settings)
    return AuthResponse(
        user=UserOut.model_validate(result.user),
        access_token=result.access_token,
        expires_in=settings.jwt_access_ttl_seconds,
    )


@router.get("/providers", summary="Available social login providers")
def list_providers() -> dict[str, list[Any]]:
    """Always empty. Email and password only.

    Google and Apple OAuth are deliberately not built. An empty list is a
    working answer, not a stub: there is no provider code behind this endpoint
    for it to describe.
    """
    return {"providers": []}


@router.post("/signup", status_code=status.HTTP_201_CREATED, summary="Create an account")
def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    session: DbSession,
    settings: Config,
    user_agent: Annotated[str | None, Header()] = None,
) -> AuthResponse:
    enforce_rate_limit(
        session,
        subject=f"ip:{_client_ip(request, settings)}",
        bucket="auth.signup",
        limit=settings.rate_limit_auth_per_minute,
        settings=settings,
    )
    result = auth_service.signup(
        session,
        email=payload.email,
        password=payload.password,
        name=payload.name,
        settings=settings,
        user_agent=user_agent,
        ip=_client_ip(request, settings),
    )
    session.commit()
    return _auth_response(result, response, settings)


@router.post("/login", summary="Sign in")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: DbSession,
    settings: Config,
    user_agent: Annotated[str | None, Header()] = None,
) -> AuthResponse:
    # Per-IP rate limiting is defence in depth *behind* the per-account
    # lockout, not a substitute for it: limiting only by IP lets a botnet
    # spread attempts, and limiting only by account lets one request lock a
    # stranger out.
    enforce_rate_limit(
        session,
        subject=f"ip:{_client_ip(request, settings)}",
        bucket="auth.login",
        limit=settings.rate_limit_auth_per_minute,
        settings=settings,
    )
    try:
        result = auth_service.login(
            session,
            email=payload.email,
            password=payload.password,
            settings=settings,
            user_agent=user_agent,
            ip=_client_ip(request, settings),
        )
    except Exception:
        # The failed-attempt counter must survive the rejection, so commit the
        # increment before re-raising. Without this, a rollback would reset it
        # and the lockout would never trigger.
        session.commit()
        raise
    session.commit()
    return _auth_response(result, response, settings)


#: The header that proves a refresh came from our own client.
#:
#: This endpoint is authenticated by the refresh cookie alone, and in
#: production that cookie is `SameSite=None` because the frontend and the API
#: are on different registrable domains. Without this header the request was a
#: CORS "simple request": any page on the internet could POST to it with the
#: victim's cookie attached. The attacker could not read the reply — CORS
#: stops that — but `Set-Cookie` still applied, so the token rotated under the
#: victim's own tab. The next legitimate refresh then presented a rotated
#: token, reuse detection fired exactly as designed, and every session for
#: that user was revoked. Any website could sign any athlete out, on repeat.
#:
#: A header outside the CORS safelist cannot be sent cross-origin without a
#: preflight, and the preflight is answered against `CORS_ALLOWED_ORIGINS`.
#: So this turns the attack into a request the browser refuses to make.
CLIENT_HEADER = "X-RaceOS-Client"


@router.post("/refresh", summary="Rotate the session")
def refresh(
    request: Request,
    response: Response,
    session: DbSession,
    settings: Config,
    user_agent: Annotated[str | None, Header()] = None,
    raceos_refresh: Annotated[str | None, Cookie()] = None,
    x_raceos_client: Annotated[str | None, Header()] = None,
) -> AuthResponse:
    from raceos.api.errors import Unauthenticated

    if not x_raceos_client:
        # Deliberately the same answer as a missing cookie: a cross-origin
        # caller learns nothing about whether a session existed.
        raise Unauthenticated("No session to refresh.")
    if not raceos_refresh:
        raise Unauthenticated("No session to refresh.")
    result = auth_service.refresh_session(
        session,
        refresh_token=raceos_refresh,
        settings=settings,
        user_agent=user_agent,
        ip=_client_ip(request, settings),
    )
    session.commit()
    return _auth_response(result, response, settings)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    # `response_model=None` is required, not decorative: this module uses
    # `from __future__ import annotations`, so FastAPI resolves the `-> None`
    # return annotation through `get_type_hints` and infers `NoneType` as a
    # response model — which it then refuses to pair with a 204.
    response_model=None,
    summary="Sign out",
)
def logout(
    response: Response,
    session: DbSession,
    settings: Config,
    user: CurrentUser,
    raceos_refresh: Annotated[str | None, Cookie()] = None,
) -> None:
    auth_service.logout(session, refresh_token=raceos_refresh, user_id=user.id)
    session.commit()
    # Same attributes as `_set_refresh_cookie`: a deletion whose `secure`,
    # `samesite` or `domain` differ addresses a different cookie, and the real
    # one would survive the logout.
    secure, samesite = _cookie_policy(settings)
    response.delete_cookie(
        settings.session_cookie_name,
        path="/api/v1/auth",
        domain=settings.session_cookie_domain or None,
        secure=secure,
        httponly=True,
        samesite=samesite,
    )


@router.post(
    "/forgot-password",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a password reset",
)
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    session: DbSession,
    settings: Config,
) -> dict[str, str]:
    """**Always 202, whether or not the address exists.**

    A hard requirement (Part 8.5): any difference in status, body or timing
    turns this endpoint into an account enumerator.
    """
    enforce_rate_limit(
        session,
        subject=f"ip:{_client_ip(request, settings)}",
        bucket="auth.forgot",
        limit=settings.rate_limit_auth_per_minute,
        settings=settings,
    )
    auth_service.request_password_reset(session, email=payload.email, settings=settings)
    session.commit()
    return {"message": "If that address has an account, a reset link is on its way."}


@router.post("/reset-password", summary="Set a new password")
def reset_password(
    payload: ResetPasswordRequest, session: DbSession, settings: Config
) -> dict[str, str]:
    """Single-use, time-bounded, and it signs every other session out."""
    auth_service.reset_password(
        session,
        token=payload.token,
        new_password=payload.new_password,
        settings=settings,
    )
    session.commit()
    return {"message": "Password updated. You have been signed out everywhere else."}


@router.get("/verify-email/{token}", summary="Confirm an email address")
def verify_email(token: str, session: DbSession) -> dict[str, str]:
    auth_service.verify_email(session, token=token)
    session.commit()
    return {"message": "Email confirmed."}


@router.post(
    "/resend-verification",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send the verification message again",
)
def resend_verification(session: DbSession, settings: Config, user: CurrentUser) -> dict[str, str]:
    auth_service.resend_verification(session, user=user, settings=settings)
    session.commit()
    return {"message": "Verification message sent."}


@router.get("/me", summary="The signed-in athlete")
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/me/erasure-impact", summary="What deleting this account would destroy")
def erasure_impact(session: DbSession, user: CurrentUser) -> ErasureImpactOut:
    """Read before the delete, so the confirmation states facts.

    "4 plans and 2 invoices" is a different decision from "0 and 0", and a
    generic warning makes both look the same.
    """
    return ErasureImpactOut.model_validate(auth_service.erasure_impact(session, user=user))


@router.delete(
    "/me",
    status_code=status.HTTP_200_OK,
    summary="Delete this account (GDPR erasure)",
)
def erase_me(payload: ErasureRequest, session: DbSession, user: CurrentUser) -> ErasureImpactOut:
    """**Irreversible, and only ever by the person who holds the account.**

    Not a hard delete. Invoices are financial records with statutory
    retention, plans a coach built are that coach's work too, and the audit
    log has to stay referentially intact — so the row survives as a tombstone
    with an id, a state and a timestamp, and every field that identifies a
    person is scrubbed. Enough for an invoice to point somewhere; not enough
    to say who it was.

    Every session is revoked and every token issued before now stops
    verifying, so an access token still sitting in another tab dies with the
    account rather than outliving it. The response is the last thing this
    account will ever receive.
    """
    impact = auth_service.erase_account(
        session,
        user=user,
        actor=user,
        confirmation=payload.confirmation,
        reason=payload.reason,
    )
    session.commit()
    return ErasureImpactOut.model_validate(impact)


@router.patch("/me", summary="Change your own details")
def update_me(payload: ProfileUpdate, session: DbSession, user: CurrentUser) -> UserOut:
    """Whatever was sent, and nothing that was not.

    ``exclude_unset`` matters more here than it looks: a settings form that
    round-trips the whole object would wipe every field the screen does not
    render, and the emergency contact is exactly the field a screen forgets.
    Only keys the client actually sent are written.

    The actor is the token's own user and there is no id parameter, so there is
    no shape of request that edits somebody else.
    """
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "country" and isinstance(value, str):
            value = value.strip().upper()[:2] or None
        if isinstance(value, str) and field != "country":
            value = value.strip() or None
        setattr(user, field, value)
    session.commit()
    session.refresh(user)
    return UserOut.model_validate(user)
