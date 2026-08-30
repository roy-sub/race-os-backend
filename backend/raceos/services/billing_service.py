"""Checkout, entitlements, invoices and refunds.

**Two-phase capture.** ``POST /checkout/authorize`` places a hold and charges
nothing. The solve runs. Only a *successful* solve captures; a solver failure
voids the hold. An athlete is therefore never charged for a plan they did not
get, and that is enforced here rather than promised in copy — the capture call
sits on the success path and nowhere else.

**Entitlements are scoped to actions.** The decision itself is pure and lives
in :mod:`raceos.domain.entitlements`; this module gathers the facts it needs
from the database and turns a refusal into a 402 carrying the upgrade path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raceos.api.errors import Conflict, InvalidInput, NotFound, PaymentRequired
from raceos.config import Settings
from raceos.db.models import (
    Invoice,
    Plan,
    PriceCatalogEntry,
    Purchase,
    Refund,
    Subscription,
    User,
)
from raceos.domain.entitlements import (
    Decision,
    EntitlementAction,
    EntitlementContext,
    decide,
)
from raceos.domain.enums import (
    Currency,
    PurchaseStatus,
    RefundReason,
    SubscriptionStatus,
    UserTier,
)
from raceos.logging import get_logger
from raceos.payments import PaymentError, get_payment_gateway
from raceos.payments.base import PaymentStateError

logger = get_logger(__name__)

#: The published price list, in minor units, transcribed from the pricing page
#: (`lib/pricing.ts`). Seeded into ``price_catalog``; Stripe remains the source
#: of truth for what is actually charged, and a divergence between the two is a
#: sync problem to surface, never a silent adjustment to what the athlete pays.
PUBLISHED_PRICES: dict[UserTier, dict[Currency, int]] = {
    UserTier.PER_RACE: {Currency.GBP: 1500, Currency.USD: 1900, Currency.EUR: 1800},
    UserTier.SEASON: {Currency.GBP: 4700, Currency.USD: 5900, Currency.EUR: 5500},
    UserTier.COACH: {Currency.GBP: 7900, Currency.USD: 9900, Currency.EUR: 9200},
}


# ---------------------------------------------------------------------------
# Entitlements
# ---------------------------------------------------------------------------


def _active_subscription(session: Session, user_id: UUID) -> Subscription | None:
    return session.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
        .order_by(Subscription.created_at.desc())
    )


def has_captured_purchase_for_race(session: Session, *, user_id: UUID, race_id: UUID) -> bool:
    """Whether this athlete has paid, and been charged, for this race.

    Joined through ``plans`` rather than stored on the purchase, because a
    race can carry many plan versions and the entitlement belongs to the race.
    A purchase against version 1 must still cover version 4 after three
    re-solves, which the athlete never paid extra for.
    """
    count = session.scalar(
        select(func.count())
        .select_from(Purchase)
        .join(Plan, Plan.id == Purchase.plan_id)
        .where(
            Purchase.user_id == user_id,
            Purchase.status == PurchaseStatus.CAPTURED,
            Plan.race_id == race_id,
        )
    )
    return bool(count)


def has_open_authorization_for_race(session: Session, *, user_id: UUID, race_id: UUID) -> bool:
    """Whether a hold is currently placed against any plan for this race.

    This is what makes two-phase capture work: at the moment a first solve is
    authorised there is by definition nothing captured yet, because the
    capture is what the solve's success triggers.
    """
    count = session.scalar(
        select(func.count())
        .select_from(Purchase)
        .join(Plan, Plan.id == Purchase.plan_id)
        .where(
            Purchase.user_id == user_id,
            Purchase.status == PurchaseStatus.AUTHORIZED,
            Plan.race_id == race_id,
        )
    )
    return bool(count)


def context_for(session: Session, *, user: User, race_id: UUID | None = None) -> EntitlementContext:
    subscription = _active_subscription(session, user.id)
    return EntitlementContext(
        tier=user.tier,
        subscription_active=subscription is not None,
        has_race_purchase=(
            has_captured_purchase_for_race(session, user_id=user.id, race_id=race_id)
            if race_id is not None
            else False
        ),
        has_open_authorization=(
            has_open_authorization_for_race(session, user_id=user.id, race_id=race_id)
            if race_id is not None
            else False
        ),
    )


def check(
    session: Session, *, user: User, action: EntitlementAction, race_id: UUID | None = None
) -> Decision:
    return decide(action, context_for(session, user=user, race_id=race_id))


def require(
    session: Session, *, user: User, action: EntitlementAction, race_id: UUID | None = None
) -> None:
    """Raise 402 with the upgrade path when the action is not entitled."""
    decision = check(session, user=user, action=action, race_id=race_id)
    if decision.allowed:
        return
    raise PaymentRequired(
        decision.reason,
        details={
            "action": decision.action.value,
            "required_tiers": [tier.value for tier in decision.required_tiers],
            "purchasable_per_race": decision.purchasable_per_race,
        },
    )


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


def price_for(session: Session, *, tier: UserTier, currency: Currency) -> int:
    """The displayed price in minor units.

    Reads ``price_catalog`` first so an admin price change takes effect
    without a deploy, and falls back to the published list so a fresh database
    still quotes the right number instead of zero.
    """
    entry = session.scalar(
        select(PriceCatalogEntry).where(
            PriceCatalogEntry.tier == tier, PriceCatalogEntry.currency == currency
        )
    )
    if entry is not None:
        return entry.amount_cents
    published = PUBLISHED_PRICES.get(tier, {}).get(currency)
    if published is None:
        raise InvalidInput(f"No price is published for {tier.value} in {currency.value}.")
    return published


def price_catalog(session: Session) -> list[dict[str, Any]]:
    """Every tier and currency, for the pricing page."""
    rows: list[dict[str, Any]] = []
    for tier in (UserTier.PER_RACE, UserTier.SEASON, UserTier.COACH):
        for currency in Currency:
            rows.append(
                {
                    "tier": tier.value,
                    "currency": currency.value,
                    "amount_cents": price_for(session, tier=tier, currency=currency),
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Two-phase checkout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Authorization:
    purchase: Purchase
    #: Handed to the client so it can confirm the payment method. Never
    #: logged, and never stored.
    client_secret: str | None


def authorize(
    session: Session,
    *,
    user: User,
    plan: Plan,
    currency: Currency,
    idempotency_key: str,
    settings: Settings,
) -> Authorization:
    """Place a hold for one race plan. Charges nothing.

    A second authorize with the same idempotency key returns the first
    purchase rather than placing a second hold — enforced by a unique index on
    the column, so the guarantee is the database's rather than the caller's.
    """
    if plan.user_id != user.id:
        raise NotFound("Plan not found.")

    existing = session.scalar(select(Purchase).where(Purchase.idempotency_key == idempotency_key))
    if existing is not None:
        return Authorization(purchase=existing, client_secret=None)

    open_hold = session.scalar(
        select(Purchase).where(
            Purchase.plan_id == plan.id,
            Purchase.status.in_([PurchaseStatus.AUTHORIZED, PurchaseStatus.CAPTURED]),
        )
    )
    if open_hold is not None:
        raise Conflict(
            "This plan already has a payment in progress or completed. "
            "Re-solving an existing plan is free."
        )

    amount = price_for(session, tier=UserTier.PER_RACE, currency=currency)
    gateway = get_payment_gateway(settings)
    intent = gateway.authorize(
        amount_cents=amount,
        currency=currency.value,
        idempotency_key=idempotency_key,
        description=f"RaceOS race plan · plan {plan.id}",
        customer_ref=_customer_ref(session, user),
    )

    purchase = Purchase(
        user_id=user.id,
        plan_id=plan.id,
        payment_provider_intent_id=intent.id,
        amount_cents=intent.amount_cents,
        currency=currency,
        status=PurchaseStatus.AUTHORIZED,
        authorized_at=datetime.now(UTC),
        idempotency_key=idempotency_key,
    )
    session.add(purchase)
    session.flush()
    logger.info(
        "checkout.authorized",
        extra={"purchase_id": str(purchase.id), "plan_id": str(plan.id), "amount_cents": amount},
    )
    return Authorization(purchase=purchase, client_secret=intent.client_secret)


def _customer_ref(session: Session, user: User) -> str | None:
    subscription = session.scalar(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
    )
    return subscription.payment_provider_customer_id if subscription else None


def open_authorization(session: Session, *, plan: Plan) -> Purchase | None:
    """The hold waiting on this plan's solve, if there is one."""
    return session.scalar(
        select(Purchase).where(
            Purchase.plan_id == plan.id, Purchase.status == PurchaseStatus.AUTHORIZED
        )
    )


