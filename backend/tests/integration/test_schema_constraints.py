"""The schema's constraints must actually bite.

A CHECK constraint nobody has tried to violate is a comment with extra steps.
Each test here attempts the thing the constraint forbids and asserts the
database refuses it, so the invariants survive a future migration written by
someone who has not read the specification.

The three structural guarantees appear here in their *schema* form. Their
service- and endpoint-level forms are proven in the security suite; this is
the layer underneath — the one that means there is no column to misuse.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from raceos.db.models import (
    Course,
    CourseBundle,
    CourseBundleLeg,
    Plan,
    PlanBag,
    PlanBagItem,
    PlanSegment,
    Race,
    ShareLink,
    User,
)
from raceos.domain.enums import (
    BagKey,
    Difficulty,
    DistanceType,
    Leg,
    PlanStatus,
    ShareScope,
    SurfaceQuality,
)

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_user(db: Session, email: str | None = None) -> User:
    user = User(email=email or f"{uuid.uuid4().hex}@example.test", name="Test Athlete")
    db.add(user)
    db.flush()
    return user


def make_bundle(db: Session, **overrides: object) -> CourseBundle:
    course = Course(
        name="Test Course",
        place="Nowhere",
        slug=f"test-{uuid.uuid4().hex[:8]}",
        distance_type=DistanceType.FULL,
        difficulty=Difficulty.MODERATE,
        timezone="Europe/Madrid",
        lat=39.85,
        lng=3.12,
    )
    db.add(course)
    db.flush()

    fields: dict[str, object] = {
        "course_id": course.id,
        "version": "v2026.1",
        "attribution": "© OpenStreetMap contributors, ODbL 1.0",
        "barriers": [{"name": "finish", "leg": "RUN", "limit_minutes_from_start": 960, "km": 42.2}],
    }
    fields.update(overrides)
    bundle = CourseBundle(**fields)  # type: ignore[arg-type]
    db.add(bundle)
    db.flush()
    return bundle


def make_plan(db: Session, *, status: PlanStatus = PlanStatus.DRAFT, version: int = 1) -> Plan:
    user = make_user(db)
    bundle = make_bundle(db)
    race = Race(
        user_id=user.id,
        course_id=bundle.course_id,
        course_bundle_id=bundle.id,
        event_date=date(2026, 9, 19),
        start_time_local=time(7, 0),
    )
    db.add(race)
    db.flush()
    plan = Plan(race_id=race.id, user_id=user.id, status=status, version=version)
    db.add(plan)
    db.flush()
    return plan


# ---------------------------------------------------------------------------
# Structural guarantee 1 — a coach can never write an athlete's constraints
# ---------------------------------------------------------------------------


def test_coach_link_has_no_constraints_permission_column(migrated_engine) -> None:
    """The guarantee at its foundation: there is no column to flip.

    A permission that does not exist cannot be granted by a bug, a migration,
    an admin tool or a bulk script.
    """
    columns = {c["name"] for c in inspect(migrated_engine).get_columns("coach_athlete_links")}
    assert {"perm_plans", "perm_build", "perm_analysis"} <= columns
    forbidden = {name for name in columns if "constraint" in name.lower()}
    assert not forbidden, (
        f"coach_athlete_links has constraint-related column(s) {forbidden}. "
        f"A coach writing an athlete's constraints must be structurally "
        f"impossible, not a boolean that could be flipped true."
    )


def test_constraints_table_has_no_coach_or_admin_writer_column(migrated_engine) -> None:
    """Nor is there a column recording a coach as the author of a value."""
    columns = {c["name"] for c in inspect(migrated_engine).get_columns("constraints")}
    for forbidden in ("coach_id", "written_by_coach_id", "updated_by_coach_id", "admin_override"):
        assert (
            forbidden not in columns
        ), f"constraints.{forbidden} exists; there must be no coach write path at all."


# ---------------------------------------------------------------------------
# Structural guarantee 2 — share links always expire
# ---------------------------------------------------------------------------


def test_share_link_requires_an_expiry(db: Session) -> None:
    """A non-expiring share link cannot be created, even by direct insert."""
    plan = make_plan(db)
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO share_links "
                "(plan_id, token_hash, token_prefix, scope, created_by, expires_at) "
                "VALUES (:plan_id, :hash, :prefix, 'full_plan', :user_id, NULL)"
            ),
            {
                "plan_id": plan.id,
                "hash": uuid.uuid4().hex,
                "prefix": "abc123",
                "user_id": plan.user_id,
            },
        )


def test_share_link_with_expiry_is_accepted(db: Session) -> None:
    plan = make_plan(db)
    link = ShareLink(
        plan_id=plan.id,
        token_hash=uuid.uuid4().hex,
        token_prefix="abc123",
        scope=ShareScope.FULL_PLAN,
        created_by=plan.user_id,
        expires_at=NOW + timedelta(days=7),
    )
    db.add(link)
    db.flush()
    assert link.opens_count == 0


# ---------------------------------------------------------------------------
# Structural guarantee 3 — a granted support grant always has an expiry
# ---------------------------------------------------------------------------


def test_granted_support_access_must_have_an_expiry(db: Session) -> None:
    """A grant without an expiry would be permanent access, silently."""
    athlete = make_user(db)
    agent = make_user(db)
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO support_access_grants "
                "(athlete_id, support_agent_id, requested_at, granted_at, expires_at) "
                "VALUES (:athlete, :agent, now(), now(), NULL)"
            ),
            {"athlete": athlete.id, "agent": agent.id},
        )


# ---------------------------------------------------------------------------
# Bundle invariants (SOLVER_MODEL.md §1.2)
# ---------------------------------------------------------------------------


def test_bundle_with_zero_barriers_is_unstorable(db: Session) -> None:
    """§1.2: zero barriers is a data error, not a solvable plan.

    Enforced here rather than only at solve time, because a solve-time
    rejection lands on an athlete hours later instead of on the admin who
    published it.
    """
    with pytest.raises(IntegrityError):
        make_bundle(db, barriers=[])


def test_bundle_with_non_terrain_elevation_is_unstorable(db: Session) -> None:
    """§1.2: elevation is terrain-sampled, never barometric or GPS."""
    with pytest.raises(IntegrityError):
        make_bundle(db, elevation_source="barometric")


def test_bundle_without_attribution_is_unstorable(db: Session) -> None:
    """ODbL obliges attribution wherever derived data is displayed."""
    with pytest.raises(IntegrityError):
        make_bundle(db, attribution="")


def test_bundle_leg_geometry_round_trips_with_its_z_ordinate(db: Session) -> None:
    """The Z ordinate is the elevation series the solver reads.

    If Z were silently dropped by the column type, every gradient would be
    zero and every plan would be wrong in a way no other test would notice.
    """
    bundle = make_bundle(db)
    leg = CourseBundleLeg(
        bundle_id=bundle.id,
        leg=Leg.BIKE,
        geometry="SRID=4326;LINESTRING Z (3.121 39.840 3.17, 3.122 39.841 12.5, 3.123 39.842 31.0)",
        distance_m=250.0,
        elevation_gain_m=27.83,
        node_count=3,
        surface_quality=SurfaceQuality.TYPICAL_ROAD,
    )
    db.add(leg)
    db.flush()

    z_values = db.execute(
        text(
            "SELECT ST_Z(ST_PointN(geometry, 1)), ST_Z(ST_PointN(geometry, 3)) "
            "FROM course_bundle_legs WHERE id = :id"
        ),
        {"id": leg.id},
    ).one()
    assert z_values[0] == pytest.approx(3.17)
    assert z_values[1] == pytest.approx(31.0)


# ---------------------------------------------------------------------------
# Plan invariants
# ---------------------------------------------------------------------------


def test_only_one_active_plan_version_per_race(db: Session) -> None:
    """The partial unique index, doing the job a service check could forget."""
    plan = make_plan(db, status=PlanStatus.ACTIVE, version=1)
    duplicate = Plan(
        race_id=plan.race_id, user_id=plan.user_id, status=PlanStatus.ACTIVE, version=2
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.flush()


def test_many_past_versions_per_race_are_allowed(db: Session) -> None:
    """Superseded versions stay readable forever; only `active` is unique.

    Post-race comparison must reference the version live at race time, so
    history cannot be pruned.
    """
    plan = make_plan(db, status=PlanStatus.PAST, version=1)
    for version in (2, 3):
        db.add(
            Plan(
                race_id=plan.race_id,
                user_id=plan.user_id,
                status=PlanStatus.PAST,
                version=version,
            )
        )
    db.flush()
    count = db.execute(
        text("SELECT count(*) FROM plans WHERE race_id = :race_id"), {"race_id": plan.race_id}
    ).scalar_one()
    assert count == 3


def test_plan_version_is_unique_per_race(db: Session) -> None:
    plan = make_plan(db, version=1)
    db.add(Plan(race_id=plan.race_id, user_id=plan.user_id, version=1))
    with pytest.raises(IntegrityError):
        db.flush()


def test_generated_bag_item_must_carry_a_reason(db: Session) -> None:
    """§6.1: an item with no upstream justification cannot be emitted.

    This is what makes "Why this?" work on a bag item exactly as it works on a
    wattage target.
    """
    plan = make_plan(db)
    bag = PlanBag(
        plan_id=plan.id, key=BagKey.BIKE_SN, name="Bike Special Needs", when_label="km 90"
    )
    db.add(bag)
    db.flush()

    db.add(PlanBagItem(bag_id=bag.id, ordinal=1, name="Salt capsules", qty="13"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_user_added_bag_item_may_omit_a_reason(db: Session) -> None:
    """The athlete's own additions have no upstream justification, by definition."""
    plan = make_plan(db)
    bag = PlanBag(plan_id=plan.id, key=BagKey.MORNING, name="Morning", when_label="race morning")
    db.add(bag)
    db.flush()
    db.add(PlanBagItem(bag_id=bag.id, ordinal=1, name="Lucky socks", is_user_added=True))
    db.flush()


