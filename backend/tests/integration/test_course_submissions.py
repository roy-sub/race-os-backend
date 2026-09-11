"""Athlete-submitted courses, end to end through the API.

The promise being tested is narrow: **an athlete racing an event we have not
built is not blocked on our release schedule.** What comes out is a real course
with a real bundle, held to the same standard as the official calendar — and
private to the athlete who added it, because their file may be an organiser's
licensed course data and republishing it is not ours to do.

The elevation source is injected so this whole path runs offline. It is a real
implementation (`ConstantElevation`), not a mock: the same discipline payments
and storage already follow.
"""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import Course, CourseSubmission
from raceos.domain.enums import DistanceType, Leg, SubmissionStatus
from raceos.ingest.elevation import ConstantElevation
from raceos.services import submission_service

pytestmark = pytest.mark.integration


def gpx_of(points: list[tuple[float, float]]) -> bytes:
    body = "".join(f'<trkpt lat="{lat}" lon="{lng}"><ele>999</ele></trkpt>' for lng, lat in points)
    return (
        '<?xml version="1.0"?><gpx version="1.1" creator="test">'
        f"<trk><trkseg>{body}</trkseg></trk></gpx>"
    ).encode()


def straight(metres: float, bearing_deg: float, nodes: int = 400) -> list[tuple[float, float]]:
    lat0, lng0 = 44.26, 12.36
    bearing = math.radians(bearing_deg)
    out = []
    for index in range(nodes + 1):
        distance = metres * index / nodes
        dlat = distance * math.cos(bearing) / 111_320.0
        dlng = distance * math.sin(bearing) / (111_320.0 * math.cos(math.radians(lat0)))
        out.append((lng0 + dlng, lat0 + dlat))
    return out


HALF_LEGS = {
    "SWIM": ("swim.gpx", gpx_of(straight(1900, 90))),
    "BIKE": ("bike.gpx", gpx_of(straight(90_000, 45, 2000))),
    "RUN": ("run.gpx", gpx_of(straight(21_100, 200, 800))),
}

DETAILS = {
    "name": "IRONMAN 70.3 Somewhere",
    "place": "Somewhere, Italy",
    "country": "IT",
    "timezone": "Europe/Rome",
    "distance_type": "70.3",
    "lat": 44.26,
    "lng": 12.36,
    "event_date": "2027-05-30",
}


def create(api: TestClient, headers: dict, **overrides) -> dict:
    response = api.post(
        "/api/v1/course-submissions", headers=headers, json={**DETAILS, **overrides}
    )
    assert response.status_code == 201, response.text
    return response.json()


