"""Subscriptions, purchases, invoices and refunds.

Two-phase capture is the whole design: ``POST /checkout/authorize`` creates a
``purchases`` row as ``authorized`` and charges nothing; the solve runs; only a
*successful* solve fires the capture. A solver failure voids the authorization,
so the athlete is never charged for a plan they did not get.

Entitlements are scoped to **actions**, not to subscription status. On
cancellation, already-solved plans stay permanently accessible at full
function; only new solves, analysis and calibration lapse.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from raceos.db.base import CreatedOnly, Entity, pg_enum
from raceos.domain.enums import Currency, PurchaseStatus, RefundReason, SubscriptionStatus, UserTier


class Subscription(Entity):
    __tablename__ = "subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    tier: Mapped[UserTier] = mapped_column(pg_enum(UserTier, "user_tier"), nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(
        pg_enum(SubscriptionStatus, "subscription_status"), nullable=False
    )
    renews_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_provider_customer_id: Mapped[str | None] = mapped_column(Text)
    payment_provider_subscription_id: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (Index("ix_subscriptions_user_id_status", "user_id", "status"),)


class Purchase(Entity):
    """One authorization, and at most one capture or void."""

    __tablename__ = "purchases"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL")
    )
    payment_provider_intent_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[Currency] = mapped_column(pg_enum(Currency, "currency"), nullable=False)
    status: Mapped[PurchaseStatus] = mapped_column(
        pg_enum(PurchaseStatus, "purchase_status"), nullable=False
    )
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Duplicate submissions return the first result rather than charging
    #: twice. Unique, so the guarantee is the database's, not the caller's.
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    __table_args__ = (
        Index("ix_purchases_user_id", "user_id"),
        Index("ix_purchases_status", "status"),
        CheckConstraint("amount_cents >= 0", name="purchases_amount_non_negative"),
        # A captured purchase must record when. Without this a capture that
        # half-failed would be indistinguishable from a successful one.
        CheckConstraint(
            "status <> 'captured' OR captured_at IS NOT NULL",
            name="purchases_captured_has_timestamp",
        ),
    )


class Invoice(Entity):
    __tablename__ = "invoices"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    invoice_number: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[Currency] = mapped_column(pg_enum(Currency, "currency"), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payment_provider_invoice_id: Mapped[str | None] = mapped_column(Text)
    pdf_url: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_invoices_user_id_issued_at", "user_id", "issued_at"),)


class Refund(CreatedOnly):
    """Admin-actionable, with a reason enum and a full audit entry.

    ``race_cancelled`` and ``bundle_error`` are supported operational paths,
    not narrative promises.
    """

    __tablename__ = "refunds"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[RefundReason] = mapped_column(
        pg_enum(RefundReason, "refund_reason"), nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)
    provider_refund_id: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        Index("ix_refunds_invoice_id", "invoice_id"),
        CheckConstraint("amount_cents > 0", name="refunds_amount_positive"),
    )


class PriceCatalogEntry(Entity):
    """Prices mirrored from the provider for display.

    Stripe is the source of truth; this is what renders on the pricing page
    without a provider round-trip. A mismatch is a seed or sync problem, not a
    charging problem — the amount charged always comes from the provider.
    """

    __tablename__ = "price_catalog"

    tier: Mapped[UserTier] = mapped_column(pg_enum(UserTier, "user_tier"), nullable=False)
    currency: Mapped[Currency] = mapped_column(pg_enum(Currency, "currency"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_price_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("tier", "currency", name="uq_price_catalog_tier_currency"),
        CheckConstraint("amount_cents >= 0", name="price_catalog_amount_non_negative"),
    )
