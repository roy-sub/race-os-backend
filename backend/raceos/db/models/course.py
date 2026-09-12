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
    CourseAvailability,
    CourseVisibility,
    CurationStatus,
    Difficulty,
    DistanceType,
    Leg,
    Provenance,
    RaceStatus,
    SubmissionStatus,
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

    #: Whether this row can be entered as a race yet.
    #:
    #: A listed course with no course data is a real product state, not an
    #: oversight: the season is announced as a whole and the bundles land one
    #: at a time. Storing it rather than inferring it from "has a bundle"
    #: means an event can be listed as coming soon while its bundle is being
    #: reviewed, and a bundle that fails review does not silently promote a
    #: row to bookable.
    availability: Mapped[CourseAvailability] = mapped_column(
        pg_enum(CourseAvailability, "course_availability"),
        nullable=False,
        default=CourseAvailability.COMING_SOON,
        server_default=text("'coming_soon'"),
    )
    #: Catalogue row, or the signed-out showcase. See :class:`CourseVisibility`.
    visibility: Mapped[CourseVisibility] = mapped_column(
        pg_enum(CourseVisibility, "course_visibility"),
        nullable=False,
        default=CourseVisibility.CATALOGUE,
        server_default=text("'catalogue'"),
    )
    #: The published date of the next edition, where the organiser has
    #: announced one.
    #:
    #: A *race* still owns the date an athlete plans against — this column
    #: does not change that and nothing solves from it. It exists because a
    #: directory of dateless venues is unusable for choosing a race, which is
    #: the one job the directory has.
    next_edition_date: Mapped[date | None] = mapped_column(Date)
    #: The organiser's own event name, where it differs from :attr:`name`.
    official_event_name: Mapped[str | None] = mapped_column(Text)
    #: Who submitted this course, when an athlete added it themselves.
    #:
    #: Null for everything in the official catalogue. A non-null value makes
    #: the row private to that athlete — see ``course_service.list_courses``.
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    #: Whether a submitted course has been reviewed into the shared catalogue.
    #:
    #: Meaningless while :attr:`submitted_by_user_id` is null — a house course
    #: is in the catalogue by construction and has nothing to review — which
    #: is why the default is ``UNREVIEWED`` rather than ``PUBLISHED``: it says
    #: "no review has happened", which is true of both.
    curation_status: Mapped[CurationStatus] = mapped_column(
        pg_enum(CurationStatus, "curation_status"),
        nullable=False,
        default=CurationStatus.UNREVIEWED,
        server_default=text("'unreviewed'"),
    )
    #: Why a reviewer published or rejected it, in the submitter's terms.
    curation_note: Mapped[str | None] = mapped_column(Text)
    curated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    curated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    bundles: Mapped[list[CourseBundle]] = relationship(back_populates="course")

    __table_args__ = (
        Index("ix_courses_distance_type", "distance_type"),
        Index("ix_courses_visibility", "visibility"),
        Index("ix_courses_submitted_by_user_id", "submitted_by_user_id"),
        Index("ix_courses_curation_status", "curation_status"),
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


class CourseSubmission(Entity):
    """An athlete adding a race the catalogue does not carry yet.

    **The athlete is not blocked on our release schedule.** Fifteen events are
    listed and one has course data; an athlete racing the sixteenth should not
    have to wait, and the files they already hold — the athlete guide's GPX,
    or a route they traced — are enough to build a real bundle from.

    The row is a *workflow*, not a course. It holds what was uploaded and what
    happened to it, and on success it points at the :class:`Course` that was
    created. Keeping the two separate is what lets a failed submission carry
    its problems back to the athlete without a broken half-course existing in
    the directory for even a moment.

    Uploaded files live in object storage, never in this table: a 90 km GPX is
    megabytes, and a database is not a file system.
    """

    __tablename__ = "course_submissions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[SubmissionStatus] = mapped_column(
        pg_enum(SubmissionStatus, "submission_status"),
        nullable=False,
        default=SubmissionStatus.DRAFT,
        server_default=text("'draft'"),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    place: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str | None] = mapped_column(String(2))
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    distance_type: Mapped[DistanceType] = mapped_column(
        pg_enum(DistanceType, "distance_type"), nullable=False
    )
    lat: Mapped[float] = mapped_column(Numeric, nullable=False)
    lng: Mapped[float] = mapped_column(Numeric, nullable=False)
    #: The edition the athlete is entering. Not solved from — it becomes the
    #: default when they enter the race — but it is what makes the submission
    #: about a specific running of the event rather than about a venue.
    event_date: Mapped[date | None] = mapped_column(Date)
    start_time_local: Mapped[time | None] = mapped_column(Time)
    notes: Mapped[str | None] = mapped_column(Text)

    #: Storage keys for the three uploaded route files, one per leg.
    swim_file_key: Mapped[str | None] = mapped_column(Text)
    bike_file_key: Mapped[str | None] = mapped_column(Text)
    run_file_key: Mapped[str | None] = mapped_column(Text)
    #: The names the athlete's own files had, so an error can name the file
    #: they recognise rather than a storage key they have never seen.
    file_names: Mapped[JsonObject] = mapped_column(
        Json, nullable=False, server_default=text("'{}'")
    )

    #: Everything wrong with the last attempt, in the athlete's own terms.
    #: Emptied on a successful run rather than left behind, so a stale problem
    #: cannot be shown against a course that now works.
    problems: Mapped[JsonArray] = mapped_column(Json, nullable=False, server_default=text("'[]'"))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set once the bundle is built and loaded. Null until then.
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_course_submissions_user_id", "user_id"),
        Index("ix_course_submissions_status", "status"),
        CheckConstraint("lat BETWEEN -90 AND 90", name="course_submissions_lat_range"),
        CheckConstraint("lng BETWEEN -180 AND 180", name="course_submissions_lng_range"),
    )


