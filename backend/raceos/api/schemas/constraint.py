"""Constraint request and response shapes."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from raceos.domain.enums import ConstraintSource


class ConstraintOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    key: str
    value: float
    unit: str
    #: Law 2: provenance is stored, queryable and rendered everywhere the
    #: value appears — including inside generated PDFs.
    source: ConstraintSource
    source_detail: str | None
    confidence_pct: int | None
    evidence_note: str | None
    tested_at: datetime | None
    measured_at: datetime | None
    measured_at_temp_c: float | None
    updated_at: datetime
    stale: bool = False


class ConstraintWrite(BaseModel):
    value: float
    #: Defaults to `manual` — a typed value is a manual one. An estimator or a
    #: calibration sets its own source through its own route.
    source: ConstraintSource = ConstraintSource.MANUAL
    evidence_note: str | None = Field(default=None, max_length=500)
    #: §F.4. Currently meaningful for `sweat_rate`.
    measured_at_temp_c: float | None = None


class ConstraintHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    key: str
    value: float
    unit: str
    source: ConstraintSource
    source_detail: str | None
    evidence_note: str | None
    superseded_at: datetime | None
    change_reason: str | None
    created_at: datetime


class EstimateRequest(BaseModel):
    """Answers to the estimator's two questions. Shape varies by key."""

    answers: dict[str, Any] = Field(default_factory=dict)


class EstimateOut(BaseModel):
    key: str
    value: float
    unit: str
    confidence_pct: int
    evidence_note: str
    #: Whether the estimate was written to the athlete's constraints, or just
    #: returned for them to look at first.
    applied: bool
