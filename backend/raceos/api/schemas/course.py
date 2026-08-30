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
