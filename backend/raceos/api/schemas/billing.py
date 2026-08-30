"""Checkout, entitlement and invoice payloads."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from raceos.domain.enums import Currency, PurchaseStatus, RefundReason, UserTier


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