def capture_for_plan(session: Session, *, plan: Plan, settings: Settings) -> Purchase | None:
    """Take the money. **Only ever called on a successful solve.**

    Returns ``None`` when there is nothing to capture, which is the normal
    case for a re-solve, a season subscriber, or a plan solved on a coach's
    seat. A missing authorization is not an error: it means this solve was
    already paid for.
    """
    purchase = open_authorization(session, plan=plan)
    if purchase is None:
        return None

    gateway = get_payment_gateway(settings)
    try:
        gateway.capture(purchase.payment_provider_intent_id)
    except PaymentError:
        # The solve succeeded and the plan is saved. A capture failure is a
        # billing problem to chase, never a reason to withhold work the
        # athlete has already received.
        logger.exception("checkout.capture_failed", extra={"purchase_id": str(purchase.id)})
        raise

    purchase.status = PurchaseStatus.CAPTURED
    purchase.captured_at = datetime.now(UTC)
    session.flush()
    issue_invoice(session, purchase=purchase, description="RaceOS race plan")
    logger.info("checkout.captured", extra={"purchase_id": str(purchase.id)})
    return purchase


def void_for_plan(
    session: Session, *, plan: Plan, reason: str, settings: Settings
) -> Purchase | None:
    """Release the hold. Called when a solve fails or is abandoned."""
    purchase = open_authorization(session, plan=plan)
    if purchase is None:
        return None

    gateway = get_payment_gateway(settings)
    try:
        gateway.void(purchase.payment_provider_intent_id)
    except PaymentStateError:
        # Already captured or already cancelled at the provider. The local row
        # is what is out of date, so record what the provider says rather than
        # forcing a state it does not agree with.
        logger.warning(
            "checkout.void_rejected",
            extra={"purchase_id": str(purchase.id), "reason": reason},
        )
        raise

    purchase.status = PurchaseStatus.VOIDED
    purchase.voided_at = datetime.now(UTC)
    session.flush()
    logger.info("checkout.voided", extra={"purchase_id": str(purchase.id), "reason": reason})
    return purchase


