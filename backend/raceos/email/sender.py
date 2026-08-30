"""Email: the whole subsystem, with a transport that sends nothing in V1.

``EMAIL_ENABLED=false``, and the bound transport is :class:`LoggingEmailSender`,
which renders every message in full and writes it to structured logs and to the
``email_messages`` table. The subsystem is therefore exercised end to end —
templates, preference matrix, fan-out — without a provider account.

The consequences are handled rather than papered over:

* ``REQUIRE_EMAIL_VERIFICATION=false``, so new accounts are created already
  verified. The whole verification flow is built and tested, so flipping the
  flag later needs no code change.
* Password reset writes its link to the log and to the database, and an
  admin-only endpoint hands it to support. **It is never in a public response**
  — that would turn "forgot password" into an account-takeover primitive.
* In-app notifications are the live V1 channel. Email and push are transports
  that are currently off, not features that are missing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from raceos.config import EmailTransport, Settings
from raceos.db.models import EmailMessage
from raceos.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class RenderedEmail:
    to_address: str
    subject: str
    template_key: str
    body_text: str
    body_html: str | None = None
    user_id: UUID | None = None
    #: A link the message contains, retained so support can hand it over while
    #: delivery is a no-op. Never returned by a public endpoint.
    delivery_link: str | None = None


@dataclass(frozen=True)
class SendResult:
    delivered: bool
    provider_message_id: str | None = None
    error: str | None = None


class EmailSender(ABC):
    """What the notification fan-out is allowed to assume about email."""

    @abstractmethod
    def send(self, message: RenderedEmail) -> SendResult:
        """Attempt delivery. Never raises: a failed email is not a failed request."""


class LoggingEmailSender(EmailSender):
    """The V1 transport. Renders in full, delivers nothing.

    Not a stub that discards the message — the point is that everything up to
    the wire is real, so turning delivery on later changes one config value
    rather than exposing a pile of untested code.
    """

    def send(self, message: RenderedEmail) -> SendResult:
        logger.info(
            "email rendered (delivery disabled)",
            extra={
                "email_to": message.to_address,
                "email_subject": message.subject,
                "email_template": message.template_key,
                "email_body": message.body_text,
                "email_link": message.delivery_link,
            },
        )
        return SendResult(delivered=False, error="EMAIL_ENABLED=false")


class HttpEmailSender(EmailSender):
    """The real adapter, Resend/Postmark-shaped. **Unused in V1.**

    Present so that enabling email is filling in a blank rather than writing
    code. It is never constructed while ``EMAIL_ENABLED`` is false, and the
    settings object only requires its API key when the flag is on.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._endpoint = (
            "https://api.resend.com/emails"
            if settings.email_transport is EmailTransport.RESEND
            else "https://api.postmarkapp.com/email"
        )
        self._client = client or httpx.Client(timeout=15.0)

    def send(self, message: RenderedEmail) -> SendResult:
        key = self._settings.email_provider_api_key.get_secret_value()
        if not key:
            return SendResult(delivered=False, error="no provider API key configured")
        try:
            response = self._client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "from": f"{self._settings.email_from_name} "
                    f"<{self._settings.email_from_address}>",
                    "to": [message.to_address],
                    "subject": message.subject,
                    "text": message.body_text,
                    **({"html": message.body_html} if message.body_html else {}),
                },
            )
        except httpx.HTTPError as exc:
            return SendResult(delivered=False, error=type(exc).__name__)

        if response.status_code >= 400:
            return SendResult(delivered=False, error=f"HTTP {response.status_code}")
        return SendResult(delivered=True, provider_message_id=response.json().get("id"))


def build_sender(settings: Settings) -> EmailSender:
    """Bind the configured transport.

    ``EMAIL_ENABLED=false`` binds the logging sender **whatever the transport
    says**, so turning the transport to `resend` without also enabling email
    cannot start sending real mail by accident.
    """
    if not settings.email_enabled or settings.email_transport is EmailTransport.LOGGING:
        return LoggingEmailSender()
    return HttpEmailSender(settings)


def deliver(session: Session, message: RenderedEmail, settings: Settings) -> EmailMessage:
    """Send and record. **Every rendered message is persisted**, sent or not.

    That is what lets support read out a reset link that could not be
    delivered, and what makes the email subsystem observable in V1 at all.
    """
    result = build_sender(settings).send(message)
    record = EmailMessage(
        user_id=message.user_id,
        to_address=message.to_address,
        from_address=settings.email_from_address,
        subject=message.subject,
        template_key=message.template_key,
        body_text=message.body_text,
        body_html=message.body_html,
        transport=settings.email_transport.value,
        delivered=result.delivered,
        delivery_error=result.error,
        provider_message_id=result.provider_message_id,
    )
    session.add(record)
    session.flush()
    return record
