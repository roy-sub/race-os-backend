"""Post-race upload, analysis and calibration payloads."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from raceos.domain.enums import CompareState, RaceFileFormat, RaceFileStatus


class RaceFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    plan_id: UUID | None = None
    original_filename: str
    format: RaceFileFormat
    size_bytes: int
    status: RaceFileStatus
    uploaded_at: datetime
    #: Specific and actionable, naming what is missing — never "upload failed".
    failure_reason: str | None = None


class CompareRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordinal: int
    name: str
    planned: str
    actual: str
    delta: str
    state: CompareState
    why: str | None = None
    drift_pct: float | None = None


class CalibrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    constraint_key: str
    was: float
    now: float
    evidence_text: str
    applied: bool
    applied_at: datetime | None = None
    dismissed_at: datetime | None = None


class ActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rank: int
    projected_gain_minutes: float
    name: str
    description: str
    how_to: str


class AnalysisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    plan_id: UUID
    #: The version live at race time, stored rather than joined.
    plan_version: int
    race_file_id: UUID
    generated_at: datetime
    compare_rows: list[CompareRowOut] = Field(default_factory=list)
    calibrations: list[CalibrationOut] = Field(default_factory=list)
    actions: list[ActionOut] = Field(default_factory=list)


class AnalyseRequest(BaseModel):
    race_file_id: UUID
    #: Omit to compare against the version that was live at race time.
    plan_id: UUID | None = None
    race_id: UUID | None = None
