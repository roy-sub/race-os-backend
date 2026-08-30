"""Identity, sessions, athlete constraints and their history.

The one structurally important thing in this module is what is **absent**:
there is no column, anywhere, by which a coach could write an athlete's
constraint. ``constraints.user_id`` is the owning athlete and the service
layer's write path takes the actor explicitly; there is no
``updated_by_coach_id``, no ``perm_constraints`` on the coach link, and no
admin override column. That is the first of the three structural guarantees,
and it is enforced by the shape of the schema rather than by a check that
could later be loosened.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from raceos.db.base import CreatedOnly, Entity, pg_enum
from raceos.domain.enums import (
    AccountState,
    AdminRole,
    AthleteLevel,
    BikePosition,
    ConstraintSource,
    Currency,
    HelmetType,
    UnitSystem,
    UserTier,
)


class User(Entity):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    #: Null is legitimate only for a user created before setting a password.
    #: There is no OAuth path in this build, so in practice it is always set.
    password_hash: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    country: Mapped[str | None] = mapped_column(String(2))
    emergency_contact_name: Mapped[str | None] = mapped_column(Text)
    emergency_contact_phone: Mapped[str | None] = mapped_column(Text)

    units: Mapped[UnitSystem] = mapped_column(
        pg_enum(UnitSystem, "unit_system"),
        nullable=False,
        default=UnitSystem.METRIC,
        server_default=text("'metric'"),
    )
    level: Mapped[AthleteLevel] = mapped_column(
        pg_enum(AthleteLevel, "athlete_level"),
        nullable=False,
        default=AthleteLevel.FIRST,
        server_default=text("'first'"),
    )
    avatar_url: Mapped[str | None] = mapped_column(Text)

    # Equipment facts, not measured physiology, so they live here rather than
    # on `constraints`: they have no source, no staleness window and no
    # calibration path (SOLVER_MODEL.md §F.2).
    bike_position: Mapped[BikePosition | None] = mapped_column(
        pg_enum(BikePosition, "bike_position_enum")
    )
    bike_helmet: Mapped[HelmetType | None] = mapped_column(pg_enum(HelmetType, "bike_helmet_enum"))

    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tier: Mapped[UserTier] = mapped_column(
        pg_enum(UserTier, "user_tier"),
        nullable=False,
        default=UserTier.FREE,
        server_default=text("'free'"),
    )
    is_coach: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    account_state: Mapped[AccountState] = mapped_column(
        pg_enum(AccountState, "account_state"),
        nullable=False,
        default=AccountState.ACTIVE,
        server_default=text("'active'"),
    )
    currency: Mapped[Currency] = mapped_column(
        pg_enum(Currency, "currency"),
        nullable=False,
        default=Currency.GBP,
        server_default=text("'GBP'"),
    )

    #: Bumped on password reset. Every token verification checks that its
    #: `issued_at` is later than this, which is what makes global revocation
    #: possible without tracking every issued token.
    sessions_invalidated_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    constraints: Mapped[list[Constraint]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_users_tier", "tier"),
        Index("ix_users_account_state", "account_state"),
        Index("ix_users_last_seen_at", "last_seen_at", postgresql_using="btree"),
        CheckConstraint("failed_login_count >= 0", name="users_failed_login_count_non_negative"),
    )


class Session(Entity):
    """A refresh-token session. Tokens are stored hashed, never in the clear."""

    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    refresh_token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(Text)
    #: Hashed, never raw: an IP address is PII (Build Spec Part 16.4).
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    #: Set when this session's token is rotated, so a replayed old token can
    #: be recognised as reuse rather than merely as expiry.
    rotated_to_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )


class Constraint(Entity):
    """One row per athlete per key: the current value only.

    History lives in :class:`ConstraintHistory`, which is append-only.
    """

    __tablename__ = "constraints"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float] = mapped_column(Numeric, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[ConstraintSource] = mapped_column(
        pg_enum(ConstraintSource, "constraint_source"), nullable=False
    )
    #: Where a `measured` value came from: an uploaded filename, a calibrating
    #: race id, or an estimator version. All the traceability the product
    #: needs now that there are no device integrations (Part 0.4 C1).
    source_detail: Mapped[str | None] = mapped_column(Text)
    confidence_pct: Mapped[int | None] = mapped_column(Integer)
    evidence_note: Mapped[str | None] = mapped_column(Text)
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: SOLVER_MODEL.md §F.4. Dry-bulb air temperature at which this value was
    #: measured; currently meaningful for `sweat_rate` only, nullable for all
    #: keys. Absent, the solver assumes 15 °C WBGT and says so in
    #: `assumed_fields` rather than defaulting silently.
    measured_at_temp_c: Mapped[float | None] = mapped_column(Numeric)

    user: Mapped[User] = relationship(back_populates="constraints")

    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_constraints_user_id_key"),
        CheckConstraint(
            "confidence_pct IS NULL OR (confidence_pct BETWEEN 0 AND 100)",
            name="constraints_confidence_pct_range",
        ),
        Index("ix_constraints_user_id", "user_id"),
    )


class ConstraintHistory(CreatedOnly):
    """Append-only mirror of :class:`Constraint`. Never updated, never deleted.

    Written on every value or source change. This is what makes "constraints
    six months stale" comparisons and post-race calibration deltas possible,
    and what lets an athlete see exactly what a calibration changed.
    """

    __tablename__ = "constraint_history"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float] = mapped_column(Numeric, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[ConstraintSource] = mapped_column(
        pg_enum(ConstraintSource, "constraint_source"), nullable=False
    )
    source_detail: Mapped[str | None] = mapped_column(Text)
    confidence_pct: Mapped[int | None] = mapped_column(Integer)
    evidence_note: Mapped[str | None] = mapped_column(Text)
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    measured_at_temp_c: Mapped[float | None] = mapped_column(Numeric)

    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    change_reason: Mapped[str | None] = mapped_column(Text)
    #: The actor who caused the change. A coach can never appear here for a
    #: constraint write, because no such write path exists.
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (Index("ix_constraint_history_user_id_key", "user_id", "key"),)


class PasswordResetToken(CreatedOnly):
    """Single-use, time-bounded, stored hashed.

    With email disabled in V1 the link cannot be delivered, so it is written
    to structured logs and kept here for an admin-only endpoint to hand over
    manually. It is never returned in the public response — that would turn
    "forgot password" into an account-takeover primitive.
    """

    __tablename__ = "password_reset_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The full reset link, retained only so support can read it out while
    #: EMAIL_ENABLED is false. Exposed by an admin-only endpoint, never by a
    #: public one.
    delivery_link: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_password_reset_tokens_user_id", "user_id"),)


class EmailVerificationToken(CreatedOnly):
    """The verification flow stays implemented even though the gate is off.

    ``REQUIRE_EMAIL_VERIFICATION`` is false in V1, so accounts are created
    already verified. Keeping this table and its flow means flipping the flag
    later needs no code change.
    """

    __tablename__ = "email_verification_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_link: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_email_verification_tokens_user_id", "user_id"),)


class AdminRoleAssignment(Entity):
    """RBAC by role, not a boolean (Build Spec Part 6.9).

    Support cannot see bundle publish controls or the refunds workspace, and
    that is expressed by not holding the role.
    """

    __tablename__ = "admin_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[AdminRole] = mapped_column(pg_enum(AdminRole, "admin_role"), nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "role", name="uq_admin_roles_user_id_role"),)