def test_segment_split_time_must_be_positive(db: Session) -> None:
    """§4.3.3: never emit a negative or zero split time."""
    plan = make_plan(db)
    db.add(
        PlanSegment(
            plan_id=plan.id,
            ordinal=1,
            name="Coll de Femenia",
            leg=Leg.BIKE,
            from_km=51.6,
            to_km=60.0,
            target_minutes=0,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_segment_km_range_must_be_ordered(db: Session) -> None:
    plan = make_plan(db)
    db.add(
        PlanSegment(
            plan_id=plan.id,
            ordinal=1,
            name="Backwards",
            leg=Leg.BIKE,
            from_km=60.0,
            to_km=51.6,
            target_minutes=47.3,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


# ---------------------------------------------------------------------------
# General schema properties
# ---------------------------------------------------------------------------


def test_email_uniqueness_is_case_insensitive(db: Session) -> None:
    """citext: `A@example.test` and `a@example.test` are the same account."""
    make_user(db, email="Elena.Marsh@example.test")
    db.add(User(email="elena.marsh@example.test"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_updated_at_is_maintained_by_a_trigger(migrated_engine) -> None:
    """Not by the ORM: a row touched by a migration must be stamped too.

    This test commits, twice, rather than using the rolled-back session
    fixture. `now()` in PostgreSQL is the *transaction* timestamp, so an
    INSERT and an UPDATE inside one transaction share it by design — and a
    single-transaction test would therefore pass whether the trigger existed
    or not. Two transactions is the only way to observe the trigger working.

    Transaction time is the right semantics here, not `clock_timestamp()`:
    rows changed together in one transaction changed at one moment, and an
    audit trail that says otherwise is harder to reason about.
    """
    user_id = uuid.uuid4()
    with migrated_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": user_id, "email": f"{user_id.hex}@example.test"},
        )
        original = connection.execute(
            text("SELECT updated_at FROM users WHERE id = :id"), {"id": user_id}
        ).scalar_one()

    try:
        with migrated_engine.begin() as connection:
            connection.execute(
                text("UPDATE users SET name = 'Renamed' WHERE id = :id"), {"id": user_id}
            )
            refreshed = connection.execute(
                text("SELECT updated_at FROM users WHERE id = :id"), {"id": user_id}
            ).scalar_one()
        assert refreshed > original
    finally:
        with migrated_engine.begin() as connection:
            connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


def test_postgis_is_available(migrated_engine) -> None:
    version = (
        migrated_engine.connect()
        .execute(text("SELECT extversion FROM pg_extension WHERE extname = 'postgis'"))
        .scalar_one_or_none()
    )
    assert version is not None, "PostGIS must be enabled; every geometry column needs it"


def test_enum_labels_are_values_not_python_names(migrated_engine) -> None:
    """`70.3` must reach the database as `70.3`, not as `HALF`.

    Storing member names would silently diverge from what the frontend sends
    and from SOLVER_MODEL.md's vocabulary, and nothing else would catch it.
    """
    labels = [
        row[0]
        for row in migrated_engine.connect().execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'distance_type' ORDER BY e.enumsortorder"
            )
        )
    ]
    assert labels == ["Sprint", "Olympic", "70.3", "Full"]


def test_solver_distance_vocabulary_is_separate(migrated_engine) -> None:
    """The solver's vocabulary is not a database enum at all.

    It lives in `solver/tables/`, keyed by SolverDistance. Keeping it out of
    the schema is what stops the two vocabularies being conflated.
    """
    enum_names = {
        row[0]
        for row in migrated_engine.connect().execute(
            text("SELECT DISTINCT typname FROM pg_type WHERE typtype = 'e'")
        )
    }
    assert "distance_type" in enum_names
    assert "solver_distance" not in enum_names
