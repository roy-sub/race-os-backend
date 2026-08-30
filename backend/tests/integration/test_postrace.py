"""Post-race upload, analysis and calibration write-back.

Real files through the real decoder, a real solved plan, and the real
constraint write guard.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import (
    AnalysisCalibration,
    Constraint,
    Course,
    CourseBundle,
    PostRaceFile,
    Race,
    User,
)
from raceos.domain.enums import ConstraintSource, RaceFileStatus
from raceos.ingest.bundle_loader import load_bundle_file
from tests.integration.conftest import buy_plan

pytestmark = pytest.mark.integration

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"
TRAMUNTANA = BUNDLE_DIR / "tramuntana-full.bundle.json"

needs_bundle = pytest.mark.skipif(
    not TRAMUNTANA.is_file(), reason="generated bundles are git-ignored build artefacts"
)

ATHLETE_M = {
    "swim_threshold_pace": 105,
    "bike_threshold_power": 224,
    "run_threshold_pace": 282,
    "weight": 75,
    "sweat_rate": 1.1,
    "sodium_loss": 900,
    "gut_carb_ceiling": 75,
    "caffeine_tolerance": 300,
}


def _gpx_steady(duration_s: int, pace_s_per_km: float) -> bytes:
    """A steady run, as a GPX a watch would produce.

    Latitude only: 0.009° is almost exactly a kilometre, which keeps the
    derived distance readable in the assertions.
    """
    start = datetime(2026, 6, 21, 5, 0, tzinfo=UTC)
    rows = []
    lat = 39.7
    for index in range(0, duration_s + 1, 10):
        moment = (start + timedelta(seconds=index)).isoformat().replace("+00:00", "Z")
        rows.append(
            f'<trkpt lat="{lat:.6f}" lon="2.600000">'
            f"<ele>10.0</ele><time>{moment}</time></trkpt>"
        )
        lat += (10.0 / pace_s_per_km) * 0.009
    joined = "\n".join(rows)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="test" '
        'xmlns="http://www.topografix.com/GPX/1/1">\n'
        f"<trk><trkseg>\n{joined}\n</trkseg></trk></gpx>"
    ).encode()


@pytest.fixture
def raced(api: TestClient, signed_up, migrated_engine, api_db, paywall):
    """A season-tier athlete whose race has happened and whose plan is solved."""
    from sqlalchemy.orm import sessionmaker

    from raceos.db.models import Subscription
    from raceos.domain.enums import SubscriptionStatus, UserTier

    if not TRAMUNTANA.is_file():
        pytest.skip("generated bundles are git-ignored build artefacts")

    with sessionmaker(bind=migrated_engine)() as session:
        load_bundle_file(session, TRAMUNTANA)
        session.commit()
        course = session.scalar(select(Course).where(Course.slug == "tramuntana-full"))
        bundle = session.scalar(select(CourseBundle))
        course_id, bundle_id = course.id, bundle.id

    headers = signed_up["headers"]
    for key, value in ATHLETE_M.items():
        api.put(f"/api/v1/constraints/{key}", headers=headers, json={"value": value})

    user = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    race = Race(
        user_id=user.id,
        course_id=course_id,
        course_bundle_id=bundle_id,
        event_date=datetime.now(UTC).date() + timedelta(days=30),
        start_time_local=time(7, 0),
    )
    api_db.add(race)
    api_db.commit()

    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": str(race.id)})
    buy_plan(api, headers, draft.json()["id"])
    plan = api.post(f"/api/v1/plans/{draft.json()['id']}/solve", headers=headers, json={}).json()

    # Post-race analysis is a season-tier action.
    user.tier = UserTier.SEASON
    api_db.add(
        Subscription(user_id=user.id, tier=UserTier.SEASON, status=SubscriptionStatus.ACTIVE)
    )
    api_db.commit()

    return {
        "headers": headers,
        "user_id": user.id,
        "race_id": str(race.id),
        "plan": plan,
        "plan_id": plan["id"],
    }


def _upload(api, headers, data: bytes, filename: str, plan_id=None):
    files = {"file": (filename, data, "application/octet-stream")}
    payload = {"plan_id": str(plan_id)} if plan_id else {}
    return api.post("/api/v1/post-race/files", headers=headers, files=files, data=payload)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@needs_bundle
def test_a_valid_file_uploads_and_records_its_format(raced, api: TestClient) -> None:
    response = _upload(
        api, raced["headers"], _gpx_steady(40 * 60, 300.0), "race.gpx", raced["plan_id"]
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["format"] == "gpx"
    assert body["status"] == "pending"
    assert body["failure_reason"] is None


@needs_bundle
def test_an_unreadable_file_is_refused_with_a_reason_that_helps(
    raced, api: TestClient, api_db
) -> None:
    """ "Upload failed" is not a message anyone can act on."""
    response = _upload(api, raced["headers"], b"this is not a race file", "notes.txt")
    assert response.status_code == 422
    message = response.json()["error"]["message"]
    assert "FIT, GPX or TCX" in message
    assert "Export the activity again" in message

    # And the attempt is recorded, so support can see what was actually sent.
    record = api_db.scalar(select(PostRaceFile))
    assert record is not None
    assert record.status is RaceFileStatus.FAILED
    assert record.failure_reason


@needs_bundle
def test_a_route_file_is_told_it_is_a_route_not_an_activity(raced, api: TestClient) -> None:
    route = (
        b'<?xml version="1.0"?><gpx version="1.1" '
        b'xmlns="http://www.topografix.com/GPX/1/1">'
        b'<wpt lat="1" lon="2"><name>x</name></wpt></gpx>'
    )
    response = _upload(api, raced["headers"], route, "route.gpx")
    assert response.status_code == 422
    assert "route or waypoint file" in response.json()["error"]["message"]


@needs_bundle
def test_an_oversized_file_names_the_limit(raced, api: TestClient, api_settings) -> None:
    oversized = b"x" * (api_settings.upload_max_bytes + 1)
    response = _upload(api, raced["headers"], oversized, "huge.fit")
    assert response.status_code == 422
    assert "MB" in response.json()["error"]["message"]


@needs_bundle
def test_the_storage_key_is_random_not_the_filename(raced, api: TestClient, api_db) -> None:
    """The uploaded filename is untrusted input."""
    _upload(
        api,
        raced["headers"],
        _gpx_steady(40 * 60, 300.0),
        "../../etc/passwd.gpx",
        raced["plan_id"],
    )
    record = api_db.scalar(
        select(PostRaceFile).where(PostRaceFile.status == RaceFileStatus.PENDING)
    )
    assert record is not None
    assert ".." not in record.storage_key
    assert "passwd" not in record.storage_key
    assert record.original_filename == "../../etc/passwd.gpx"


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


@needs_bundle
def test_an_analysis_compares_against_the_version_that_was_live(raced, api: TestClient) -> None:
    """Not the current one: a re-solve after the race would silently judge the
    athlete against a plan they never raced."""
    headers = raced["headers"]
    file_id = _upload(
        api, headers, _gpx_steady(40 * 60, 300.0), "race.gpx", raced["plan_id"]
    ).json()["id"]

    analysis = api.post(
        "/api/v1/post-race/analyses",
        headers=headers,
        json={"race_file_id": file_id, "plan_id": raced["plan_id"]},
    )
    assert analysis.status_code == 200, analysis.text
    body = analysis.json()

    assert body["plan_version"] == raced["plan"]["version"]
    assert body["compare_rows"], "an analysis with no comparison is not an analysis"
    assert body["compare_rows"][0]["name"] == "Total time"

    # Re-solve, then confirm the stored analysis still points at the old one.
    api.post(f"/api/v1/plans/{raced['plan_id']}/resolve", headers=headers)
    refetched = api.get(f"/api/v1/post-race/analyses/{body['id']}", headers=headers).json()
    assert refetched["plan_version"] == raced["plan"]["version"]


@needs_bundle
def test_analysing_the_same_file_twice_returns_the_same_analysis(raced, api: TestClient) -> None:
    headers = raced["headers"]
    file_id = _upload(
        api, headers, _gpx_steady(40 * 60, 300.0), "race.gpx", raced["plan_id"]
    ).json()["id"]
    payload = {"race_file_id": file_id, "plan_id": raced["plan_id"]}

    first = api.post("/api/v1/post-race/analyses", headers=headers, json=payload).json()
    second = api.post("/api/v1/post-race/analyses", headers=headers, json=payload).json()
    assert first["id"] == second["id"]


@needs_bundle
def test_a_file_with_no_power_says_so_rather_than_comparing_nothing(raced, api: TestClient) -> None:
    headers = raced["headers"]
    file_id = _upload(
        api, headers, _gpx_steady(40 * 60, 300.0), "race.gpx", raced["plan_id"]
    ).json()["id"]
    body = api.post(
        "/api/v1/post-race/analyses",
        headers=headers,
        json={"race_file_id": file_id, "plan_id": raced["plan_id"]},
    ).json()

    names = {row["name"] for row in body["compare_rows"]}
    assert "Bike power" not in names
    assert any("Record power" in action["name"] for action in body["actions"])


@needs_bundle
def test_the_analysis_notifies_the_athlete(raced, api: TestClient) -> None:
    headers = raced["headers"]
    file_id = _upload(
        api, headers, _gpx_steady(40 * 60, 300.0), "race.gpx", raced["plan_id"]
    ).json()["id"]
    api.post(
        "/api/v1/post-race/analyses",
        headers=headers,
        json={"race_file_id": file_id, "plan_id": raced["plan_id"]},
    )

    inbox = api.get("/api/v1/notifications?type=analysis", headers=headers).json()
    assert inbox["total"] == 1
    assert inbox["data"][0]["cta_label"] == "See analysis"


# ---------------------------------------------------------------------------
# Calibration — SOLVER_MODEL.md §2.5.3
# ---------------------------------------------------------------------------


@needs_bundle
def test_a_qualifying_effort_produces_a_calibration_proposal(raced, api: TestClient) -> None:
    headers = raced["headers"]
    file_id = _upload(
        api, headers, _gpx_steady(40 * 60, 300.0), "race.gpx", raced["plan_id"]
    ).json()["id"]
    body = api.post(
        "/api/v1/post-race/analyses",
        headers=headers,
        json={"race_file_id": file_id, "plan_id": raced["plan_id"]},
    ).json()

    proposals = {c["constraint_key"]: c for c in body["calibrations"]}
    assert "run_threshold_pace" in proposals
    proposal = proposals["run_threshold_pace"]
    assert proposal["was"] == 282.0
    assert proposal["applied"] is False
    assert "pace variation" in proposal["evidence_text"]


@needs_bundle
def test_a_short_interval_derives_nothing_and_says_why(raced, api: TestClient) -> None:
    """§2.5.3 step 4. A `measured` stamp on a bad derivation is the worst
    thing this product can produce."""
    headers = raced["headers"]
    file_id = _upload(
        api, headers, _gpx_steady(12 * 60, 300.0), "interval.gpx", raced["plan_id"]
    ).json()["id"]
    body = api.post(
        "/api/v1/post-race/analyses",
        headers=headers,
        json={"race_file_id": file_id, "plan_id": raced["plan_id"]},
    ).json()

    assert body["calibrations"] == []
    reasons = [action["description"] for action in body["actions"]]
    assert any("No sustained effort" in reason for reason in reasons)


@needs_bundle
def test_applying_a_calibration_writes_a_measured_constraint(
    raced, api: TestClient, api_db
) -> None:
    headers = raced["headers"]
    file_id = _upload(
        api, headers, _gpx_steady(40 * 60, 300.0), "race.gpx", raced["plan_id"]
    ).json()["id"]
    body = api.post(
        "/api/v1/post-race/analyses",
        headers=headers,
        json={"race_file_id": file_id, "plan_id": raced["plan_id"]},
    ).json()
    calibration_id = body["calibrations"][0]["id"]

    applied = api.post(f"/api/v1/post-race/calibrations/{calibration_id}/apply", headers=headers)
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True

    constraint = api_db.scalar(
        select(Constraint).where(
            Constraint.user_id == raced["user_id"],
            Constraint.key == "run_threshold_pace",
        )
    )
    api_db.refresh(constraint)
    assert constraint.source is ConstraintSource.MEASURED
    assert float(constraint.value) == applied.json()["now"]


@needs_bundle
def test_a_calibration_cannot_be_applied_twice(raced, api: TestClient) -> None:
    headers = raced["headers"]
    file_id = _upload(
        api, headers, _gpx_steady(40 * 60, 300.0), "race.gpx", raced["plan_id"]
    ).json()["id"]
    body = api.post(
        "/api/v1/post-race/analyses",
        headers=headers,
        json={"race_file_id": file_id, "plan_id": raced["plan_id"]},
    ).json()
    calibration_id = body["calibrations"][0]["id"]

    api.post(f"/api/v1/post-race/calibrations/{calibration_id}/apply", headers=headers)
    again = api.post(f"/api/v1/post-race/calibrations/{calibration_id}/apply", headers=headers)
    assert again.status_code == 409


@needs_bundle
def test_a_dismissed_calibration_writes_nothing(raced, api: TestClient, api_db) -> None:
    headers = raced["headers"]
    file_id = _upload(
        api, headers, _gpx_steady(40 * 60, 300.0), "race.gpx", raced["plan_id"]
    ).json()["id"]
    body = api.post(
        "/api/v1/post-race/analyses",
        headers=headers,
        json={"race_file_id": file_id, "plan_id": raced["plan_id"]},
    ).json()
    calibration_id = body["calibrations"][0]["id"]

    dismissed = api.post(
        f"/api/v1/post-race/calibrations/{calibration_id}/dismiss", headers=headers
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["dismissed_at"] is not None

    constraint = api_db.scalar(
        select(Constraint).where(
            Constraint.user_id == raced["user_id"],
            Constraint.key == "run_threshold_pace",
        )
    )
    api_db.refresh(constraint)
    assert float(constraint.value) == 282.0
    assert constraint.source is not ConstraintSource.MEASURED


@needs_bundle
def test_calibration_goes_through_the_constraint_write_guard(
    raced, api: TestClient, api_db
) -> None:
    """Structural guarantee 1, reached from the calibration path.

    The service is called directly with a *different* user as the actor: not
    even an internal caller can write a constraint on someone's behalf.
    """
    from raceos.api.errors import ForbiddenStructural
    from raceos.services import postrace_service

    headers = raced["headers"]
    file_id = _upload(
        api, headers, _gpx_steady(40 * 60, 300.0), "race.gpx", raced["plan_id"]
    ).json()["id"]
    body = api.post(
        "/api/v1/post-race/analyses",
        headers=headers,
        json={"race_file_id": file_id, "plan_id": raced["plan_id"]},
    ).json()

    calibration = api_db.get(AnalysisCalibration, UUID(body["calibrations"][0]["id"]))
    other = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "coach@example.com",
            "password": "correct-horse-battery",
            "name": "Coach",
        },
    ).json()
    coach = api_db.scalar(select(User).where(User.email == "coach@example.com"))
    assert other["user"]["id"]

    with pytest.raises((ForbiddenStructural, Exception)) as error:
        postrace_service.apply_calibration(api_db, calibration=calibration, user=coach)
    assert error.value is not None


# ---------------------------------------------------------------------------
# Entitlement and authorization
# ---------------------------------------------------------------------------


@needs_bundle
def test_post_race_analysis_is_a_season_tier_action(raced, api: TestClient, api_db) -> None:
    from raceos.db.models import Subscription
    from raceos.domain.enums import UserTier

    headers = raced["headers"]
    file_id = _upload(
        api, headers, _gpx_steady(40 * 60, 300.0), "race.gpx", raced["plan_id"]
    ).json()["id"]

    # Drop back to the per-race tier and cancel the subscription.
    user = api_db.get(User, raced["user_id"])
    user.tier = UserTier.PER_RACE
    for subscription in api_db.scalars(select(Subscription)):
        api_db.delete(subscription)
    api_db.commit()

    response = api.post(
        "/api/v1/post-race/analyses",
        headers=headers,
        json={"race_file_id": file_id, "plan_id": raced["plan_id"]},
    )
    assert response.status_code == 402
    assert "season" in response.json()["error"]["details"]["required_tiers"]


@needs_bundle
def test_nobody_reads_another_athletes_analysis(raced, api: TestClient) -> None:
    headers = raced["headers"]
    file_id = _upload(
        api, headers, _gpx_steady(40 * 60, 300.0), "race.gpx", raced["plan_id"]
    ).json()["id"]
    analysis_id = api.post(
        "/api/v1/post-race/analyses",
        headers=headers,
        json={"race_file_id": file_id, "plan_id": raced["plan_id"]},
    ).json()["id"]

    other = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "jonas.feldt@example.com",
            "password": "correct-horse-battery",
            "name": "Jonas Feldt",
        },
    ).json()
    intruder = {"Authorization": f"Bearer {other['access_token']}"}

    assert api.get(f"/api/v1/post-race/analyses/{analysis_id}", headers=intruder).status_code == 404
    assert api.get("/api/v1/post-race/analyses", headers=intruder).json() == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/post-race/files"),
        ("POST", "/api/v1/post-race/analyses"),
        ("GET", "/api/v1/post-race/analyses"),
        ("GET", f"/api/v1/post-race/analyses/{UUID(int=0)}"),
        ("POST", f"/api/v1/post-race/calibrations/{UUID(int=0)}/apply"),
        ("POST", f"/api/v1/post-race/calibrations/{UUID(int=0)}/dismiss"),
    ],
)
def test_every_post_race_endpoint_rejects_an_absent_token(
    api: TestClient, method: str, path: str
) -> None:
    assert api.request(method, path, json={}).status_code == 401
