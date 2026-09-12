"""Response shapes for courses and bundles.

Field names follow the frontend where the frontend names a *field*, and follow
storage where the frontend's value is a formatted display string. The rule,
from ``BACKENDREQUIREMENTS.md`` §4: store numerics, format at the API boundary.

So ``elevation_gain_m`` is an integer here and the frontend's ``gain:
"2,340 m"`` is rendered from it; ``cutoff`` likewise is derived from the real
barriers rather than stored as ``"10:30 bike"``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from raceos.domain.enums import (
    BundleStatus,
    CourseAvailability,
    CourseVisibility,
    Difficulty,
    DistanceType,
    Leg,
    Provenance,
    SurfaceQuality,
)

#: The frontend's `Provenance` union spells the crowd value `CROWD-VERIFIED`,
#: while the schema's enum (Build Spec Part 4.1's DDL) spells it `CROWD`. The
#: build spec outranks the frontend for storage, and the frontend outranks the
#: build spec for what it renders — so the value is stored as `CROWD` and
#: serialised as `CROWD-VERIFIED`. Recorded in
#: docs/FIELD_NAME_RECONCILIATION.md R-005.
PROVENANCE_DISPLAY: dict[Provenance, str] = {
    Provenance.OFFICIAL: "OFFICIAL",
    Provenance.CROWD: "CROWD-VERIFIED",
    Provenance.ESTIMATED: "ESTIMATED",
}


class CourseSummary(BaseModel):
    """One row of the race directory."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    place: str
    slug: str
    distance_type: DistanceType
    difficulty: Difficulty
    elevation_gain_m: int | None
    tone_color: str | None
    media_card_path: str | None
    media_hero_path: str | None
    timezone: str
    lat: float
    lng: float
    is_fictional: bool

    #: Whether this event can be entered yet. A ``coming_soon`` row is listed
    #: on purpose: the season is announced as a whole, the course data lands
    #: one event at a time, and hiding the rest would misrepresent the season.
    availability: CourseAvailability = CourseAvailability.COMING_SOON
    #: Catalogue row or signed-out showcase. See :class:`CourseVisibility`.
    visibility: CourseVisibility = CourseVisibility.CATALOGUE
    #: The organiser's announced date for the next edition, where there is one.
    #: Display only — an athlete's own race still owns the date they plan to.
    next_edition_date: date | None = None
    official_event_name: str | None = None
    #: True when the athlete reading this added the course themselves.
    is_user_submitted: bool = False

    #: Whether the caller may see this course's geometry, elevation series and
    #: furniture. Sent so a client renders the correct state in one request
    #: rather than discovering the gate by being refused.
    map_unlocked: bool = False
    #: Why not, when ``map_unlocked`` is false. Written for the athlete.
    map_locked_reason: str | None = None
    #: Set on the showcase course only. The signed-out hero map is a tuned,
    #: deliberately out-of-scale illustration, and it says so on its face
    #: rather than in a footnote nobody reads.
    illustrative_map: bool = False

    #: From the published bundle, when one exists. A course with no published
    #: bundle is still listed — it just cannot be solved against yet.
    provenance: str | None = None
    bundle_version: str | None = None
    #: `limit_minutes_from_start` of the bike cut-off, or of the finish when a
    #: course has no bike cut-off. Minutes, not `"10:30 bike"`.
    cutoff_minutes: float | None = None
    cutoff_barrier_name: str | None = None


class LegSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    leg: Leg
    distance_m: float
    elevation_gain_m: float
    node_count: int
    surface_quality: SurfaceQuality


class BundleSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: str
    status: BundleStatus
    provenance: str
    verified_at: date | None
    published_at: datetime | None
    season_year: int | None
    changelog: str | None
    #: ODbL obliges this wherever the derived data is displayed. It is carried
    #: on every response that includes geometry so a client cannot render the
    #: data without having been handed the attribution alongside it.
    attribution: str
    elevation_source: str
    plans_affected_count: int


class CourseDetail(CourseSummary):
    """A course plus the summary of its active bundle."""

    active_bundle: BundleSummary | None = None
    legs: list[LegSummary] = Field(default_factory=list)


class BundleDetail(BundleSummary):
    """Everything the map, the elevation profile and the solver read.

    One geometry, three consumers (Build Spec Part 10.3). Divergence between
    them is a bug, so they are served from this one payload.
    """

    course_id: UUID
    legs: list[LegSummary] = Field(default_factory=list)
    barriers: list[dict[str, Any]] = Field(default_factory=list)
    aid_stations: list[dict[str, Any]] = Field(default_factory=list)
    waypoints: list[dict[str, Any]] = Field(default_factory=list)
    segments: list[dict[str, Any]] = Field(default_factory=list)
    elevation_profile: dict[str, Any] = Field(default_factory=dict)
    bundle_asset_key: str | None = None
    terrain_pmtiles_key: str | None = None
    #: How the bundle was built — pinned road-data release, DEM tileset,
    #: node spacing, cut-off ratios. Not a column; kept for auditability.
    provenance_detail: dict[str, Any] = Field(default_factory=dict)


class BundleHistoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: str
    status: BundleStatus
    provenance: str
    published_at: datetime | None
    changelog: str | None
    plans_affected_count: int


class Page(BaseModel):
    """Cursor pagination envelope: ``{data, meta}``."""

    data: list[Any]
    meta: dict[str, Any]


class ConditionsObservationOut(BaseModel):
    """One past edition's race-morning weather, as the archive recorded it."""

    model_config = ConfigDict(from_attributes=True)

    observed_on: date
    observed_hour: int
    air_temp_c: float
    humidity_pct: float
    wind_speed_ms: float
    wind_dir_deg: float | None = None
    precipitation_mm: float | None = None
    cloud_cover_pct: float | None = None
    #: Null wherever the marine archive does not cover this swim — a lake, an
    #: inland reservoir. Not estimated from air temperature.
    water_temp_c: float | None = None


class ConditionsSummaryOut(BaseModel):
    """Medians, and the count they rest on.

    The count travels with every figure deliberately: a median of two years is
    a different claim from a median of ten, and a panel showing the number
    without it makes them look the same.
    """

    model_config = ConfigDict(from_attributes=True)

    observations: int
    median_air_temp_c: float | None = None
    median_humidity_pct: float | None = None
    median_wind_speed_ms: float | None = None
    warmest_air_temp_c: float | None = None
    coolest_air_temp_c: float | None = None
    water_observations: int = 0
    median_water_temp_c: float | None = None
    #: Fraction of observed years whose water would have been wetsuit-legal.
    #: Null when no year has a water temperature — never inferred from the air.
    wetsuit_legal_fraction: float | None = None
    wet_start_fraction: float | None = None


class ConditionsHistoryOut(BaseModel):
    """What race day has actually been like here.

    **There is no finish-time distribution.** It needs real results, which this
    system does not have and cannot obtain, so the field is absent rather than
    drawn — `finish_times_available` says so in as many words, so a client does
    not have to infer it from a missing key.
    """

    course_slug: str
    course_name: str
    summary: ConditionsSummaryOut
    observations: list[ConditionsObservationOut] = Field(default_factory=list)
    finish_times_available: bool = False
    #: Why there is nothing, when there is nothing. One of
    #: ``no_edition_date`` (nothing to anchor past years to) or
    #: ``not_collected_yet``.
    empty_reason: str | None = None
