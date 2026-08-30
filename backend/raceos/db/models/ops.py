"""Notifications, crowd verification, admin, ops, and the V1 infrastructure
tables that replace Redis.

Five tables here exist because V1 has no Redis and no Celery. Each replaces a
component the original specification assumed, and each is noted as such:
:class:`RateLimitCounter`, :class:`CacheEntry`, :class:`IdempotencyKey`,
:class:`JobRun` and :class:`EmailMessage`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from raceos.db.base import CreatedOnly, Entity, Json, JsonArray, JsonObject, pg_enum
from raceos.domain.enums import (
    AdminRole,
    CrowdCategory,
    CrowdConfidence,
    CrowdStatus,
    DriftSensitivity,
    IncidentSeverity,
    NotificationSeverity,
    NotificationType,
    ServiceStatus,
)

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class NotificationPreference(Entity):
    """Per-type channel matrix.

    Critical types (``drift``, ``cutoff``) cannot be fully disabled: in-app is
    the floor. The user chooses the channel; they do not choose whether a
    cut-off warning exists. Enforced server-side, not by the UI.
    """

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type_key: Mapped[NotificationType] = mapped_column(
        pg_enum(NotificationType, "notification_type"), nullable=False
    )
    channel_email: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    channel_push: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    channel_inapp: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    drift_sensitivity: Mapped[DriftSensitivity] = mapped_column(
        pg_enum(DriftSensitivity, "drift_sensitivity"),
        nullable=False,
        default=DriftSensitivity.BALANCED,
        server_default=text("'balanced'"),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "type_key", name="uq_notification_preferences_user_type"),
    )


class Notification(Entity):
    """The in-app inbox: a real paginated queryable resource, not a toast.

    This is the live delivery channel in V1, because email is a no-op and push
    is off. Critical types still land here.
    """

    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type_key: Mapped[NotificationType] = mapped_column(
        pg_enum(NotificationType, "notification_type"), nullable=False
    )
    tag: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[NotificationSeverity] = mapped_column(
        pg_enum(NotificationSeverity, "notification_severity"), nullable=False
    )
    race_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("races.id", ondelete="CASCADE")
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: `[{k, from, to}]` — the structured deltas the body was rendered from.
    #: Kept so the phrasing boundary stays auditable: the numbers are here,
    #: the prose is derived.
    deltas: Mapped[JsonArray] = mapped_column(Json, nullable=False, server_default=text("'[]'"))
    cta_label: Mapped[str | None] = mapped_column(Text)
    cta_href: Mapped[str | None] = mapped_column(Text)
    read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    __table_args__ = (
        Index("ix_notifications_user_id_created_at", "user_id", text("created_at DESC")),
        Index(
            "ix_notifications_user_id_unread",
            "user_id",
            postgresql_where=text("read = false"),
        ),
    )


class PushSubscription(Entity):
    """Web-push endpoints. Built, disabled by ``PUSH_ENABLED`` in V1."""

    __tablename__ = "push_subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    p256dh_key: Mapped[str] = mapped_column(Text, nullable=False)
    auth_key: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    __table_args__ = (Index("ix_push_subscriptions_user_id", "user_id"),)


class EmailMessage(CreatedOnly):
    """Every rendered message, whether or not a transport sent it.

    **Replaces nothing in the original spec — it exists because V1's transport
    is a no-op.** ``LoggingEmailSender`` writes the fully rendered message to
    structured logs and to this table, so the email subsystem is exercised end
    to end without a provider account, and support can read out a reset link
    that could not be delivered.
    """

    __tablename__ = "email_messages"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    to_address: Mapped[str] = mapped_column(Text, nullable=False)
    from_address: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    template_key: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(Text, nullable=False)
    #: False whenever EMAIL_ENABLED is off, which is the V1 state. The row
    #: still exists, so nothing about the flow is untested.
    delivered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    delivery_error: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_email_messages_user_id", "user_id"),
        Index("ix_email_messages_template_key", "template_key"),
    )


# ---------------------------------------------------------------------------
# Crowd verification
# ---------------------------------------------------------------------------


class CrowdReport(Entity):
    __tablename__ = "crowd_reports"

    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[CrowdCategory] = mapped_column(
        pg_enum(CrowdCategory, "crowd_category"), nullable=False
    )
    upload_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: Computed across *independent* uploads, never a raw count.
    agreement_weight_pct: Mapped[float] = mapped_column(
        Numeric, nullable=False, default=0, server_default=text("0")
    )
    confidence: Mapped[CrowdConfidence] = mapped_column(
        pg_enum(CrowdConfidence, "crowd_confidence"),
        nullable=False,
        default=CrowdConfidence.LOW,
        server_default=text("'low'"),
    )
    affected_plans_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[CrowdStatus] = mapped_column(
        pg_enum(CrowdStatus, "crowd_status"),
        nullable=False,
        default=CrowdStatus.PENDING,
        server_default=text("'pending'"),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_crowd_reports_course_id_status", "course_id", "status"),
        CheckConstraint(
            "agreement_weight_pct BETWEEN 0 AND 100", name="crowd_reports_agreement_pct_range"
        ),
    )


class CrowdReportUpload(CreatedOnly):
    """One athlete's contribution. Agreement is computed across these."""

    __tablename__ = "crowd_report_uploads"

    crowd_report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crowd_reports.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    payload: Mapped[JsonObject] = mapped_column(Json, nullable=False, server_default=text("'{}'"))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # One contribution per athlete per report, or "independent uploads"
        # would count the same person twice and inflate agreement.
        UniqueConstraint("crowd_report_id", "user_id", name="uq_crowd_report_uploads_report_user"),
    )


