"""The payment provider behind one interface.

**Two-phase capture is the point.** ``authorize`` places a hold and charges
nothing; the solve runs; only a *successful* solve captures. A solver failure
voids the hold, so an athlete is never charged for a plan they did not get.
That sequence is what this interface exists to make expressible — a gateway
that could only "charge" would force the charge to happen before the outcome
was known.

Two implementations ship, mirroring :mod:`raceos.storage`:

:class:`~raceos.payments.stripe.StripeGateway`
    The real one. Manual-capture PaymentIntents over Stripe's HTTP API.

:class:`InMemoryPaymentGateway`
    A complete, working gateway that keeps intents in process memory. Not a
    mock: it enforces the same state machine — an authorization can be
    captured once, or voided once, never both — so a test that passes against
    it is testing the caller rather than a stub's indulgence. The brief
    requires the suite to run fully offline with no credentials, and this is
    how the billing paths are exercised without one.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from raceos.config import Settings, get_settings


class PaymentError(RuntimeError):
    """Any payment operation that did not succeed."""


class PaymentStateError(PaymentError):
    """The intent is not in a state where this operation is legal.

    Distinct from a transport failure: capturing a voided authorization is a
    caller bug that a retry cannot fix, and the two must not be conflated.
    """


class WebhookVerificationError(PaymentError):
    """The signature did not verify, so the payload is not from the provider."""


@dataclass(frozen=True)
class PaymentIntent:
    """One authorization and its lifecycle, as the provider reports it."""

    id: str
    amount_cents: int
    currency: str
    #: `requires_capture`, `succeeded`, `canceled` — the provider's vocabulary,
    #: deliberately not translated here. The service layer maps it to
    #: `PurchaseStatus`, and keeping the raw value visible means a provider
    #: state we did not anticipate surfaces as an error rather than as a
    #: silently wrong mapping.
    status: str
    client_secret: str | None = None
    captured_at: datetime | None = None
    voided_at: datetime | None = None


@dataclass(frozen=True)
class ProviderRefund:
    id: str
    intent_id: str
    amount_cents: int
    status: str


@dataclass(frozen=True)
class WebhookEvent:
    id: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)


class PaymentGateway(ABC):
    """What the rest of the application may assume about payments."""

    @abstractmethod
    def authorize(
        self,
        *,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        description: str,
        customer_ref: str | None = None,
    ) -> PaymentIntent:
        """Place a hold. **Charges nothing.**

        ``idempotency_key`` is the provider's own idempotency mechanism, not
        ours: a retried request must return the first intent rather than
        placing a second hold.
        """

    @abstractmethod
    def capture(self, intent_id: str) -> PaymentIntent:
        """Take the money that was held. Only after a successful solve."""

    @abstractmethod
    def void(self, intent_id: str) -> PaymentIntent:
        """Release the hold without charging."""

    @abstractmethod
    def refund(self, *, intent_id: str, amount_cents: int, reason: str) -> ProviderRefund:
        """Return money already captured."""

    @abstractmethod
    def verify_webhook(self, *, payload: bytes, signature_header: str) -> WebhookEvent:
        """Verify the signature and parse the event.

        Raises :class:`WebhookVerificationError` on anything that does not
        verify. An unverified webhook is an attacker asserting that a payment
        succeeded, so this is a security boundary, not a parsing convenience.
        """

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Detail for ``/readyz``. Raises if the gateway is unusable."""