def upload(api: TestClient, headers: dict, submission_id: str, leg: str) -> dict:
    filename, data = HALF_LEGS[leg]
    response = api.put(
        f"/api/v1/course-submissions/{submission_id}/files/{leg}",
        headers=headers,
        files={"file": (filename, data, "application/gpx+xml")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def build(api_db, headers_user_id, submission_id, settings) -> CourseSubmission:
    """Run the build with an offline elevation source."""
    submission = api_db.get(CourseSubmission, submission_id)
    user = submission.user
    return submission_service.process(
        api_db,
        user=user,
        submission=submission,
        settings=settings,
        elevation=ConstantElevation(12.0),
    )


# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------


def test_a_new_submission_lists_what_it_still_needs(api: TestClient, signed_up: dict) -> None:
    body = create(api, signed_up["headers"])
    assert body["status"] == "draft"
    assert sorted(body["missing_legs"]) == ["BIKE", "RUN", "SWIM"]
    assert body["course_id"] is None


def test_a_file_that_is_not_gpx_is_refused_before_it_is_stored(
    api: TestClient, signed_up: dict
) -> None:
    """The athlete finds out while the file picker is still in front of them."""
    submission = create(api, signed_up["headers"])
    response = api.put(
        f"/api/v1/course-submissions/{submission['id']}/files/BIKE",
        headers=signed_up["headers"],
        files={"file": ("bike.gpx", b"this is a .fit file, renamed", "application/gpx+xml")},
    )
    assert response.status_code == 400
    assert "bike.gpx" in response.json()["error"]["message"]

    still_empty = api.get(
        f"/api/v1/course-submissions/{submission['id']}", headers=signed_up["headers"]
    ).json()
    assert "BIKE" in still_empty["missing_legs"]


def test_uploading_every_leg_queues_the_submission(api: TestClient, signed_up: dict) -> None:
    submission = create(api, signed_up["headers"])
    for leg in ("SWIM", "BIKE"):
        body = upload(api, signed_up["headers"], submission["id"], leg)
        assert body["status"] == "draft", "not ready until all three are in"
    body = upload(api, signed_up["headers"], submission["id"], "RUN")
    assert body["status"] == "queued"
    assert body["missing_legs"] == []
    assert set(body["file_names"]) == {"SWIM", "BIKE", "RUN"}


def test_a_built_course_is_real_and_private_to_its_athlete(
    api: TestClient, signed_up: dict, api_db, api_settings
) -> None:
    submission = create(api, signed_up["headers"])
    for leg in HALF_LEGS:
        upload(api, signed_up["headers"], submission["id"], leg)

    built = build(api_db, signed_up["user"]["id"], submission["id"], api_settings)
    api_db.commit()
    assert built.status is SubmissionStatus.READY, built.problems
    assert built.course_id is not None

    course = api_db.get(Course, built.course_id)
    assert course.submitted_by_user_id is not None
    assert course.distance_type is DistanceType.HALF
    # It is a real course: three legs of real geometry, not a placeholder row.
    bundle = course.bundles[0]
    assert {leg.leg for leg in bundle.legs} == {Leg.SWIM, Leg.BIKE, Leg.RUN}
    assert bundle.elevation_source == "terrain"
    assert bundle.barriers
    assert bundle.aid_stations

    # The athlete who added it sees it in their directory...
    mine = api.get("/api/v1/courses", headers=signed_up["headers"]).json()
    assert course.slug in {row["slug"] for row in mine["data"]}
    # ...and a signed-out visitor does not.
    public = api.get("/api/v1/courses").json()
    assert course.slug not in {row["slug"] for row in public["data"]}


def test_another_athlete_cannot_see_a_submitted_course(
    api: TestClient, signed_up: dict, api_db, api_settings
) -> None:
    submission = create(api, signed_up["headers"])
    for leg in HALF_LEGS:
        upload(api, signed_up["headers"], submission["id"], leg)
    built = build(api_db, signed_up["user"]["id"], submission["id"], api_settings)
    api_db.commit()
    course = api_db.get(Course, built.course_id)

    other = api.post(
        "/api/v1/auth/signup",
        json={"email": "someone.else@example.com", "password": "correct-horse-battery"},
    )
    assert other.status_code == 201
    headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

    listing = api.get("/api/v1/courses", headers=headers).json()
    assert course.slug not in {row["slug"] for row in listing["data"]}
    # The same answer a slug that does not exist gets — see `visible_to`.
    assert api.get(f"/api/v1/courses/{course.slug}", headers=headers).status_code == 404
    assert api.get(f"/api/v1/courses/{course.slug}/recon", headers=headers).status_code == 404


def test_another_athletes_submission_cannot_be_read_or_edited(
    api: TestClient, signed_up: dict
) -> None:
    submission = create(api, signed_up["headers"])

    other = api.post(
        "/api/v1/auth/signup",
        json={"email": "intruder@example.com", "password": "correct-horse-battery"},
    )
    headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

    assert (
        api.get(f"/api/v1/course-submissions/{submission['id']}", headers=headers).status_code
        == 404
    )
    assert (
        api.delete(f"/api/v1/course-submissions/{submission['id']}", headers=headers).status_code
        == 404
    )
    assert api.get("/api/v1/course-submissions", headers=headers).json() == []


# ---------------------------------------------------------------------------
# Failure is a state, not an error
# ---------------------------------------------------------------------------


def test_a_wrong_distance_fails_with_reasons_and_keeps_the_files(
    api: TestClient, signed_up: dict, api_db, api_settings
) -> None:
    """The recovery path is replacing one file, not starting again."""
    submission = create(api, signed_up["headers"])
    upload(api, signed_up["headers"], submission["id"], "SWIM")
    upload(api, signed_up["headers"], submission["id"], "RUN")
    api.put(
        f"/api/v1/course-submissions/{submission['id']}/files/BIKE",
        headers=signed_up["headers"],
        # An Olympic bike leg, submitted as a 70.3.
        files={"file": ("bike.gpx", gpx_of(straight(40_000, 45)), "application/gpx+xml")},
    )

    built = build(api_db, signed_up["user"]["id"], submission["id"], api_settings)
    api_db.commit()

    assert built.status is SubmissionStatus.FAILED
    assert built.problems
    assert any("bike" in problem.lower() for problem in built.problems)
    assert built.course_id is None

    # Nothing half-built reached the directory.
    listing = api.get("/api/v1/courses", headers=signed_up["headers"]).json()
    assert listing["meta"]["total"] == 0

    # And the files are still there.
    current = api.get(
        f"/api/v1/course-submissions/{submission['id']}", headers=signed_up["headers"]
    ).json()
    assert current["missing_legs"] == []
    assert set(current["file_names"]) == {"SWIM", "BIKE", "RUN"}


def test_replacing_a_file_clears_the_old_problems(
    api: TestClient, signed_up: dict, api_db, api_settings
) -> None:
    """A stale problem shown against a file that has been replaced is a lie."""
    submission = create(api, signed_up["headers"])
    upload(api, signed_up["headers"], submission["id"], "SWIM")
    upload(api, signed_up["headers"], submission["id"], "RUN")
    api.put(
        f"/api/v1/course-submissions/{submission['id']}/files/BIKE",
        headers=signed_up["headers"],
        files={"file": ("bike.gpx", gpx_of(straight(40_000, 45)), "application/gpx+xml")},
    )
    build(api_db, signed_up["user"]["id"], submission["id"], api_settings)
    api_db.commit()

    fixed = upload(api, signed_up["headers"], submission["id"], "BIKE")
    assert fixed["problems"] == []
    assert fixed["status"] == "queued"


def test_submitting_before_every_leg_is_in_is_refused(api: TestClient, signed_up: dict) -> None:
    submission = create(api, signed_up["headers"])
    upload(api, signed_up["headers"], submission["id"], "SWIM")
    response = api.post(
        f"/api/v1/course-submissions/{submission['id']}/submit", headers=signed_up["headers"]
    )
    assert response.status_code == 409
    assert "bike" in response.json()["error"]["message"].lower()


def test_deleting_a_submission_removes_the_course_it_built(
    api: TestClient, signed_up: dict, api_db, api_settings
) -> None:
    submission = create(api, signed_up["headers"])
    for leg in HALF_LEGS:
        upload(api, signed_up["headers"], submission["id"], leg)
    built = build(api_db, signed_up["user"]["id"], submission["id"], api_settings)
    api_db.commit()
    slug = api_db.get(Course, built.course_id).slug

    assert (
        api.delete(
            f"/api/v1/course-submissions/{submission['id']}", headers=signed_up["headers"]
        ).status_code
        == 204
    )

    api_db.expire_all()
    assert api_db.scalar(select(Course).where(Course.slug == slug)) is None
    assert api.get("/api/v1/course-submissions", headers=signed_up["headers"]).json() == []
