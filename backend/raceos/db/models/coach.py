"""Coach links, notes, and share links.

**There is no ``perm_constraints`` column, and there must never be one.** The
three permissions a coach can hold are ``plans``, ``build`` and ``analysis``.
Constraints are not a permission that happens to be off; there is no column
that could be flipped true, and no endpoint, admin tool or bulk script that
writes an athlete's constraint on a coach's behalf. That is the first
structural guarantee, expressed as an absence.

Share links carry the second: **no scope exposes a constraint value or account
data, including ``full_plan``.** The scope shapes which *plan* content is
returned, never whether the athlete's body is included — it never is.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from raceos.db.base import CreatedOnly, Entity, pg_enum
from raceos.domain.enums import CoachLinkStatus, ShareScope


class CoachAthleteLink(Entity):
    """Invite, then the athlete accepts. No coach-initiated silent linking."""

    __tablename__ = "coach_athlete_links"

    coach_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    athlete_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[CoachLinkStatus] = mapped_column(
        pg_enum(CoachLinkStatus, "coach_link_status"),
        nullable=False,
        default=CoachLinkStatus.PENDING,
        server_default=text("'pending'"),
    )

    # The three permissions. Each toggles independently, revokes immediately,
    # and is checked live per request rather than cached at issuance.
    perm_plans: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    perm_build: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    perm_analysis: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # There is deliberately no `perm_constraints`. See the module docstring.

    invited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invite_token_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Retained while email delivery is a no-op so support can hand the invite
    #: over manually. Admin-only, never in a public response.
    invite_delivery_link: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("coach_id", "athlete_id", name="uq_coach_athlete_links_coach_athlete"),
        CheckConstraint("coach_id <> athlete_id", name="coach_athlete_links_not_self"),
        Index("ix_coach_athlete_links_coach_id_status", "coach_id", "status"),
        Index("ix_coach_athlete_links_athlete_id_status", "athlete_id", "status"),
    )


class CoachNote(Entity):
    """Coach-authored, athlete-visible. Sanitized on write against stored XSS."""

    __tablename__ = "coach_notes"

    coach_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    athlete_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_coach_notes_athlete_id", "athlete_id"),)


class ShareLink(Entity):
    """A real token scheme. The frontend mock's six-character gate is cosmetic.

    Only the hash is stored; the raw token is returned once at creation. Every
    resolve re-checks revocation and expiry, so revoking works immediately —
    including on a page that is already open.
    """

    __tablename__ = "share_links"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    #: A short non-secret prefix, so a link can be identified in a list
    #: without storing anything that could reconstruct the token.
    token_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    scope: Mapped[ShareScope] = mapped_column(pg_enum(ShareScope, "share_scope"), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    recipient_label: Mapped[str | None] = mapped_column(Text)
    #: NOT NULL by design: a non-expiring share link cannot be created.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Optional second factor only, rate-limited against brute force. Never
    #: the sole gate.
    access_code_hash: Mapped[str | None] = mapped_column(Text)
    opens_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    opens: Mapped[list[ShareLinkOpen]] = relationship(
        back_populates="share_link", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_share_links_plan_id", "plan_id"),
        Index("ix_share_links_token_prefix", "token_prefix"),
        CheckConstraint("opens_count >= 0", name="share_links_opens_count_non_negative"),
    )


class ShareLinkOpen(CreatedOnly):
    __tablename__ = "share_link_opens"

    share_link_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("share_links.id", ondelete="CASCADE"), nullable=False
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Hashed: an IP address is PII.
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)

    share_link: Mapped[ShareLink] = relationship(back_populates="opens")

    __table_args__ = (Index("ix_share_link_opens_share_link_id", "share_link_id"),)
