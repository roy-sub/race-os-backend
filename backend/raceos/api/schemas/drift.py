"""Drift and bundle-publish payloads."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from raceos.domain.enums import DriftCause, DriftSeverity, DriftStatus


class DriftEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    plan_id: UUID
    detected_at: datetime
    cause: DriftCause
    severity: DriftSeverity
    status: DriftStatus
    field_deltas: list[dict[str, Any]] = Field(default_factory=list)
    applied_at: datetime | None = None
    resulting_plan_id: UUID | None = None


class DriftCheckOut(BaseModel):
    """A shadow recompute's verdict. **Nothing has been written to the plan.**"""

    plan_id: UUID
    cause: DriftCause
    severity: DriftSeverity
    material: bool
    now_infeasible: bool = False
    infeasible_message: str = ""
    projected_minutes: float | None = None
    worst_margin_minutes: float | None = None
    field_deltas: list[dict[str, Any]] = Field(default_factory=list)
    event: DriftEventOut | None = None


class AffectedPlanOut(BaseModel):
    plan_id: UUID
    race_id: UUID
    user_id: UUID
    event_date: date
    days_away: int


class BlastRadiusOut(BaseModel):
    course_id: UUID
    course_name: str
    from_bundle_version: str | None = None
    to_bundle_version: str
    athletes: int
    races: int
    plans: int
    races_in_race_week: int
    field_deltas: list[dict[str, Any]] = Field(default_factory=list)
    affected: list[AffectedPlanOut] = Field(default_factory=list)
    freeze_blocked: bool
    freeze_reason: str = ""


class PublishRequest(BaseModel):
    #: A bundle correcting a *wrong* cut-off is more dangerous to withhold
    #: than to publish. Deliberate and audited, never a default.
    override_freeze: bool = False
    #: Required when overriding, because the audit line needs to say why.
    override_reason: str | None = Field(default=None, max_length=500)


class PublishResultOut(BaseModel):
    bundle_id: UUID
    version: str
    published_at: datetime | None = None
    superseded_version: str | None = None
    plans_affected: int
    drift_events_raised: int
    field_deltas: list[dict[str, Any]] = Field(default_factory=list)
