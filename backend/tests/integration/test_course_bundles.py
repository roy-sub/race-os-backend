"""The three generated bundles load, and serve correctly with attribution.

These tests read the **real** artefacts in
``pipelines/course-ingest/out/bundles/``, not fixtures written to match. That
is the point: the loader's contract is with what the pipeline actually emits,
and a test against a hand-written stand-in would keep passing after the two
diverged.

They skip rather than fail when the bundles are absent, because ``out/`` is
git-ignored — it holds build artefacts that belong in object storage. A
checkout without them is a valid checkout.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from raceos.api.main import create_app
from raceos.config import Settings
from raceos.db.models import Course, CourseBundle, CourseBundleLeg
from raceos.domain.enums import DistanceType, Leg, Provenance, SurfaceQuality
from raceos.ingest.bundle_loader import (
    BundleValidationError,
    load_bundle_directory,
    load_bundle_file,
    validate_bundle,
)

pytestmark = pytest.mark.integration

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"

#: The three courses the pipeline generates. Six more specs are marked
#: `status: pending` and are deliberately not built, so the directory shows
#: three — that is expected, not a gap in this test.
EXPECTED_SLUGS = {"tramuntana-full", "kalmar-703", "skagen-703"}

needs_bundles = pytest.mark.skipif(
    not BUNDLE_DIR.is_dir() or not list(BUNDLE_DIR.glob("*.bundle.json")),
    reason="generated bundles are git-ignored build artefacts; none in this checkout",
)


def _payload(slug: str) -> dict[str, Any]:
    return json.loads((BUNDLE_DIR / f"{slug}.bundle.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@needs_bundles
def test_every_generated_bundle_validates() -> None:
    """If this ever fails, the pipeline and the loader have diverged."""
    failures: dict[str, list[str]] = {}
    for path in sorted(BUNDLE_DIR.glob("*.bundle.json")):
        problems = validate_bundle(json.loads(path.read_text(encoding="utf-8")), path.name)
        if problems:
            failures[path.name] = problems
    assert not failures, f"bundles failed validation: {failures}"


@needs_bundles
def test_all_three_bundles_load(db: Session) -> None:
    results = load_bundle_directory(db, BUNDLE_DIR)
    assert {r.slug for r in results} == EXPECTED_SLUGS
    assert all(r.created for r in results)
    assert all(r.legs == 3 for r in results), "every bundle needs SWIM, BIKE and RUN"
    assert all(r.barriers > 0 for r in results), "§1.2: zero barriers is a data error"


@needs_bundles
def test_loading_is_idempotent(db: Session) -> None:
    """`make seed` must be re-runnable without duplicating anything."""
    first = load_bundle_directory(db, BUNDLE_DIR)
    second = load_bundle_directory(db, BUNDLE_DIR)

    assert all(r.created for r in first)
    assert not any(r.created for r in second), "a second load must update, not insert"
    assert {r.course_id for r in first} == {r.course_id for r in second}
    assert {r.bundle_id for r in first} == {r.bundle_id for r in second}

    assert db.scalar(select(text("count(*)")).select_from(Course)) == 3
    assert db.scalar(select(text("count(*)")).select_from(CourseBundle)) == 3
    assert db.scalar(select(text("count(*)")).select_from(CourseBundleLeg)) == 9


@needs_bundles
def test_tramuntana_loads_with_its_real_geometry(db: Session) -> None:
    """Spot-check the flagship course against the artefact's own numbers."""
    load_bundle_file(db, BUNDLE_DIR / "tramuntana-full.bundle.json")
    payload = _payload("tramuntana-full")

    course = db.scalar(select(Course).where(Course.slug == "tramuntana-full"))
    assert course is not None
    assert course.name == "Tramuntana Full"
    assert course.distance_type is DistanceType.FULL
    assert course.is_fictional is True, "the races are invented; only the terrain is real"

    bundle = db.scalar(select(CourseBundle).where(CourseBundle.course_id == course.id))
    assert bundle is not None
    assert bundle.provenance is Provenance.ESTIMATED
    assert bundle.elevation_source == "terrain"
    assert "OpenStreetMap" in bundle.attribution
    assert len(bundle.segments) == len(payload["course_bundle"]["segments"])

    bike = db.scalar(
        select(CourseBundleLeg).where(
            CourseBundleLeg.bundle_id == bundle.id, CourseBundleLeg.leg == Leg.BIKE
        )
    )
    assert bike is not None
    expected = next(leg for leg in payload["course_bundle_legs"] if leg["leg"] == "BIKE")
    assert float(bike.distance_m) == pytest.approx(expected["distance_m"])
    assert bike.node_count == expected["node_count"]
    assert bike.surface_quality is SurfaceQuality(expected["surface_quality"])


