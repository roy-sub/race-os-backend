"""Checkout, entitlements and invoices.

``POST /checkout/authorize`` places a hold and **charges nothing**. The
capture happens on the solve endpoint, on the success path only, so this
router cannot take money on its own — which is the structural version of the
promise that a failed solve is never billed.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, status

from raceos.api.deps import Config, CurrentUser, DbSession
from raceos.api.errors import InvalidInput, NotFound
from raceos.api.schemas.billing import (
    AuthorizeRequest,
    AuthorizeResponse,
    EntitlementOut,
    InvoiceOut,
    PriceOut,
    PurchaseOut,
)
from raceos.db.models import Invoice
from raceos.domain.entitlements import RULES, EntitlementAction
from raceos.domain.enums import Currency
from raceos.logging import get_logger
from raceos.payments import get_payment_gateway
from raceos.payments.base import WebhookVerificationError
from raceos.services import billing_service, plan_service

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["billing"])


@router.get("/prices", summary="The published price list")
def list_prices(session: DbSession) -> list[PriceOut]:
    """Public: the pricing page renders from this, so it needs no session."""
    return [PriceOut.model_validate(row) for row in billing_service.price_catalog(session)]


@router.get("/entitlements", summary="What this user may do")
def list_entitlements(
    session: DbSession,
    user: CurrentUser,
    race_id: Annotated[
        UUID | None, Query(description="Scope race-specific actions to this race")
    ] = None,
) -> list[EntitlementOut]:
    """Every gated action with a verdict, so the UI never guesses.

    Returning the whole matrix rather than one answer at a time means the
    client can render the correct state for a screen in one request, and a
    disabled button always has a reason attached to it.
    """
    out: list[EntitlementOut] = []
    for action in RULES:
        decision = billing_service.check(session, user=user, action=action, race_id=race_id)
        out.append(
            EntitlementOut(
                action=decision.action.value,
                allowed=decision.allowed,
                reason=decision.reason,
                required_tiers=list(decision.required_tiers),
                purchasable_per_race=decision.purchasable_per_race,
            )
        )
    return out


@router.post(
    "/checkout/authorize",
    status_code=status.HTTP_201_CREATED,
    summary="Place a hold for one race plan",
)
def authorize_checkout(
    payload: AuthorizeRequest,
    session: DbSession,
    user: CurrentUser,
    settings: Config,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AuthorizeResponse:
    """Charges nothing. The capture happens only if the solve succeeds."""
    plan = plan_service.get_plan(session, plan_id=payload.plan_id, user=user)
    key = idempotency_key or billing_service.new_idempotency_key()
    authorization = billing_service.authorize(
        session,
        user=user,
        plan=plan,
        currency=payload.currency,
        idempotency_key=key,
        settings=settings,
    )
    session.commit()
    return AuthorizeResponse(
        purchase=PurchaseOut.model_validate(authorization.purchase),
        client_secret=authorization.client_secret,
        amount_cents=authorization.purchase.amount_cents,
        currency=authorization.purchase.currency,
    )


@router.post("/checkout/void", summary="Release a hold the athlete abandoned")
def void_checkout(
    payload: AuthorizeRequest, session: DbSession, user: CurrentUser, settings: Config
) -> PurchaseOut:
    """For a builder the athlete closed without solving.

    Without it a hold would sit on their card until the provider expired it,
    which looks exactly like being charged.
    """
    plan = plan_service.get_plan(session, plan_id=payload.plan_id, user=user)
    purchase = billing_service.void_for_plan(
        session, plan=plan, reason="abandoned at checkout", settings=settings
    )
    if purchase is None:
        raise NotFound("There is no open authorization on this plan.")
    session.commit()
    return PurchaseOut.model_validate(purchase)


@router.get("/invoices", summary="This user's invoices")
def list_invoices(session: DbSession, user: CurrentUser) -> list[InvoiceOut]:
    return [
        InvoiceOut.model_validate(invoice)
        for invoice in billing_service.list_invoices(session, user=user)
    ]


@router.get("/invoices/{invoice_id}", summary="One invoice")
def get_invoice(invoice_id: UUID, session: DbSession, user: CurrentUser) -> InvoiceOut:
    invoice = session.get(Invoice, invoice_id)
    if invoice is None or invoice.user_id != user.id:
        raise NotFound("Invoice not found.")
    return InvoiceOut.model_validate(invoice)


# ---------------------------------------------------------------------------
# Provider webhook
# ---------------------------------------------------------------------------

webhook_router = APIRouter(prefix="/webhooks", tags=["billing"])


@webhook_router.post("/payments", summary="Payment provider callbacks")
async def payment_webhook(
    request: Request,
    session: DbSession,
    settings: Config,
    signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
) -> dict[str, str]:
    """Unauthenticated by design — **the signature is the authentication.**

    An unverified webhook is an attacker asserting that a payment succeeded,
    so a bad signature is a 422 and nothing is read from the body before the
    signature has verified against the raw bytes.
    """
    payload = await request.body()
    if not signature:
        raise InvalidInput("Missing signature header.", field="Stripe-Signature")

    gateway = get_payment_gateway(settings)
    try:
        event = gateway.verify_webhook(payload=payload, signature_header=signature)
    except WebhookVerificationError as error:
        # Logged without the body: an unverified payload is attacker-supplied.
        logger.warning("payments.webhook_rejected", extra={"reason": str(error)})
        raise InvalidInput("Webhook signature did not verify.") from error

    outcome = billing_service.apply_webhook(session, event_type=event.type, data=event.data)
    session.commit()
    logger.info("payments.webhook", extra={"event_type": event.type, "outcome": outcome})
    return {"received": "true", "outcome": outcome}


#: Re-exported so the currency query parameter has one definition.
__all__ = ["Currency", "EntitlementAction", "router", "webhook_router"]