# ---------------------------------------------------------------------------
# Ops and admin
# ---------------------------------------------------------------------------


class Incident(Entity):
    __tablename__ = "incidents"

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    severity: Mapped[IncidentSeverity] = mapped_column(
        pg_enum(IncidentSeverity, "incident_severity"), nullable=False
    )
    what: Mapped[str] = mapped_column(Text, nullable=False)
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    service_ref: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_incidents_occurred_at", text("occurred_at DESC")),)


class ServiceHealth(Entity):
    """One row per logical service, written by health checks, never by hand."""

    __tablename__ = "service_health"

    service_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[ServiceStatus] = mapped_column(
        pg_enum(ServiceStatus, "service_status"),
        nullable=False,
        default=ServiceStatus.NOMINAL,
        server_default=text("'nominal'"),
    )
    uptime_pct_30d: Mapped[float | None] = mapped_column(Numeric)
    note: Mapped[str | None] = mapped_column(Text)


class KpiSnapshot(Entity):
    """One row per day. Solver percentiles are real measurements.

    Populated by the nightly aggregation from :class:`~raceos.db.models.plan.SolveTiming`
    rows, never from display constants (Build Spec Part 5.4).
    """

    __tablename__ = "kpi_snapshots"

    date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    plans_solved: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    free_to_paid_pct: Mapped[float | None] = mapped_column(Numeric)
    solver_p50_ms: Mapped[int | None] = mapped_column(Integer)
    solver_p95_ms: Mapped[int | None] = mapped_column(Integer)
    solver_p99_ms: Mapped[int | None] = mapped_column(Integer)
    support_ticket_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    total_accounts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    paying_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    season_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    coach_seat_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class FunnelEvent(CreatedOnly):
    """Event-level funnel, so conversion is segmentable.

    Specifically: whether the user was looking at a cut-off warning when they
    converted. That question cannot be answered from aggregate counts.
    """

    __tablename__ = "funnel_events"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL")
    )
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="SET NULL")
    )
    context: Mapped[JsonObject] = mapped_column(Json, nullable=False, server_default=text("'{}'"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_funnel_events_event_type_occurred_at", "event_type", "occurred_at"),
    )


class SupportAccessGrant(Entity):
    """The third structural guarantee.

    Support sees nothing until the **athlete** approves, for one hour,
    non-renewable without fresh approval. Every access under a grant is
    appended to ``accessed_log``, and that log is visible to the athlete —
    transparency, not merely internal audit. Expiry is enforced server-side on
    every read, plus a sweeper job.
    """

    __tablename__ = "support_access_grants"

    athlete_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    support_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope: Mapped[JsonObject] = mapped_column(Json, nullable=False, server_default=text("'{}'"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    denied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: `[{at, what, by}]`, append-only in practice and athlete-visible.
    accessed_log: Mapped[JsonArray] = mapped_column(
        Json, nullable=False, server_default=text("'[]'")
    )

    __table_args__ = (
        Index("ix_support_access_grants_athlete_id", "athlete_id"),
        Index("ix_support_access_grants_expires_at", "expires_at"),
        CheckConstraint(
            "granted_at IS NULL OR expires_at IS NOT NULL",
            name="support_access_grants_granted_has_expiry",
        ),
    )


class AuditLog(CreatedOnly):
    """Actor, timestamp, before and after. Written for the acts that matter.

    Bundle publish, refund, GDPR erasure, support-grant access, tier change,
    coach permission change.
    """

    __tablename__ = "audit_log"

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    before: Mapped[JsonObject] = mapped_column(Json, nullable=False, server_default=text("'{}'"))
    after: Mapped[JsonObject] = mapped_column(Json, nullable=False, server_default=text("'{}'"))
    request_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_actor_user_id", "actor_user_id"),
    )


class AdminRoleAudit(CreatedOnly):
    """Every grant or removal of an admin role, permanently."""

    __tablename__ = "admin_role_audit"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[AdminRole] = mapped_column(pg_enum(AdminRole, "admin_role"), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


# ---------------------------------------------------------------------------
# V1 infrastructure: what replaces Redis and Celery
# ---------------------------------------------------------------------------


class IdempotencyKey(Entity):
    """Replaces Redis for idempotent POSTs.

    A duplicate submission returns the first recorded response rather than
    repeating the effect. Already in the original schema, so this is not a V1
    addition — only its backing store is.
    """

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_body: Mapped[JsonObject] = mapped_column(
        Json, nullable=False, server_default=text("'{}'")
    )
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_idempotency_keys_expires_at", "expires_at"),)


class RateLimitCounter(Entity):
    """Replaces Redis for rate limiting.

    **Per-instance-safe because exactly one web instance runs.** Counters are
    rows in a shared database, so they are in fact correct across instances
    too; the note matters in the other direction — the atomic upsert this uses
    is the reason it stays correct if a second instance is ever added, whereas
    an in-process counter would not. Recorded in ``docs/DECISIONS.md``.
    """

    __tablename__ = "rate_limit_counters"

    #: Identity being limited: an account id, or a hashed IP for anonymous
    #: endpoints. Never a raw IP.
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    bucket: Mapped[str] = mapped_column(Text, nullable=False)
    #: Start of the fixed window this row counts.
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    __table_args__ = (
        UniqueConstraint(
            "subject", "bucket", "window_start", name="uq_rate_limit_counters_subject_bucket_window"
        ),
        Index("ix_rate_limit_counters_window_start", "window_start"),
    )


class CacheEntry(Entity):
    """Replaces Redis for forecast and bundle caching.

    A TTL column plus a small in-process cache in front. Values are bytes so
    a packed course bundle can be cached as delivered rather than re-encoded.
    """

    __tablename__ = "cache_entries"

    cache_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_type: Mapped[str] = mapped_column(
        Text, nullable=False, default="application/json", server_default=text("'application/json'")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_cache_entries_namespace", "namespace"),
        Index("ix_cache_entries_expires_at", "expires_at"),
    )


class JobRun(CreatedOnly):
    """Replaces Celery's result backend and Beat's history.

    Every scheduled job is a service method exposed at
    ``/internal/jobs/{name}`` and called by an external cron. This table is how
    a run is observable afterwards: what ran, when, how long, what it touched,
    and what failed. It is also what makes a job idempotent in practice — a
    run can look up what the previous one covered.
    """

    __tablename__ = "job_runs"

    job_name: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    succeeded: Mapped[bool | None] = mapped_column(Boolean)
    items_processed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: Job-specific counters and findings — for the media audit, the list of
    #: assets that did not resolve.
    result: Mapped[JsonObject] = mapped_column(Json, nullable=False, server_default=text("'{}'"))
    error: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_job_runs_job_name_started_at", "job_name", text("started_at DESC")),
    )
