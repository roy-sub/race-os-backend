"""Courses, versioned bundles, and an athlete's entry into one.

The five columns added beyond the original Build Spec Part 4.3 —
``segments``, ``waypoints``, ``elevation_source``, ``attribution`` on
``course_bundles``, and ``surface_quality`` on ``course_bundle_legs`` — come
from ``pipelines/course-ingest/docs/SCHEMA_CHANGES.md``, which is exact
replacement text for a decision already taken and already implemented by the
pipeline. Each has a reason it cannot live anywhere else, restated on the
column.

Two constraints are enforced by the database rather than by the loader, because
``SOLVER_MODEL.md`` §1.2 would otherwise reject the bundle at *solve* time,
which is hours too late and lands on an athlete rather than on an admin.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from raceos.db.base import CreatedOnly, Entity, Json, JsonArray, JsonObject, pg_enum
from raceos.domain.enums import (
    BundleStatus,
    Difficulty,
    DistanceType,
    Leg,
    Provenance,
    RaceStatus,
    SurfaceQuality,
)


class Course(Entity):
    """The venue and route, stable across years."""

    __tablename__ = "courses"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    place: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    distance_type: Mapped[DistanceType] = mapped_column(
        pg_enum(DistanceType, "distance_type"), nullable=False
    )
    difficulty: Mapped[Difficulty] = mapped_column(
        pg_enum(Difficulty, "difficulty"), nullable=False
    )
    #: Hysteresis-filtered surveyed ascent — the figure a UI shows. The solver
    #: recomputes gain from the node series and is unaffected by this column;
    #: see SCHEMA_CHANGES.md §7 for why the two differ and do not disagree.
    elevation_gain_m: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    media_hero_path: Mapped[str | None] = mapped_column(Text)
    media_card_path: Mapped[str | None] = mapped_column(Text)
    #: Fallback background behind course art. With all 41 media assets absent
    #: in V1, this is what actually renders on a course card.
    tone_color: Mapped[str | None] = mapped_column(String(9))
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    lat: Mapped[float] = mapped_column(Numeric, nullable=False)
    lng: Mapped[float] = mapped_column(Numeric, nullable=False)
    is_fictional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    bundles: Mapped[list[CourseBundle]] = relationship(back_populates="course")

    __table_args__ = (
        Index("ix_courses_distance_type", "distance_type"),
        CheckConstraint("lat BETWEEN -90 AND 90", name="courses_lat_range"),
        CheckConstraint("lng BETWEEN -180 AND 180", name="courses_lng_range"),
    )


class CourseBundle(Entity):
    """A versioned, publishable snapshot of everything the solver needs."""

    __tablename__ = "course_bundles"

    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[BundleStatus] = mapped_column(
        pg_enum(BundleStatus, "bundle_status"),
        nullable=False,
        default=BundleStatus.DRAFT,
        server_default=text("'draft'"),
    )
    provenance: Mapped[Provenance] = mapped_column(
        pg_enum(Provenance, "provenance"),
        nullable=False,
        default=Provenance.ESTIMATED,
        server_default=text("'ESTIMATED'"),
    )
    verified_at: Mapped[date | None] = mapped_column(Date)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    season_year: Mapped[int | None] = mapped_column(Integer)

    elevation_profile: Mapped[JsonObject] = mapped_column(
        Json, nullable=False, server_default=text("'{}'")
    )
    #: `{name, leg, limit_minutes_from_start, km}`. Zero barriers is a data
    #: error, not a solvable plan (§1.2), enforced below.
    barriers: Mapped[JsonArray] = mapped_column(Json, nullable=False, server_default=text("'[]'"))
    #: `{leg, name, km, contents[], provenance}` — aid stations ONLY.
    #: "One action per aid station" (§5.5) is a correctness property, and
    #: keeping this array pure makes it hold by construction.
    aid_stations: Mapped[JsonArray] = mapped_column(
        Json, nullable=False, server_default=text("'[]'")
    )
    #: `{type, leg, name, km, provenance}` — transitions, special needs and
    #: distance markers. Deliberately NOT inside `aid_stations`.
    waypoints: Mapped[JsonArray] = mapped_column(Json, nullable=False, server_default=text("'[]'"))
    #: `{ordinal, leg, name, from_km, to_km, net_gradient, elevation_gain_m,
    #: surface_quality, name_source}`. Named segments are the solver's primary
    #: unit of work (§1.1, §4.2.1): one power target per segment, time
    #: integrated over the node series inside it. `plan_segments` is solver
    #: *output* keyed to a plan, so it cannot be the input.
    segments: Mapped[JsonArray] = mapped_column(Json, nullable=False, server_default=text("'[]'"))

    #: §1.2 raises BundleIncomplete for anything but 'terrain'. A column
    #: rather than a blob key makes the invariant queryable, and the check
    #: below makes an offending bundle unstorable rather than merely
    #: unsolvable.
    elevation_source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'terrain'")
    )
    #: ODbL obliges attribution wherever the derived data is displayed, so it
    #: is a licence-compliance artefact and must be auditable with a query.
    attribution: Mapped[str] = mapped_column(Text, nullable=False)

    changelog: Mapped[str | None] = mapped_column(Text)
    plans_affected_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    bundle_asset_key: Mapped[str | None] = mapped_column(Text)
    terrain_pmtiles_key: Mapped[str | None] = mapped_column(Text)
    #: How the bundle was built: pinned road-data release, DEM tileset and
    #: sample zoom, node spacing, cut-off ratios. Not a column in the original
    #: spec; kept with the artefact for auditability.
    provenance_detail: Mapped[JsonObject] = mapped_column(
        Json, nullable=False, server_default=text("'{}'")
    )

    course: Mapped[Course] = relationship(back_populates="bundles")
    legs: Mapped[list[CourseBundleLeg]] = relationship(
        back_populates="bundle", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("course_id", "version", name="uq_course_bundles_course_id_version"),
        CheckConstraint(
            "elevation_source = 'terrain'", name="course_bundles_elevation_source_terrain"
        ),
        CheckConstraint("length(attribution) > 0", name="course_bundles_attribution_present"),
        CheckConstraint("jsonb_array_length(barriers) > 0", name="course_bundles_barriers_present"),
        Index("ix_course_bundles_course_id_status", "course_id", "status"),
        Index(
            "ix_course_bundles_segments_gin",
            "segments",
            postgresql_using="gin",
            postgresql_ops={"segments": "jsonb_path_ops"},
        ),
    )


class CourseBundleLeg(Entity):
    """One leg's geometry. Separate table so PostGIS indexes work per leg.

    ``geometry`` is ``LINESTRING Z``: the Z ordinate **is** the elevation
    series the solver reads. ``elevation_profile.legs[*].display`` on the
    bundle is a charting downsample and must never be solved against.
    """

    __tablename__ = "course_bundle_legs"

    bundle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("course_bundles.id", ondelete="CASCADE"), nullable=False
    )
    leg: Mapped[Leg] = mapped_column(pg_enum(Leg, "leg"), nullable=False)
    geometry: Mapped[str] = mapped_column(
        Geometry(geometry_type="LINESTRINGZ", srid=4326, spatial_index=False), nullable=False
    )
    distance_m: Mapped[float] = mapped_column(Numeric, nullable=False)
    elevation_gain_m: Mapped[float] = mapped_column(
        Numeric, nullable=False, default=0, server_default=text("0")
    )
    node_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: §I.2.2 takes Crr from the course surface, not the athlete. NOT NULL
    #: because it is meaningful on every leg the solver costs; the pipeline
    #: writes a placeholder on the swim row, which the solver never reads.
    surface_quality: Mapped[SurfaceQuality] = mapped_column(
        pg_enum(SurfaceQuality, "surface_quality"),
        nullable=False,
        default=SurfaceQuality.TYPICAL_ROAD,
        server_default=text("'typical_road'"),
    )

    bundle: Mapped[CourseBundle] = relationship(back_populates="legs")

    __table_args__ = (
        UniqueConstraint("bundle_id", "leg", name="uq_course_bundle_legs_bundle_id_leg"),
        CheckConstraint("distance_m > 0", name="course_bundle_legs_distance_positive"),
        CheckConstraint("node_count > 1", name="course_bundle_legs_node_count_min"),
        Index("ix_course_bundle_legs_geometry", "geometry", postgresql_using="gist"),
    )


class CourseBundleDiff(CreatedOnly):
    """Field-level deltas between two bundle versions.

    Drives the admin blast-radius preview and per-plan drift when a bundle
    changes under a solved plan.
    """

    __tablename__ = "course_bundle_diffs"

    from_bundle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("course_bundles.id", ondelete="CASCADE"), nullable=False
    )
    to_bundle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("course_bundles.id", ondelete="CASCADE"), nullable=False
    )
    #: `[{key, label, from, to}]`
    field_deltas: Mapped[JsonArray] = mapped_column(
        Json, nullable=False, server_default=text("'[]'")
    )
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("from_bundle_id", "to_bundle_id", name="uq_course_bundle_diffs_from_to"),
    )


class Race(Entity):
    """An athlete's entry into a specific course edition."""

    __tablename__ = "races"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="RESTRICT"), nullable=False
    )
    #: The bundle version this race is pinned to. A plan solved against it
    #: stays solved against it until the athlete applies a drift event.
    course_bundle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("course_bundles.id", ondelete="RESTRICT"), nullable=False
    )
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time_local: Mapped[time] = mapped_column(Time, nullable=False)
    status: Mapped[RaceStatus] = mapped_column(
        pg_enum(RaceStatus, "race_status"),
        nullable=False,
        default=RaceStatus.UPCOMING,
        server_default=text("'upcoming'"),
    )
    bib: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_races_user_id_status", "user_id", "status"),
        Index("ix_races_event_date", "event_date"),
        Index("ix_races_course_bundle_id", "course_bundle_id"),
    )
