"""Stripe implementation of :class:`~raceos.payments.base.PaymentGateway`.

Stripe's HTTP API directly, over ``httpx``, rather than the ``stripe`` SDK.
Four calls are needed — create a manual-capture PaymentIntent, capture it,
cancel it, refund it — and the SDK brings a large dependency, its own retry
and logging behaviour, and a habit of reading ``STRIPE_API_KEY`` out of the
environment on import. Reading configuration from :class:`Settings` and
nowhere else is a rule in this codebase, so the four calls are written out.

**Nothing here ever logs a key, a client secret, or a full request body.**
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from raceos.config import Settings, get_settings
from raceos.payments.base import (
    PaymentError,
    PaymentGateway,
    PaymentIntent,
    PaymentStateError,
    ProviderRefund,
    WebhookEvent,
    verify_signature,
)

API_BASE = "https://api.stripe.com/v1"

#: Stripe reports a state that is not ours to interpret. These are the ones
#: this integration understands; anything else raises rather than being mapped
#: onto the nearest familiar value.
CAPTURABLE = "requires_capture"


class StripeGateway(PaymentGateway):
    """Manual-capture PaymentIntents. Authorize now, capture on success."""

    def __init__(
        self, settings: Settings | None = None, client: httpx.Client | None = None
    ) -> None:
        self._settings = settings or get_settings()
        key = self._settings.stripe_secret_key.get_secret_value()
        self._client = client or httpx.Client(
            base_url=API_BASE,
            timeout=self._settings.stripe_request_timeout_seconds,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/x-www-form-urlencoded",
                # Pinned: a Stripe API upgrade must be a deliberate change with
                # a test run behind it, never something that arrives one
                # morning because Stripe rolled a new default.
                "Stripe-Version": "2024-06-20",
            },
        )

    # -- helpers -------------------------------------------------------
    def _post(
        self, path: str, data: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            response = self._client.post(path, data=data, headers=headers)
        except httpx.HTTPError as error:
            raise PaymentError(
                f"could not reach the payment provider: {type(error).__name__}"
            ) from error
        return self._parse(response, path)

    @staticmethod
    def _parse(response: httpx.Response, path: str) -> dict[str, Any]:
        try:
            body = response.json()
        except json.JSONDecodeError as error:
            raise PaymentError(
                f"payment provider returned a non-JSON response to {path} "
                f"(HTTP {response.status_code})"
            ) from error

        if response.status_code >= 400:
            error_body = body.get("error", {}) if isinstance(body, dict) else {}
            code = str(error_body.get("code") or error_body.get("type") or "unknown")
            # The message is Stripe's own and safe to surface; the request
            # body is not echoed, because it carried the amount and the
            # customer reference.
            message = str(error_body.get("message") or "the payment provider refused the request")
            if code in {
                "payment_intent_unexpected_state",
                "charge_already_captured",
                "charge_already_refunded",
            }:
                raise PaymentStateError(f"{code}: {message}")
            raise PaymentError(f"{code}: {message}")

        if not isinstance(body, dict):  # pragma: no cover - Stripe returns objects
            raise PaymentError(f"payment provider returned {type(body).__name__} for {path}")
        return body

    @staticmethod
    def _intent(body: dict[str, Any]) -> PaymentIntent:
        return PaymentIntent(
            id=str(body["id"]),
            amount_cents=int(body["amount"]),
            currency=str(body["currency"]),
            status=str(body["status"]),
            client_secret=body.get("client_secret"),
        )

    # -- interface -----------------------------------------------------
    def authorize(
        self,
        *,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        description: str,
        customer_ref: str | None = None,
    ) -> PaymentIntent:
        """`capture_method=manual` is what makes this an authorization.

        Without it Stripe charges immediately and the two-phase design is
        gone — so it is not a tunable.
        """
        payload: dict[str, Any] = {
            "amount": amount_cents,
            "currency": currency.lower(),
            "capture_method": "manual",
            "description": description,
            "automatic_payment_methods[enabled]": "true",
        }
        if customer_ref:
            payload["customer"] = customer_ref
        return self._intent(
            self._post("/payment_intents", payload, idempotency_key=idempotency_key)
        )

    def capture(self, intent_id: str) -> PaymentIntent:
        return self._intent(self._post(f"/payment_intents/{intent_id}/capture", {}))

    def void(self, intent_id: str) -> PaymentIntent:
        return self._intent(self._post(f"/payment_intents/{intent_id}/cancel", {}))

    def refund(self, *, intent_id: str, amount_cents: int, reason: str) -> ProviderRefund:
        """Stripe accepts only three reason strings; ours are richer.

        The RaceOS reason (``race_cancelled``, ``bundle_error``) is stored on
        our own ``refunds`` row and sent to Stripe as metadata. Squeezing it
        into Stripe's enum would lose the distinction that makes the refunds
        workspace useful.
        """
        body = self._post(
            "/refunds",
            {
                "payment_intent": intent_id,
                "amount": amount_cents,
                "metadata[raceos_reason]": reason,
            },
        )
        return ProviderRefund(
            id=str(body["id"]),
            intent_id=intent_id,
            amount_cents=int(body["amount"]),
            status=str(body["status"]),
        )

    def verify_webhook(self, *, payload: bytes, signature_header: str) -> WebhookEvent:
        verify_signature(
            payload=payload,
            signature_header=signature_header,
            secret=self._settings.stripe_webhook_secret.get_secret_value(),
        )
        body = json.loads(payload.decode("utf-8"))
        return WebhookEvent(
            id=str(body.get("id", "")),
            type=str(body.get("type", "")),
            data=dict(body.get("data", {})),
        )

    def health(self) -> dict[str, Any]:
        """A cheap authenticated read, so a bad key surfaces at ``/readyz``."""
        response = self._client.get("/balance")
        if response.status_code >= 400:
            raise PaymentError(
                f"payment provider health check failed with HTTP {response.status_code}"
            )
        return {"gateway": "stripe", "reachable": True}

    def close(self) -> None:
        self._client.close()