@needs_bundles
def test_the_z_ordinate_survives_the_round_trip(db: Session) -> None:
    """The Z ordinate *is* the elevation series the solver reads.

    If the geometry column silently dropped Z, every gradient would be zero,
    every plan would be wrong, and nothing else in the suite would notice.
    """
    load_bundle_file(db, BUNDLE_DIR / "tramuntana-full.bundle.json")
    bike = db.scalar(
        select(CourseBundleLeg)
        .join(CourseBundle)
        .join(Course)
        .where(Course.slug == "tramuntana-full", CourseBundleLeg.leg == Leg.BIKE)
    )
    assert bike is not None

    row = db.execute(
        text(
            "SELECT ST_NPoints(geometry), "
            "       MIN(ST_Z(pt.geom)), MAX(ST_Z(pt.geom)) "
            "FROM course_bundle_legs, "
            "     LATERAL ST_DumpPoints(geometry) AS pt "
            "WHERE course_bundle_legs.id = :id "
            "GROUP BY geometry"
        ),
        {"id": bike.id},
    ).one()
    npoints, min_z, max_z = row
    assert npoints == bike.node_count
    # Real Mallorcan terrain: sea level to a few hundred metres. A leg whose
    # Z range collapsed to zero would pass every other assertion here.
    assert max_z - min_z > 100, "the bike leg must carry real elevation variation"


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------


@needs_bundles
@pytest.mark.parametrize(
    ("mutate", "expected_fragment"),
    [
        (lambda p: p["course_bundle"].update(barriers=[]), "zero barriers"),
        (lambda p: p["course_bundle"].update(elevation_source="gps"), "terrain-sampled"),
        (lambda p: p["course_bundle"].update(attribution="  "), "attribution"),
        (lambda p: p["course_bundle_legs"].pop(), "missing leg"),
        (
            lambda p: p["course_bundle_legs"][0].update(surface_quality="gravel"),
            "unmapped",
        ),
        (
            lambda p: p["course_bundle"]["barriers"].reverse(),
            "chronologically ordered",
        ),
    ],
)
def test_a_broken_bundle_is_rejected_before_anything_is_written(
    db: Session, mutate, expected_fragment: str
) -> None:
    """Rejection must happen at load time, not at solve time.

    A bad bundle caught here costs an admin a minute. The same bundle caught
    at solve time costs an athlete their plan, hours later, with an error
    that names a barrier rather than the bundle.
    """
    payload = _payload("kalmar-703")
    mutate(payload)

    from raceos.ingest.bundle_loader import load_bundle_payload

    with pytest.raises(BundleValidationError) as excinfo:
        load_bundle_payload(db, payload, source="mutated")

    assert any(
        expected_fragment in problem for problem in excinfo.value.problems
    ), f"expected a problem mentioning {expected_fragment!r}, got {excinfo.value.problems}"
    # Nothing was written.
    assert db.scalar(select(text("count(*)")).select_from(Course)) == 0


@needs_bundles
def test_validation_reports_every_problem_not_just_the_first(db: Session) -> None:
    """An admin fixing one problem at a time is an admin fixing it four times."""
    payload = _payload("kalmar-703")
    payload["course_bundle"]["barriers"] = []
    payload["course_bundle"]["attribution"] = ""
    payload["course_bundle"]["elevation_source"] = "gps"

    problems = validate_bundle(payload, "mutated")
    assert len(problems) >= 3


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


@pytest.fixture
def client(migrated_engine, settings: Settings) -> TestClient:
    from raceos.db import session as session_module

    session_module.reset_engine()
    session_module._engine = migrated_engine
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
    session_module.reset_engine()


@pytest.fixture
def seeded(migrated_engine) -> None:
    """Commit the three bundles so the API's own session can see them.

    The skip lives inside the fixture rather than as a mark on it: pytest
    ignores marks applied to fixtures, so a decorator here would silently do
    nothing and every dependent test would fail on an empty directory instead
    of skipping.
    """
    from sqlalchemy.orm import sessionmaker

    if not BUNDLE_DIR.is_dir() or not list(BUNDLE_DIR.glob("*.bundle.json")):
        pytest.skip("generated bundles are git-ignored build artefacts")

    factory = sessionmaker(bind=migrated_engine)
    with factory() as session:
        session.execute(text("DELETE FROM course_bundle_legs"))
        session.execute(text("DELETE FROM course_bundles"))
        session.execute(text("DELETE FROM courses"))
        load_bundle_directory(session, BUNDLE_DIR)
        session.commit()
    yield
    with factory() as session:
        session.execute(text("DELETE FROM course_bundle_legs"))
        session.execute(text("DELETE FROM course_bundles"))
        session.execute(text("DELETE FROM courses"))
        session.commit()