class RaceWeekTask(Entity):
    """One dated, checkable thing to do before a race.

    **Generated and personal in the same table.** The four or five items every
    race has are derived from the event date (see
    :data:`~raceos.exports.files.RACE_WEEK_ITEMS`) and written once per race;
    anything the athlete adds sits beside them. Two tables would mean two
    queries, two orderings and two ways to tick something off, for a
    distinction the athlete does not have — to them it is one list.

    ``key`` is what makes regeneration safe. It is stable across a rebuild, so
    a task already ticked off stays ticked; the title and the date are not,
    because a course can be re-dated and the copy can be edited.
    """

    __tablename__ = "race_week_tasks"

    race_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("races.id", ondelete="CASCADE"), nullable=False
    )
    #: Denormalised from the race so a listing is one query and ownership is
    #: checkable without a join.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Stable identity. Generated items use their derivation's key; an
    #: athlete's own uses a generated one.
    key: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: False for anything the athlete added. Only generated rows are rebuilt
    #: when a race is re-dated, so a personal task is never silently moved.
    generated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("race_id", "key", name="uq_race_week_tasks_race_id_key"),
        Index("ix_race_week_tasks_user_id", "user_id"),
        Index("ix_race_week_tasks_race_id_due_date", "race_id", "due_date"),
    )


class CourseConditionsHistory(Entity):
    """What race day was actually like, one row per past edition.

    **Observed, not modelled and not invented.** Every value here is
    reanalysis from the weather archive for this course's coordinates on this
    date. The prototype's conditions panel quoted a median air temperature, a
    wetsuit likelihood and a finish-time distribution, and none of the three
    was backed by anything — they were written to look like data.

    ``water_temp_c`` is nullable and frequently null, deliberately. Sea-surface
    temperature is available for a coastal swim and not for a lake, and a lake
    course guessing at its own water temperature would be the invented number
    all over again. Where it is absent, the wetsuit likelihood is absent too
    rather than estimated from air temperature.

    There is no finish-time column. That needs actual results, which this
    system does not have and cannot obtain, so the recon page says so instead
    of drawing a distribution.
    """

    __tablename__ = "course_conditions_history"

    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    #: The date observed. Usually a past edition's race day; where the exact
    #: date is unknown it is the same calendar day in that year, which is what
    #: "what is it like then" actually asks.
    observed_on: Mapped[date] = mapped_column(Date, nullable=False)
    #: Local hour the observation is for — the race's start hour, so a 07:00
    #: start is not described by an afternoon high.
    observed_hour: Mapped[int] = mapped_column(Integer, nullable=False)

    air_temp_c: Mapped[float] = mapped_column(Numeric, nullable=False)
    humidity_pct: Mapped[float] = mapped_column(Numeric, nullable=False)
    wind_speed_ms: Mapped[float] = mapped_column(Numeric, nullable=False)
    wind_dir_deg: Mapped[float | None] = mapped_column(Numeric)
    precipitation_mm: Mapped[float | None] = mapped_column(Numeric)
    cloud_cover_pct: Mapped[float | None] = mapped_column(Numeric)
    #: Null for any course whose swim the marine archive does not cover.
    water_temp_c: Mapped[float | None] = mapped_column(Numeric)

    #: Which archive this came from, so a row can be traced and re-fetched.
    source: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "course_id", "observed_on", name="uq_course_conditions_history_course_date"
        ),
        Index("ix_course_conditions_history_course_id", "course_id"),
        CheckConstraint(
            "observed_hour BETWEEN 0 AND 23", name="course_conditions_history_hour_valid"
        ),
    )
