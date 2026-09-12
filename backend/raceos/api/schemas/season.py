"""The season view's shapes."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SeasonRaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    race_id: UUID
    course_name: str
    course_place: str
    course_slug: str
    distance_type: str
    event_date: date
    race_status: str
    plan_id: UUID | None = None
    plan_version: int | None = None
    goal_minutes: float | None = None
    projected_minutes: float | None = None
    #: From the post-race analysis of the version live at race time. Absent is
    #: the ordinary case — most races have not been analysed — and a season
    #: view that invented a finish time for them would be worse than one that
    #: says nothing.
    actual_minutes: float | None = None
    has_analysis: bool = False

    goal_label: str | None = None
    projected_label: str | None = None
    actual_label: str | None = None


class SeasonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    #: The year the season starts in. A season spans two calendar years, so
    #: `label` is what a screen prints.
    season: int
    label: str
    planned_count: int
    raced_count: int
    races: list[SeasonRaceOut] = Field(default_factory=list)


class ConstraintPointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    value: float
    unit: str
    source: str
    at: datetime
    change_reason: str | None = None


class ConstraintTrackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    unit: str
    #: Oldest first. The whole series rather than a start-and-end pair, because
    #: the shape is the point: a value that rose, fell back and rose again is a
    #: different story from one that climbed steadily to the same place.
    points: list[ConstraintPointOut] = Field(default_factory=list)
    #: Signed change across the window. `None` when there is one point, because
    #: a single reading has not moved — it has only been taken.
    change: float | None = None


class SeasonHistoryOut(BaseModel):
    """A season at a glance, and how the athlete moved across it."""

    seasons: list[SeasonOut] = Field(default_factory=list)
    constraints: list[ConstraintTrackOut] = Field(default_factory=list)