# ---------------------------------------------------------------------------
# Invoices and refunds
# ---------------------------------------------------------------------------


def _invoice_number(session: Session, *, issued_at: datetime) -> str:
    """``RO-YYYY-NNNNNN``, sequential within the year.

    Derived from a count rather than a sequence so it stays readable and
    stable across environments; the unique index is what actually prevents a
    collision, and a collision retries with the next number.
    """
    year = issued_at.year
    used = session.scalar(
        select(func.count())
        .select_from(Invoice)
        .where(func.extract("year", Invoice.issued_at) == year)
    )
    return f"RO-{year}-{(used or 0) + 1:06d}"


def issue_invoice(session: Session, *, purchase: Purchase, description: str) -> Invoice:
    issued_at = datetime.now(UTC)
    invoice = Invoice(
        user_id=purchase.user_id,
        description=description,
        invoice_number=_invoice_number(session, issued_at=issued_at),
        amount_cents=purchase.amount_cents,
        currency=purchase.currency,
        issued_at=issued_at,
        payment_provider_invoice_id=purchase.payment_provider_intent_id,
    )
    session.add(invoice)
    session.flush()
    return invoice


def list_invoices(session: Session, *, user: User) -> list[Invoice]:
    return list(
        session.scalars(
            select(Invoice).where(Invoice.user_id == user.id).order_by(Invoice.issued_at.desc())
        )
    )