@needs_bundles
def test_directory_lists_the_seeded_courses(client: TestClient, seeded: None) -> None:
    response = client.get("/api/v1/courses")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] == 3
    assert {row["slug"] for row in body["data"]} == EXPECTED_SLUGS


@needs_bundles
def test_directory_filters_by_distance(client: TestClient, seeded: None) -> None:
    response = client.get("/api/v1/courses", params={"dist": "70.3"})
    slugs = {row["slug"] for row in response.json()["data"]}
    assert slugs == {"kalmar-703", "skagen-703"}


@needs_bundles
def test_directory_carries_a_real_cutoff_not_a_stored_label(
    client: TestClient, seeded: None
) -> None:
    """`cutoff_minutes` is derived from the bundle's barriers.

    The frontend's `cutoff: "10:30 bike"` is rendered from this. Storing the
    string would let a bundle's cut-off move and leave a stale label behind.
    """
    rows = {r["slug"]: r for r in client.get("/api/v1/courses").json()["data"]}
    tramuntana = rows["tramuntana-full"]
    assert tramuntana["cutoff_barrier_name"] == "bike_cutoff"
    assert tramuntana["cutoff_minutes"] == 630.0


@needs_bundles
def test_bundle_response_carries_attribution(client: TestClient, seeded: None) -> None:
    """ODbL obliges attribution wherever the derived data is displayed.

    Handing it with the geometry is what makes that structural: a client
    cannot render what it was not given.
    """
    response = client.get("/api/v1/courses/tramuntana-full/bundle")
    assert response.status_code == 200
    body = response.json()
    assert "OpenStreetMap" in body["attribution"]
    assert body["elevation_source"] == "terrain"
    assert len(body["legs"]) == 3
    assert body["barriers"]
    assert body["aid_stations"]
    assert body["segments"]


@needs_bundles
def test_bundle_legs_are_in_solver_order(client: TestClient, seeded: None) -> None:
    """SWIM, BIKE, RUN — the fixed order the solver accumulates them in."""
    legs = client.get("/api/v1/courses/tramuntana-full/bundle").json()["legs"]
    assert [leg["leg"] for leg in legs] == ["SWIM", "BIKE", "RUN"]


@needs_bundles
def test_bundle_serves_an_etag(client: TestClient, seeded: None) -> None:
    response = client.get("/api/v1/courses/kalmar-703/bundle")
    assert response.headers["ETag"].strip('"').endswith(":v2026.1")


@needs_bundles
def test_course_detail_resolves_by_slug_and_by_id(client: TestClient, seeded: None) -> None:
    by_slug = client.get("/api/v1/courses/kalmar-703").json()
    by_id = client.get(f"/api/v1/courses/{by_slug['id']}").json()
    assert by_slug == by_id


@needs_bundles
def test_crowd_provenance_is_serialised_as_the_frontend_spells_it(
    client: TestClient, seeded: None
) -> None:
    """Stored `CROWD`, served `CROWD-VERIFIED`.

    The build spec's DDL says `CROWD`; the frontend's Provenance union says
    `CROWD-VERIFIED`. Both are authoritative in their own layer, so the value
    is translated at the boundary. See docs/FIELD_NAME_RECONCILIATION.md R-005.
    """
    from raceos.api.schemas.course import PROVENANCE_DISPLAY

    assert PROVENANCE_DISPLAY[Provenance.CROWD] == "CROWD-VERIFIED"
    assert PROVENANCE_DISPLAY[Provenance.OFFICIAL] == "OFFICIAL"
    assert PROVENANCE_DISPLAY[Provenance.ESTIMATED] == "ESTIMATED"
    # The seeded bundles are all ESTIMATED, honestly.
    assert client.get("/api/v1/courses/skagen-703").json()["provenance"] == "ESTIMATED"


def test_unknown_course_is_a_clean_404(client: TestClient) -> None:
    response = client.get("/api/v1/courses/no-such-course")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_auth_providers_is_empty(client: TestClient) -> None:
    """No OAuth is built. The frontend hides the buttons rather than rendering
    dead ones, and there is no provider code behind this to describe."""
    assert client.get("/api/v1/auth/providers").json() == {"providers": []}
