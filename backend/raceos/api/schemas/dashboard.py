"""Dashboard, My Plans and notification-inbox payloads."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from raceos.domain.enums import (
    DriftSensitivity,
    NotificationSeverity,
    NotificationType,
)


class RaceCardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    race_id: UUID
    course_id: UUID
    course_name: str
    course_place: str
    course_slug: str
    distance_type: str
    event_date: date
    start_time_local: str
    days_away: int
    race_status: str
    is_race_week: bool

    plan_id: UUID | None = None
    plan_version: int | None = None
    plan_status: str
    feasibility: str
    goal_minutes: float | None = None
    projected_minutes: float | None = None
    worst_margin_minutes: float | None = None
    readiness_fraction: float | None = None
    readiness_note: str | None = None
    shared: bool = False
    solved_at: datetime | None = None

    bundle_version: str | None = None
    has_pending_drift: bool = False
    drift_severity: str | None = None
    drift_summary: str | None = None
    next_action: str
    next_action_href: str

    #: Formatted at the boundary, never stored. Storage keeps numerics;
    #: `"11:45"` is a rendering.
    goal_label: str | None = None
    projected_label: str | None = None
    margin_label: str | None = None


class QuietWindowOut(BaseModel):
    race_id: UUID
    opens_at: datetime
    closes_at: datetime
    reason: str


class DashboardCounts(BaseModel):
    upcoming: int
    unsolved: int
    needs_review: int
    unread_notifications: int


class AthleteSummary(BaseModel):
    id: UUID
    name: str | None = None
    tier: str


class DashboardOut(BaseModel):
    athlete: AthleteSummary
    next_race: RaceCardOut | None = None
    races: list[RaceCardOut] = Field(default_factory=list)
    counts: DashboardCounts
    quiet_windows: list[QuietWindowOut] = Field(default_factory=list)


class MyPlansOut(BaseModel):
    """Grouped exactly as the screen groups them.

    Server-side rather than a client filter, so "active" means one thing.
    """

    active: list[RaceCardOut] = Field(default_factory=list)
    draft: list[RaceCardOut] = Field(default_factory=list)
    past: list[RaceCardOut] = Field(default_factory=list)


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type_key: NotificationType
    tag: str | None = None
    severity: NotificationSeverity
    race_id: UUID | None = None
    plan_id: UUID | None = None
    title: str
    body: str
    deltas: list[dict[str, object]] = Field(default_factory=list)
    cta_label: str | None = None
    cta_href: str | None = None
    read: bool
    created_at: datetime


class NotificationPage(BaseModel):
    data: list[NotificationOut] = Field(default_factory=list)
    total: int
    unread: int
    limit: int
    offset: int


class PreferenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type_key: NotificationType
    channel_email: bool
    channel_push: bool
    channel_inapp: bool
    drift_sensitivity: DriftSensitivity
    #: True for `drift` and `cutoff`: in-app cannot be switched off, and the
    #: settings screen shows the toggle locked rather than pretending it
    #: worked.
    inapp_locked: bool = False


class PreferencePatch(BaseModel):
    channel_email: bool | None = None
    channel_push: bool | None = None
    channel_inapp: bool | None = None
    drift_sensitivity: DriftSensitivity | None = None
