"""Plan request and response shapes.

Durations are numeric **minutes** in every field, formatted to `H:MM` only for
display. `format_hm` is provided so the frontend and the PDF renderer share
one implementation rather than two that drift.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from raceos.domain.enums import (
    BagKey,
    Feasibility,
    Leg,
    MarginState,
    PlanStatus,
    RiskLevel,
)


def format_hm(minutes: float | None) -> str | None:
    """`754.3` -> `"12:34"`. The one place minutes become a clock string."""
    if minutes is None:
        return None
    total = int(round(minutes))
    return f"{total // 60}:{total % 60:02d}"


class PlanCreate(BaseModel):
    race_id: UUID


class PlanDraftPatch(BaseModel):
    """Every builder step persists immediately; all fields are optional."""

    goal_minutes: float | None = Field(default=None, ge=1, le=2000)
    risk: RiskLevel | None = None
    night_flag: bool | None = None
    readiness_note: str | None = Field(default=None, max_length=300)
    forecast: dict[str, Any] | None = None


class SolveRequest(BaseModel):
    #: Requires a logged `override_events` row, which the endpoint writes
    #: before solving. Capped at the model's hard maximum, never above it.
    carb_override: float | None = Field(default=None, ge=20, le=120)
    #: Ask for a fresh solve even when the input hash already has one.
    force: bool = False


class OverrideRequest(BaseModel):
    constraint_key: str
    new_value: float
    reason: str | None = Field(default=None, max_length=500)


class SegmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordinal: int
    name: str
    leg: Leg
    from_km: float
    to_km: float
    terrain_desc: str | None
    target_watts: int | None
    target_pace_sec_per_km: int | None
    target_minutes: float
    note: str | None


class SplitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    leg: Leg
    distance: float
    target_pace_or_power: str
    unit: str
    split_minutes: float
    note: str | None
    split_label: str | None = None


class GateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    leg: Leg
    limit_minutes: float
    eta_minutes: float
    margin_minutes: float
    load_pct: float
    state: MarginState
    margin_label: str | None = None


class FuellingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    carb_g_per_hr: int
    fluid_ml_per_hr: int
    sodium_mg_per_hr: int
    caffeine_mg_total: int
    total_carb_g: int
    overridden: bool
    requires_multiple_transportable: bool
    binding_carb_key: str | None
    binding_fluid_key: str | None
    binding_sodium_key: str | None
    binding_caffeine_key: str | None


class AidActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordinal: int
    leg: Leg
    at_clock_minutes: float
    at_km: float
    station_name: str
    action_text: str
    cumulative_carb_g: float


class BagItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordinal: int
    name: str
    qty: str | None
    note: str | None
    #: Mandatory for a generated item. "Why this?" reads it directly.
    reason_constraint_key: str | None
    reason_text: str | None
    is_user_added: bool


class BagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: BagKey
    name: str
    when_label: str
    item_count: int
    items: list[BagItemOut] = Field(default_factory=list)


class ConstraintRefOut(BaseModel):
    """The "Why this?" drawer. A solved-time snapshot, not a live join."""

    model_config = ConfigDict(from_attributes=True)

    key: str
    name: str
    value: str
    unit: str | None
    source_label: str
    binding: bool
    description: str | None
    affects_text: str | None
    override_text: str | None


class PlanSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    race_id: UUID
    status: PlanStatus
    version: int
    goal_minutes: float | None
    projected_minutes: float | None
    projected_label: str | None = None
    feasibility: Feasibility
    worst_margin_minutes: float | None
    binding_constraint_key: str | None
    solved_at: datetime | None
    readiness_fraction: float | None
    readiness_note: str | None
    shared: bool
    #: §F.6. Which optional inputs the solver had to assume.
    assumed_fields: list[str] = Field(default_factory=list)


class PlanDetail(PlanSummary):
    #: Race identity, denormalised onto the plan.
    #:
    #: Every wall-clock time on the race card ("09:22 at km 34") is measured
    #: from the start, so a plan that does not carry its own date and start
    #: time forces the client into a second call just to render a heading.
    event_date: date | None = None
    start_time_local: str | None = None
    course_name: str | None = None
    course_place: str | None = None
    course_slug: str | None = None
    timezone: str | None = None
    bundle_version: str | None = None
    #: ODbL obliges attribution wherever derived geometry is displayed.
    attribution: str | None = None
    segments: list[SegmentOut] = Field(default_factory=list)
    splits: list[SplitOut] = Field(default_factory=list)
    gates: list[GateOut] = Field(default_factory=list)
    fuelling: FuellingOut | None = None
    aid_actions: list[AidActionOut] = Field(default_factory=list)
    bags: list[BagOut] = Field(default_factory=list)
    constraint_refs: list[ConstraintRefOut] = Field(default_factory=list)
    forecast_snapshot: dict[str, Any] = Field(default_factory=dict)


class SolveJobOut(BaseModel):
    """The 202 escape hatch. The synchronous path is the default."""

    job_id: UUID
    status: str
    poll_after_ms: int = 750
    resulting_plan_id: UUID | None = None
    error_code: str | None = None
