"""Checkout, entitlement and invoice payloads."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from raceos.domain.enums import (
    Currency,
    PurchaseStatus,
    RefundReason,
    SubscriptionStatus,
    UserTier,
)


class AuthorizeRequest(BaseModel):
    plan_id: UUID
    currency: Currency = Currency.GBP


class PurchaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    plan_id: UUID | None
    amount_cents: int
    currency: Currency
    status: PurchaseStatus
    authorized_at: datetime | None = None
    captured_at: datetime | None = None
    voided_at: datetime | None = None


class AuthorizeResponse(BaseModel):
    """The hold, plus what the client needs to confirm a payment method.

    ``client_secret`` is returned once, at authorization, and never stored or
    logged: it is a bearer credential for this one payment.
    """

    purchase: PurchaseOut
    client_secret: str | None = None
    amount_cents: int
    currency: Currency


class SubscribeRequest(BaseModel):
    """Which recurring tier to buy.

    No currency: a subscription is billed against a provider price id, and
    that id already fixes the currency. Accepting one here would let a client
    ask for euros and be charged in pounds, with the response quoting the
    figure it asked for.
    """

    tier: UserTier


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tier: UserTier
    status: SubscriptionStatus
    #: When the next payment is due. Also the day a scheduled cancellation
    #: takes effect, which is why one field serves both readings.
    renews_at: datetime | None = None
    #: Set once a cancellation is scheduled. The athlete keeps everything they
    #: are paying for until this date — cancelling does not take back the
    #: period they already bought.
    cancel_at: datetime | None = None


class SubscribeResponse(BaseModel):
    """The agreement, plus what the client needs to confirm a payment method.

    ``client_secret`` is returned once, never stored and never logged. It is
    absent when the provider needed no confirmation — a returning customer
    with a card on file — and that absence is a success, not a failure.
    """

    subscription: SubscriptionOut
    client_secret: str | None = None


class PriceOut(BaseModel):
    tier: UserTier
    currency: Currency
    amount_cents: int


class EntitlementOut(BaseModel):
    action: str
    allowed: bool
    reason: str = ""
    required_tiers: list[UserTier] = Field(default_factory=list)
    purchasable_per_race: bool = False


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    invoice_number: str
    description: str
    amount_cents: int
    currency: Currency
    issued_at: datetime
    pdf_url: str | None = None


class RefundRequest(BaseModel):
    reason: RefundReason
    #: Omit for a full refund of what is still refundable.
    amount_cents: int | None = Field(default=None, ge=1)
    note: str | None = Field(default=None, max_length=1000)


class RefundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    invoice_id: UUID
    reason: RefundReason
    amount_cents: int
    note: str | None = None
