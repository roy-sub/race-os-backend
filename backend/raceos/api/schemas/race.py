"""An athlete's entry into a specific race.

A ``Race`` is the join between an athlete and a course edition: *this* person,
on *this* course, on *this* date. Everything downstream hangs off it — a plan
is solved for a race, not for a course — which is why it has to exist before
the plan builder can start.
"""

from __future__ import annotations

from datetime import date, time
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from raceos.domain.enums import RaceStatus


class RaceCreate(BaseModel):
    """Identify the course by slug or id — whichever the caller has.

    The directory hands out slugs and the course detail page hands out ids;
    requiring one specific form would make one of those screens do a lookup
    for no reason.
    """

    course_ref: str = Field(
        description="Course slug (e.g. 'tramuntana-full') or its UUID",
        min_length=1,
        max_length=200,
    )
    event_date: date
    #: Local start time at the course, not the athlete's timezone. The course
    #: carries the timezone; a start time without one is ambiguous.
    start_time_local: time
    bib: str | None = Field(default=None, max_length=20)


class RaceUpdate(BaseModel):
    event_date: date | None = None
    start_time_local: time | None = None
    bib: str | None = Field(default=None, max_length=20)
    status: RaceStatus | None = None


class RaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    course_id: UUID
    course_bundle_id: UUID
    event_date: date
    start_time_local: time
    status: RaceStatus
    bib: str | None = None

    #: Denormalised so a race list needs no second call per row.
    course_name: str | None = None
    course_place: str | None = None
    course_slug: str | None = None
    distance_type: str | None = None
    timezone: str | None = None
    bundle_version: str | None = None

    #: Negative once the race is in the past.
    days_away: int | None = None
    #: Set when this race already has a plan, so the UI can link straight to it
    #: instead of offering to start one that already exists.
    plan_id: UUID | None = None
    plan_status: str | None = None


class ForecastOut(BaseModel):
    """The forecast for a race's start hour, as it stands right now.

    Deliberately **not** the plan's ``forecast_snapshot``. That one is frozen
    at solve time and must stay frozen — Law 3 says a plan's numbers do not
    change under the athlete. This is the live reading beside it, so the
    difference between the two is visible and the athlete can decide whether
    to re-solve.

    ``available`` is false rather than the response being a 404, because "no
    forecast" is an ordinary, expected state with several ordinary causes, and
    each of them wants different words on the screen. A 404 would say the race
    does not exist.
    """

    model_config = ConfigDict(from_attributes=True)

    available: bool
    #: Why there is no forecast, when there is none. One of
    #: ``beyond_horizon`` (the race is further out than a forecast means
    #: anything), ``provider_unavailable``, or ``course_unlocatable``.
    unavailable_reason: str | None = None
    #: How many days out the race is. Present even when the forecast is not,
    #: because "twelve days out" is the explanation for `beyond_horizon`.
    days_away: int | None = None
    #: The horizon this deployment trusts, in hours. Returned so the UI can
    #: say when to come back rather than guessing.
    horizon_hours: int | None = None

    temp_c: float | None = None
    humidity: float | None = None
    wind_speed_ms: float | None = None
    wind_dir_deg: float | None = None
    conditions: str | None = None
    water_temp_c: float | None = None
    pressure_hpa: float | None = None
    cloud_cover_pct: float | None = None

    #: The local date and hour this forecast is for — the race's start hour,
    #: not "now". A forecast for 3 a.m. would be no use to a 07:00 start.
    for_local_time: str | None = None
