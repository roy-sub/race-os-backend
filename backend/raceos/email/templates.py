"""Deterministic message templates.

Every message type has one, and they are the shipping path rather than a
fallback: ``PHRASING_ENABLED=false`` in V1. **Every correct number is still
present** — the templates are plainer prose, not degraded data.
"""

from __future__ import annotations

from uuid import UUID

from raceos.config import Settings
from raceos.email.sender import RenderedEmail


def verification_email(
    *,
    to_address: str,
    name: str | None,
    token: str,
    settings: Settings,
    user_id: UUID | None = None,
) -> RenderedEmail:
    link = f"{settings.app_base_url.rstrip('/')}/verify-email/{token}"
    greeting = f"Hi {name}," if name else "Hi,"
    return RenderedEmail(
        to_address=to_address,
        user_id=user_id,
        subject="Confirm your RaceOS email address",
        template_key="auth.verify_email",
        delivery_link=link,
        body_text=(
            f"{greeting}\n\n"
            f"Confirm your email address to finish setting up RaceOS:\n\n"
            f"  {link}\n\n"
            f"The link is valid for {settings.email_verification_ttl_hours} hours.\n"
            f"If you did not create an account, you can ignore this message.\n"
        ),
    )


def password_reset_email(
    *,
    to_address: str,
    name: str | None,
    token: str,
    settings: Settings,
    user_id: UUID | None = None,
) -> RenderedEmail:
    link = f"{settings.app_base_url.rstrip('/')}/reset-password?token={token}"
    greeting = f"Hi {name}," if name else "Hi,"
    return RenderedEmail(
        to_address=to_address,
        user_id=user_id,
        subject="Reset your RaceOS password",
        template_key="auth.password_reset",
        delivery_link=link,
        body_text=(
            f"{greeting}\n\n"
            f"Use this link to choose a new password:\n\n"
            f"  {link}\n\n"
            f"It is valid for {settings.password_reset_ttl_minutes} minutes and can "
            f"be used once. Resetting your password signs you out everywhere else.\n\n"
            f"If you did not ask for this, nothing has changed and you can ignore it.\n"
        ),
    )


def coach_invite_email(
    *,
    to_address: str,
    coach_name: str | None,
    token: str,
    settings: Settings,
    user_id: UUID | None = None,
) -> RenderedEmail:
    link = f"{settings.app_base_url.rstrip('/')}/coach/invite/{token}"
    who = coach_name or "A coach"
    return RenderedEmail(
        to_address=to_address,
        user_id=user_id,
        subject=f"{who} would like to coach you on RaceOS",
        template_key="coach.invite",
        delivery_link=link,
        body_text=(
            f"{who} has invited you to link your RaceOS account.\n\n"
            f"  {link}\n\n"
            f"You choose what they can see, and you can revoke it at any time.\n"
            f"A coach can never see or change your body's numbers — not at any "
            f"permission level.\n"
        ),
    )


def notification_email(
    *,
    to_address: str,
    title: str,
    body: str,
    cta_href: str | None,
    settings: Settings,
    user_id: UUID | None = None,
) -> RenderedEmail:
    tail = f"\n\n  {settings.app_base_url.rstrip('/')}{cta_href}\n" if cta_href else "\n"
    return RenderedEmail(
        to_address=to_address,
        user_id=user_id,
        subject=title,
        template_key="notification.generic",
        delivery_link=(f"{settings.app_base_url.rstrip('/')}{cta_href}" if cta_href else None),
        body_text=f"{title}\n\n{body}{tail}",
    )