class InMemoryPaymentGateway(PaymentGateway):
    """A working gateway holding intents in process memory."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._lock = threading.Lock()
        self._intents: dict[str, PaymentIntent] = {}
        self._by_idempotency_key: dict[str, str] = {}
        self._refunds: dict[str, ProviderRefund] = {}

    def authorize(
        self,
        *,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        description: str,
        customer_ref: str | None = None,
    ) -> PaymentIntent:
        if amount_cents < 0:
            raise PaymentError("amount_cents cannot be negative")
        with self._lock:
            existing = self._by_idempotency_key.get(idempotency_key)
            if existing is not None:
                return self._intents[existing]
            intent_id = f"pi_mem_{uuid.uuid4().hex}"
            intent = PaymentIntent(
                id=intent_id,
                amount_cents=amount_cents,
                currency=currency.lower(),
                status="requires_capture",
                client_secret=f"{intent_id}_secret_{uuid.uuid4().hex[:16]}",
            )
            self._intents[intent_id] = intent
            self._by_idempotency_key[idempotency_key] = intent_id
            return intent

    def _transition(self, intent_id: str, *, to: str, stamp: str) -> PaymentIntent:
        with self._lock:
            intent = self._intents.get(intent_id)
            if intent is None:
                raise PaymentError(f"no such payment intent: {intent_id}")
            if intent.status != "requires_capture":
                raise PaymentStateError(
                    f"intent {intent_id} is {intent.status}; it can no longer be "
                    f"{'captured' if to == 'succeeded' else 'voided'}"
                )
            now = datetime.now(UTC)
            updated = PaymentIntent(
                id=intent.id,
                amount_cents=intent.amount_cents,
                currency=intent.currency,
                status=to,
                client_secret=intent.client_secret,
                captured_at=now if stamp == "captured" else None,
                voided_at=now if stamp == "voided" else None,
            )
            self._intents[intent_id] = updated
            return updated

    def capture(self, intent_id: str) -> PaymentIntent:
        return self._transition(intent_id, to="succeeded", stamp="captured")

    def void(self, intent_id: str) -> PaymentIntent:
        return self._transition(intent_id, to="canceled", stamp="voided")

    def refund(self, *, intent_id: str, amount_cents: int, reason: str) -> ProviderRefund:
        with self._lock:
            intent = self._intents.get(intent_id)
            if intent is None:
                raise PaymentError(f"no such payment intent: {intent_id}")
            if intent.status != "succeeded":
                raise PaymentStateError(
                    f"intent {intent_id} is {intent.status}; only a captured "
                    f"payment can be refunded"
                )
            if amount_cents <= 0 or amount_cents > intent.amount_cents:
                raise PaymentError(
                    f"refund of {amount_cents} is outside the captured " f"{intent.amount_cents}"
                )
            refund = ProviderRefund(
                id=f"re_mem_{uuid.uuid4().hex}",
                intent_id=intent_id,
                amount_cents=amount_cents,
                status="succeeded",
            )
            self._refunds[refund.id] = refund
            return refund

    def verify_webhook(self, *, payload: bytes, signature_header: str) -> WebhookEvent:
        """The same signature scheme the real gateway uses.

        Implemented rather than waved through so the webhook route's rejection
        path is exercised offline: a test can send a wrong signature and see a
        400, which is the behaviour that actually matters.
        """
        import json

        secret = self._settings.stripe_webhook_secret.get_secret_value()
        verify_signature(payload=payload, signature_header=signature_header, secret=secret)
        body = json.loads(payload.decode("utf-8"))
        return WebhookEvent(
            id=str(body.get("id", "")),
            type=str(body.get("type", "")),
            data=dict(body.get("data", {})),
        )

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "gateway": "in_memory",
                "intents": len(self._intents),
                "refunds": len(self._refunds),
            }

    def clear(self) -> None:
        """Test helper: forget everything."""
        with self._lock:
            self._intents.clear()
            self._by_idempotency_key.clear()
            self._refunds.clear()


#: How long a signed webhook stays acceptable. Stripe's own default; a replay
#: of yesterday's "payment succeeded" must not be honoured today.
WEBHOOK_TOLERANCE_SECONDS = 300


def sign_webhook(*, payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Produce a ``Stripe-Signature`` header for *payload*.

    Lives here, beside the verifier, because the test suite needs to construct
    a valid signature offline and a second implementation in the tests could
    drift from this one — at which point the verifier would be tested against
    its own mistake.
    """
    moment = timestamp if timestamp is not None else int(time.time())
    signature = hmac.new(
        secret.encode("utf-8"), f"{moment}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={moment},v1={signature}"


def verify_signature(*, payload: bytes, signature_header: str, secret: str) -> int:
    """Verify a ``Stripe-Signature`` header. Returns the signed timestamp."""
    if not secret:
        raise WebhookVerificationError(
            "no webhook secret is configured, so no webhook can be trusted"
        )
    parts = dict(item.split("=", 1) for item in signature_header.split(",") if "=" in item)
    raw_timestamp = parts.get("t")
    provided = parts.get("v1")
    if not raw_timestamp or not provided:
        raise WebhookVerificationError("signature header is missing `t` or `v1`")
    try:
        moment = int(raw_timestamp)
    except ValueError as error:
        raise WebhookVerificationError("signature timestamp is not an integer") from error

    if abs(int(time.time()) - moment) > WEBHOOK_TOLERANCE_SECONDS:
        raise WebhookVerificationError("signature timestamp is outside the replay tolerance")

    expected = hmac.new(
        secret.encode("utf-8"), f"{moment}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    # Constant time: a byte-by-byte comparison leaks how much of a forged
    # signature was correct, which is enough to forge the rest.
    if not hmac.compare_digest(expected, provided):
        raise WebhookVerificationError("signature does not match the payload")
    return moment


_gateway: PaymentGateway | None = None


def get_payment_gateway(settings: Settings | None = None) -> PaymentGateway:
    """The process-wide gateway.

    Chosen by configuration, not by environment name: with a Stripe secret key
    present the real gateway is used, otherwise the in-memory one, which is
    what lets a local run and the whole test suite work with no credentials.
    A staging or production boot without that key has already been refused by
    :class:`~raceos.config.Settings`, so this cannot silently downgrade a real
    deployment to a gateway that charges nobody.
    """
    global _gateway
    if _gateway is not None:
        return _gateway

    settings = settings or get_settings()
    if settings.stripe_secret_key.get_secret_value().strip():
        from raceos.payments.stripe import StripeGateway

        _gateway = StripeGateway(settings)
    else:
        _gateway = InMemoryPaymentGateway(settings)
    return _gateway


def set_payment_gateway(gateway: PaymentGateway | None) -> None:
    """Override the gateway. For tests and for the seed script."""
    global _gateway
    _gateway = gateway
