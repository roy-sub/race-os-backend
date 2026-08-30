"""Post-race file upload, analysis, and calibration write-back.

The analysis is diffed against **the plan version that was active at race
time**, not the current one, which is why ``plan_version`` is stored on the
analysis rather than resolved by a join at read time.

Calibration is the one path that writes a constraint with
``source = 'measured'``. That makes it the one path that can *upgrade*
provenance while *degrading* a number, which is the worst combination this
product can produce — so a derived value is written only when the evidence
genuinely qualifies (``SOLVER_MODEL.md`` §2.5.3 step 4), and the athlete
accepts or dismisses each proposal individually.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from raceos.db.base import Entity, pg_enum
from raceos.domain.enums import CompareState, RaceFileFormat, RaceFileStatus


class PostRaceFile(Entity):
    __tablename__ = "post_race_files"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL")
    )
    #: Random key. Never a user-controlled path: the filename is untrusted.
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[RaceFileFormat] = mapped_column(
        pg_enum(RaceFileFormat, "race_file_format"), nullable=False
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[RaceFileStatus] = mapped_column(
        pg_enum(RaceFileStatus, "race_file_status"),
        nullable=False,
        default=RaceFileStatus.PENDING,
        server_default=text("'pending'"),
    )
    #: Specific and actionable, naming what is missing — never "upload failed".
    failure_reason: Mapped[str | None] = mapped_column(Text)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_post_race_files_user_id", "user_id"),
        Index("ix_post_race_files_status", "status"),
        CheckConstraint("size_bytes > 0", name="post_race_files_size_positive"),
    )


class PostRaceAnalysis(Entity):
    __tablename__ = "post_race_analyses"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )
    #: The version live at race time. Stored, not joined: the plan may have
    #: been re-solved since, and comparing against the current version would
    #: silently judge the athlete against a plan they never raced.
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    race_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("post_race_files.id", ondelete="RESTRICT"), nullable=False
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    compare_rows: Mapped[list[AnalysisCompareRow]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )
    calibrations: Mapped[list[AnalysisCalibration]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )
    actions: Mapped[list[AnalysisAction]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("plan_id", "race_file_id", name="uq_post_race_analyses_plan_file"),
        Index("ix_post_race_analyses_plan_id", "plan_id"),
    )


class AnalysisCompareRow(Entity):
    """Planned versus actual, per segment."""

    __tablename__ = "analysis_compare_rows"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("post_race_analyses.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    planned: Mapped[str] = mapped_column(Text, nullable=False)
    actual: Mapped[str] = mapped_column(Text, nullable=False)
    delta: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[CompareState] = mapped_column(
        pg_enum(CompareState, "compare_state"), nullable=False
    )
    why: Mapped[str | None] = mapped_column(Text)
    drift_pct: Mapped[float | None] = mapped_column(Numeric)

    analysis: Mapped[PostRaceAnalysis] = relationship(back_populates="compare_rows")

    __table_args__ = (
        UniqueConstraint("analysis_id", "ordinal", name="uq_analysis_compare_rows_ordinal"),
    )


class AnalysisCalibration(Entity):
    """A proposed constraint change, accepted or dismissed individually."""

    __tablename__ = "analysis_calibrations"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("post_race_analyses.id", ondelete="CASCADE"), nullable=False
    )
    constraint_key: Mapped[str] = mapped_column(Text, nullable=False)
    was: Mapped[float] = mapped_column(Numeric, nullable=False)
    now: Mapped[float] = mapped_column(Numeric, nullable=False)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    analysis: Mapped[PostRaceAnalysis] = relationship(back_populates="calibrations")

    __table_args__ = (
        UniqueConstraint("analysis_id", "constraint_key", name="uq_analysis_calibrations_key"),
    )


class AnalysisAction(Entity):
    """Ranked recommendations with projected time gains.

    ``description`` and ``how_to`` rather than the frontend's ``desc``/``how``:
    those are mock-literal shorthand in a presentation object, and ``desc`` is
    a SQL reserved word. See ``docs/FIELD_NAME_RECONCILIATION.md`` R-002.
    """

    __tablename__ = "analysis_actions"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("post_race_analyses.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    projected_gain_minutes: Mapped[float] = mapped_column(Numeric, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    how_to: Mapped[str] = mapped_column(Text, nullable=False)

    analysis: Mapped[PostRaceAnalysis] = relationship(back_populates="actions")

    __table_args__ = (
        UniqueConstraint("analysis_id", "rank", name="uq_analysis_actions_analysis_id_rank"),
    )
