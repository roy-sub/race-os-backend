"""Request and response shapes for athlete-submitted courses."""

from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from raceos.domain.enums import DistanceType, SubmissionStatus


class SubmissionCreate(BaseModel):
    """What an athlete types before they upload anything.

    Coordinates are required because they anchor the DEM extract and the map's
    default view; an athlete who does not know them can read them off the race
    village pin on any map, which is a far smaller ask than tracing a course.
    """

    name: str = Field(min_length=3, max_length=120)
    place: str = Field(min_length=2, max_length=120)
    country: str | None = Field(default=None, max_length=2)
    timezone: str = Field(min_length=3, max_length=64)
    distance_type: DistanceType
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    event_date: date | None = None
    start_time_local: time | None = None
    notes: str | None = Field(default=None, max_length=2000)


class SubmissionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=3, max_length=120)
    place: str | None = Field(default=None, min_length=2, max_length=120)
    country: str | None = Field(default=None, max_length=2)
    timezone: str | None = Field(default=None, min_length=3, max_length=64)
    distance_type: DistanceType | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    event_date: date | None = None
    start_time_local: time | None = None
    notes: str | None = Field(default=None, max_length=2000)


class SubmissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: SubmissionStatus
    name: str
    place: str
    country: str | None
    timezone: str
    distance_type: DistanceType
    lat: float
    lng: float
    event_date: date | None
    start_time_local: time | None
    notes: str | None

    #: The athlete's own filenames, keyed by leg — so an error names the file
    #: they recognise rather than a storage key they have never seen.
    file_names: dict[str, str] = Field(default_factory=dict)
    #: Legs still without a route file. The UI's checklist reads from this
    #: rather than inferring it from three nullable keys.
    missing_legs: list[str] = Field(default_factory=list)

    #: Everything wrong with the last attempt, written for the athlete.
    #: Emptied on success, so a stale problem is never shown against a course
    #: that now works.
    problems: list[str] = Field(default_factory=list)
    processed_at: datetime | None = None
    #: Set once the course exists and can be entered as a race.
    course_id: UUID | None = None
    created_at: datetime