def refund_invoice(
    session: Session,
    *,
    invoice: Invoice,
    actor: User,
    reason: RefundReason,
    amount_cents: int | None,
    note: str | None,
    settings: Settings,
) -> Refund:
    """Refund against an invoice. Admin-actionable, fully audited.

    ``race_cancelled`` and ``bundle_error`` are supported operational paths,
    so the reason is an enum on our own row rather than squeezed into the
    provider's three-value vocabulary.
    """
    purchase = session.scalar(
        select(Purchase).where(
            Purchase.payment_provider_intent_id == invoice.payment_provider_invoice_id
        )
    )
    if purchase is None:
        raise NotFound("No captured payment backs this invoice.")
    if purchase.status is not PurchaseStatus.CAPTURED:
        raise Conflict(
            f"This payment is {purchase.status.value}; only a captured payment " f"can be refunded."
        )

    already = session.scalar(
        select(func.coalesce(func.sum(Refund.amount_cents), 0)).where(
            Refund.invoice_id == invoice.id
        )
    )
    remaining = invoice.amount_cents - int(already or 0)
    amount = amount_cents if amount_cents is not None else remaining
    if amount <= 0 or amount > remaining:
        raise InvalidInput(
            f"A refund of {amount} is outside the {remaining} still refundable "
            f"on this invoice.",
            field="amount_cents",
        )

    gateway = get_payment_gateway(settings)
    provider_refund = gateway.refund(
        intent_id=purchase.payment_provider_intent_id,
        amount_cents=amount,
        reason=reason.value,
    )

    refund = Refund(
        invoice_id=invoice.id,
        reason=reason,
        amount_cents=amount,
        actor_user_id=actor.id,
        note=note,
        provider_refund_id=provider_refund.id,
    )
    session.add(refund)
    if amount == remaining:
        purchase.status = PurchaseStatus.REFUNDED
    session.flush()
    logger.info(
        "billing.refunded",
        extra={
            "invoice_id": str(invoice.id),
            "amount_cents": amount,
            "reason": reason.value,
            "actor_user_id": str(actor.id),
        },
    )
    return refund


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

#: Provider events this integration acts on. Anything else is acknowledged and
#: ignored: returning a 400 for an event we simply do not use would make the
#: provider retry it forever.
HANDLED_EVENTS = frozenset(
    {
        "payment_intent.succeeded",
        "payment_intent.canceled",
        "payment_intent.payment_failed",
        "charge.refunded",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)


def apply_webhook(session: Session, *, event_type: str, data: dict[str, Any]) -> str:
    """Reconcile local state with what the provider says happened.

    The provider is authoritative about money. A capture that succeeded there
    but failed to persist here is exactly what this path repairs, which is why
    it is written to be safely repeatable: every branch checks the current
    local state before changing it.
    """
    if event_type not in HANDLED_EVENTS:
        return "ignored"

    obj = data.get("object", {})
    if not isinstance(obj, dict):
        return "ignored"

    if event_type.startswith("payment_intent."):
        purchase = session.scalar(
            select(Purchase).where(Purchase.payment_provider_intent_id == str(obj.get("id", "")))
        )
        if purchase is None:
            return "unknown_intent"
        if event_type == "payment_intent.succeeded":
            if purchase.status is not PurchaseStatus.CAPTURED:
                purchase.status = PurchaseStatus.CAPTURED
                purchase.captured_at = datetime.now(UTC)
                issue_invoice(session, purchase=purchase, description="RaceOS race plan")
            return "captured"
        if purchase.status is PurchaseStatus.AUTHORIZED:
            purchase.status = PurchaseStatus.VOIDED
            purchase.voided_at = datetime.now(UTC)
        return "voided"

    if event_type == "charge.refunded":
        purchase = session.scalar(
            select(Purchase).where(
                Purchase.payment_provider_intent_id == str(obj.get("payment_intent", ""))
            )
        )
        if purchase is None:
            return "unknown_intent"
        if obj.get("amount_refunded") == obj.get("amount"):
            purchase.status = PurchaseStatus.REFUNDED
        return "refunded"

    # Subscription lifecycle.
    subscription = session.scalar(
        select(Subscription).where(
            Subscription.payment_provider_subscription_id == str(obj.get("id", ""))
        )
    )
    if subscription is None:
        return "unknown_subscription"
    if event_type == "customer.subscription.deleted":
        subscription.status = SubscriptionStatus.CANCELLED
    else:
        provider_status = str(obj.get("status", ""))
        subscription.status = {
            "active": SubscriptionStatus.ACTIVE,
            "trialing": SubscriptionStatus.ACTIVE,
            "past_due": SubscriptionStatus.PAST_DUE,
            "unpaid": SubscriptionStatus.PAST_DUE,
            "canceled": SubscriptionStatus.CANCELLED,
        }.get(provider_status, subscription.status)
    session.flush()
    return "subscription_updated"


def new_idempotency_key() -> str:
    """For a client that did not send one. Never reused across requests."""
    return f"chk_{uuid.uuid4().hex}"
