"""Coach and share-link payloads.

**There is no constraints permission field, and there must never be one.**
The absence is deliberate and load-bearing: a request body cannot ask for
something the schema has no field for.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from raceos.domain.enums import CoachLinkStatus, ShareScope


class InviteRequest(BaseModel):
    athlete_email: EmailStr


class CoachLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    coach_id: UUID
    athlete_id: UUID
    status: CoachLinkStatus
    perm_plans: bool
    perm_build: bool
    perm_analysis: bool
    invited_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    invite_expires_at: datetime | None = None
    coach_name: str | None = None
    athlete_name: str | None = None


class InviteResponse(BaseModel):
    link: CoachLinkOut
    #: Returned once. Only its hash is stored, so this cannot be re-read.
    invite_token: str
    invite_url: str


class AcceptRequest(BaseModel):
    token: str = Field(min_length=16)


class PermissionPatch(BaseModel):
    """The three permissions. There is no fourth."""

    plans: bool | None = None
    build: bool | None = None
    analysis: bool | None = None


class NoteRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    coach_id: UUID
    athlete_id: UUID
    body: str
    created_at: datetime


class BoardRowOut(BaseModel):
    athlete_id: UUID
    athlete_name: str | None = None
    link_id: UUID
    can_view_plans: bool
    can_build: bool
    can_view_analysis: bool
    race_id: UUID | None = None
    course_name: str | None = None
    event_date: date | None = None
    days_away: int | None = None
    plan_id: UUID | None = None
    plan_version: int | None = None
    feasibility: str | None = None
    projected_minutes: float | None = None
    projected_label: str | None = None
    worst_margin_minutes: float | None = None
    margin_label: str | None = None
    has_pending_drift: bool = False
    readiness_fraction: float | None = None
    withheld_reason: str | None = None


class CompareRequest(BaseModel):
    athlete_ids: list[UUID] = Field(min_length=1, max_length=15)


# ---------------------------------------------------------------------------
# Share links
# ---------------------------------------------------------------------------


class ShareCreateRequest(BaseModel):
    scope: ShareScope = ShareScope.FULL_PLAN
    #: Mandatory expiry. There is no "never" option, by design.
    expires_in_days: int = Field(default=30, ge=1, le=180)
    recipient_label: str | None = Field(default=None, max_length=120)
    #: An optional *second* factor. Never the sole gate.
    access_code: str | None = Field(default=None, min_length=4, max_length=64)


class ShareLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    plan_id: UUID
    scope: ShareScope
    #: A short non-secret prefix, so a link is identifiable in a list without
    #: storing anything that could reconstruct it.
    token_prefix: str
    recipient_label: str | None = None
    expires_at: datetime
    revoked_at: datetime | None = None
    opens_count: int
    last_opened_at: datetime | None = None
    has_access_code: bool = False


class ShareCreateResponse(BaseModel):
    link: ShareLinkOut
    #: Returned once, at creation.
    token: str
    url: str


class SharedPlanOut(BaseModel):
    """Built from an allow-list.

    Note what is absent: every constraint value, the athlete's weight, their
    email, their tier, their other races. No scope includes them — not even
    ``full_plan``.
    """

    plan_version: int
    course_name: str | None = None
    course_place: str | None = None
    event_date: str | None = None
    start_time_local: str | None = None
    projected_label: str | None = None
    feasibility: str
    bundle_version: str | None = None
    attribution: str | None = None
    shared_by: str | None = None
    scope: ShareScope
    expires_at: str

    splits: list[dict[str, Any]] | None = None
    segments: list[dict[str, Any]] | None = None
    gates: list[dict[str, Any]] | None = None
    fuelling: dict[str, Any] | None = None
    aid_actions: list[dict[str, Any]] | None = None
    bags: list[dict[str, Any]] | None = None
