"""Plans and everything a solve produces.

**A solve never mutates a plan.** It inserts a new ``plans`` row with
``version = max(version) + 1`` plus all its child rows in one transaction, then
flips the previous row's status. Previous versions stay readable forever,
because post-race comparison must reference *the version that was live at race
time*, not the current one.

A partial write is impossible: the whole solve is one transaction, and a
unique partial index enforces at most one ``active`` version per race.
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
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from raceos.db.base import CreatedOnly, Entity, Json, JsonArray, JsonObject, pg_enum
from raceos.domain.enums import (
    BagKey,
    DriftCause,
    DriftSeverity,
    DriftStatus,
    Feasibility,
    Leg,
    MarginState,
    PlanStatus,
    SolveJobStatus,
)


class Plan(Entity):
    __tablename__ = "plans"

    race_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("races.id", ondelete="RESTRICT"), nullable=False
    )
    #: Denormalised for query convenience; always the athlete, never a coach.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[PlanStatus] = mapped_column(
        pg_enum(PlanStatus, "plan_status"),
        nullable=False,
        default=PlanStatus.DRAFT,
        server_default=text("'draft'"),
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    goal_minutes: Mapped[float | None] = mapped_column(Numeric)
    projected_minutes: Mapped[float | None] = mapped_column(Numeric)
    feasibility: Mapped[Feasibility] = mapped_column(
        pg_enum(Feasibility, "feasibility"),
        nullable=False,
        default=Feasibility.NOT_SOLVED,
        server_default=text("'NOT_SOLVED'"),
    )
    #: The minimum margin across all gates — the *tightest*. Keeps its
    #: contract meaning even though an infeasibility now reports the earliest
    #: missed barrier (§3.3): this is what drives margin_state and the drift
    #: thresholds, and redefining it would ripple into notification logic.
    worst_margin_minutes: Mapped[float | None] = mapped_column(Numeric)
    binding_constraint_key: Mapped[str | None] = mapped_column(Text)

    solved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: sha256 of the canonical SolveInput. Identical hash implies
    #: byte-identical output, which is what makes a re-solve skippable.
    solve_input_hash: Mapped[str | None] = mapped_column(Text)
    #: Frozen copies, never live joins, so a later constraint edit does not
    #: retroactively change a solved plan's displayed numbers.
    constraints_snapshot: Mapped[JsonObject] = mapped_column(
        Json, nullable=False, server_default=text("'{}'")
    )
    forecast_snapshot: Mapped[JsonObject] = mapped_column(
        Json, nullable=False, server_default=text("'{}'")
    )
    #: SOLVER_MODEL.md §F.6. Sorted dotted paths of optional inputs that were
    #: absent and for which the solver substituted a documented default.
    #: Persisted so the UI can mark affected numbers and back-testing can
    #: stratify plans that rested on an assumption.
    assumed_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )

    readiness_fraction: Mapped[float | None] = mapped_column(Numeric)
    readiness_note: Mapped[str | None] = mapped_column(Text)
    shared: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    race_card_pdf_url: Mapped[str | None] = mapped_column(Text)

    #: Set when a coach built this plan. It is not the athlete's plan until
    #: they call the approve endpoint, which sets `approved_at`.
    built_by_coach_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL")
    )

    segments: Mapped[list[PlanSegment]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )
    splits: Mapped[list[PlanSplit]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )
    gates: Mapped[list[PlanGate]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )
    bags: Mapped[list[PlanBag]] = relationship(back_populates="plan", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("race_id", "version", name="uq_plans_race_id_version"),
        # At most one active version per race. A partial unique index is the
        # only way to express this without forbidding many `past` versions.
        Index(
            "uq_plans_one_active_per_race",
            "race_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_plans_user_id_status", "user_id", "status"),
        Index("ix_plans_solve_input_hash", "solve_input_hash"),
        Index("ix_plans_race_id_version", "race_id", text("version DESC")),
        CheckConstraint("version >= 1", name="plans_version_positive"),
    )


class PlanSegment(Entity):
    """Bike and run pacing detail: one row per named segment."""

    __tablename__ = "plan_segments"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    leg: Mapped[Leg] = mapped_column(pg_enum(Leg, "leg"), nullable=False)
    from_km: Mapped[float] = mapped_column(Numeric, nullable=False)
    to_km: Mapped[float] = mapped_column(Numeric, nullable=False)
    terrain_desc: Mapped[str | None] = mapped_column(Text)
    target_watts: Mapped[int | None] = mapped_column(Integer)
    target_pace_sec_per_km: Mapped[int | None] = mapped_column(Integer)
    target_minutes: Mapped[float] = mapped_column(Numeric, nullable=False)
    hr_zone: Mapped[str | None] = mapped_column(Text)
    zone_width_pct: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)

    plan: Mapped[Plan] = relationship(back_populates="segments")

    __table_args__ = (
        UniqueConstraint("plan_id", "ordinal", name="uq_plan_segments_plan_id_ordinal"),
        # §4.3.3: never emit a negative or zero split time. Asserted as a
        # stage postcondition in the solver AND here, because a violation
        # that reached storage would be invisible until a user saw it.
        CheckConstraint("target_minutes > 0", name="plan_segments_target_minutes_positive"),
        CheckConstraint("to_km > from_km", name="plan_segments_km_ordered"),
        Index("ix_plan_segments_plan_id", "plan_id"),
    )


class PlanSplit(Entity):
    """Per-leg summary.

    Named ``split_minutes``, not ``split_time``: the frontend's ``"1:06"`` is
    a formatted display string, and BACKENDREQUIREMENTS §4 requires numerics
    in storage with formatting at the API boundary. See
    ``docs/FIELD_NAME_RECONCILIATION.md`` R-001.
    """

    __tablename__ = "plan_splits"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    leg: Mapped[Leg] = mapped_column(pg_enum(Leg, "leg"), nullable=False)
    distance: Mapped[float] = mapped_column(Numeric, nullable=False)
    target_pace_or_power: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    split_minutes: Mapped[float] = mapped_column(Numeric, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    plan: Mapped[Plan] = relationship(back_populates="splits")

    __table_args__ = (
        UniqueConstraint("plan_id", "leg", name="uq_plan_splits_plan_id_leg"),
        CheckConstraint("split_minutes > 0", name="plan_splits_split_minutes_positive"),
    )


class PlanGate(Entity):
    """Barrier margins as solved into this specific plan."""

    __tablename__ = "plan_gates"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    leg: Mapped[Leg] = mapped_column(pg_enum(Leg, "leg"), nullable=False)
    limit_minutes: Mapped[float] = mapped_column(Numeric, nullable=False)
    eta_minutes: Mapped[float] = mapped_column(Numeric, nullable=False)
    margin_minutes: Mapped[float] = mapped_column(Numeric, nullable=False)
    load_pct: Mapped[float] = mapped_column(Numeric, nullable=False)
    state: Mapped[MarginState] = mapped_column(pg_enum(MarginState, "margin_state"), nullable=False)

    plan: Mapped[Plan] = relationship(back_populates="gates")

    __table_args__ = (
        UniqueConstraint("plan_id", "name", name="uq_plan_gates_plan_id_name"),
        Index("ix_plan_gates_plan_id", "plan_id"),
    )


class PlanFuelling(Entity):
    __tablename__ = "plan_fuelling"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    carb_g_per_hr: Mapped[int] = mapped_column(Integer, nullable=False)
    fluid_ml_per_hr: Mapped[int] = mapped_column(Integer, nullable=False)
    sodium_mg_per_hr: Mapped[int] = mapped_column(Integer, nullable=False)
    caffeine_mg_total: Mapped[int] = mapped_column(Integer, nullable=False)
    total_carb_g: Mapped[int] = mapped_column(Integer, nullable=False)
    overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: §5.1: above 60 g/hr a single carbohydrate source cannot be oxidised, so
    #: the plan must specify a glucose:fructose mix. An output flag, not a
    #: numeric adjustment.
    requires_multiple_transportable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    binding_carb_key: Mapped[str | None] = mapped_column(Text)
    binding_fluid_key: Mapped[str | None] = mapped_column(Text)
    binding_sodium_key: Mapped[str | None] = mapped_column(Text)
    binding_caffeine_key: Mapped[str | None] = mapped_column(Text)


class PlanAidAction(Entity):
    """One action per aid station, in course order. Never synthesised."""

    __tablename__ = "plan_aid_actions"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    at_clock_minutes: Mapped[float] = mapped_column(Numeric, nullable=False)
    at_km: Mapped[float] = mapped_column(Numeric, nullable=False)
    leg: Mapped[Leg] = mapped_column(pg_enum(Leg, "leg"), nullable=False)
    station_name: Mapped[str] = mapped_column(Text, nullable=False)
    action_text: Mapped[str] = mapped_column(Text, nullable=False)
    cumulative_carb_g: Mapped[float] = mapped_column(Numeric, nullable=False)

    __table_args__ = (
        UniqueConstraint("plan_id", "ordinal", name="uq_plan_aid_actions_plan_id_ordinal"),
        Index("ix_plan_aid_actions_plan_id", "plan_id"),
    )


class PlanBag(Entity):
    """Exactly five per plan, always, even when one is empty."""

    __tablename__ = "plan_bags"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[BagKey] = mapped_column(pg_enum(BagKey, "bag_key"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    when_label: Mapped[str] = mapped_column(Text, nullable=False)
    item_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    plan: Mapped[Plan] = relationship(back_populates="bags")
    items: Mapped[list[PlanBagItem]] = relationship(
        back_populates="bag", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("plan_id", "key", name="uq_plan_bags_plan_id_key"),
        CheckConstraint("item_count >= 0", name="plan_bags_item_count_non_negative"),
    )


class PlanBagItem(Entity):
    """Every generated item carries the constraint that put it there.

    ``reason_constraint_key`` is mandatory for a generated item — that is what
    makes "Why this?" work on a bag item exactly as it works on a wattage
    target. The check permits a null reason only on a user-added item, which
    by definition has no upstream justification.
    """

    __tablename__ = "plan_bag_items"

    bag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plan_bags.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    qty: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    reason_constraint_key: Mapped[str | None] = mapped_column(Text)
    reason_text: Mapped[str | None] = mapped_column(Text)
    is_user_added: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    bag: Mapped[PlanBag] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("bag_id", "ordinal", name="uq_plan_bag_items_bag_id_ordinal"),
        CheckConstraint(
            "reason_constraint_key IS NOT NULL OR is_user_added = true",
            name="plan_bag_items_generated_item_has_reason",
        ),
    )


class PlanConstraintRef(Entity):
    """The "Why this?" drawer content. A solved-time snapshot, not a live join.

    Explanations reference numbers specific to that solve ("6 w", "9 s/km"),
    so re-deriving them later from current constraints would produce text that
    contradicts the plan it sits beside.
    """

    __tablename__ = "plan_constraint_refs"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str | None] = mapped_column(Text)
    #: Presentation of provenance. No solver branch reads it (§0.6).
    source_label: Mapped[str] = mapped_column(Text, nullable=False)
    binding: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    description: Mapped[str | None] = mapped_column(Text)
    affects_text: Mapped[str | None] = mapped_column(Text)
    override_text: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("plan_id", "key", name="uq_plan_constraint_refs_plan_id_key"),
    )


class OverrideEvent(CreatedOnly):
    """A logged override, written *before* the solve that consumes it.

    Shaped so a real override-outcome statistic becomes computable later by
    joining to post-race self-reports, without a schema change.
    """

    __tablename__ = "override_events"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )
    constraint_key: Mapped[str] = mapped_column(Text, nullable=False)
    overridden_from: Mapped[float] = mapped_column(Numeric, nullable=False)
    overridden_to: Mapped[float] = mapped_column(Numeric, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_override_events_plan_id", "plan_id"),)


class PlanDriftEvent(Entity):
    """Law 3: the plan is untouched until the athlete applies this."""

    __tablename__ = "plan_drift_events"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cause: Mapped[DriftCause] = mapped_column(pg_enum(DriftCause, "drift_cause"), nullable=False)
    severity: Mapped[DriftSeverity] = mapped_column(
        pg_enum(DriftSeverity, "drift_severity"),
        nullable=False,
        default=DriftSeverity.NORMAL,
        server_default=text("'normal'"),
    )
    #: `[{key, label, from, to}]`
    field_deltas: Mapped[JsonArray] = mapped_column(
        Json, nullable=False, server_default=text("'[]'")
    )
    status: Mapped[DriftStatus] = mapped_column(
        pg_enum(DriftStatus, "drift_status"),
        nullable=False,
        default=DriftStatus.PENDING,
        server_default=text("'pending'"),
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resulting_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL")
    )

    __table_args__ = (Index("ix_plan_drift_events_plan_id_status", "plan_id", "status"),)


class SolveJob(Entity):
    """The asynchronous escape hatch.

    Solves run synchronously in the request by default — the measured cost is
    well inside the 6 s SLA. This table and ``GET /solve-jobs/:id`` exist
    because the frontend already handles the 202 path and because a solve that
    ever ran long needs somewhere to go. A long solve is routed through a
    BackgroundTask that writes here.
    """

    __tablename__ = "solve_jobs"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[SolveJobStatus] = mapped_column(
        pg_enum(SolveJobStatus, "solve_job_status"),
        nullable=False,
        default=SolveJobStatus.QUEUED,
        server_default=text("'queued'"),
    )
    solve_input_hash: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    resulting_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL")
    )
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[JsonObject] = mapped_column(
        Json, nullable=False, server_default=text("'{}'")
    )
    request_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_solve_jobs_plan_id", "plan_id"),
        Index("ix_solve_jobs_user_id_status", "user_id", "status"),
    )


class SolveTiming(CreatedOnly):
    """Measured per-stage solve latency.

    The Admin dashboard's P50/P95/P99 must be real measurements, never display
    constants (Build Spec Part 5.4), and the nightly KPI aggregation reads
    these rows to produce them.
    """

    __tablename__ = "solve_timings"

    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL")
    )
    total_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    #: `{stage_name: milliseconds}` for the six stages.
    stage_timings_ms: Mapped[JsonObject] = mapped_column(
        Json, nullable=False, server_default=text("'{}'")
    )
    exceeded_sla: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    __table_args__ = (Index("ix_solve_timings_created_at", "created_at"),)
