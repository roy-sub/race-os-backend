"""Athletes contributing findings about a course.

The queue this fills already existed; nothing could arrive in it. These tests
pin the three rules that make an arrival meaningful: findings group rather
than multiply, one athlete counts once however often they press the button,
and nothing an athlete sends changes a course by itself.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from raceos.db.models import Course, CrowdReport, CrowdReportUpload
from raceos.domain.enums import Difficulty, DistanceType
from tests.integration.test_course_submissions import gpx_of, straight

pytestmark = pytest.mark.integration

TRACE = gpx_of(straight(4000, 30, 100))


def _athlete(api: TestClient, email: str) -> dict[str, str]:
    created = api.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "correct-horse-battery", "name": email},
    ).json()
    return {"Authorization": f"Bearer {created['access_token']}"}


@pytest.fixture
def course(api_db) -> Course:
    """A house course to report against. The suite truncates between tests,
    so the seeded catalogue is not there to borrow one from."""
    row = Course(
        slug="mallorca-312",
        name="Mallorca 312",
        place="Alcudia",
        timezone="Europe/Madrid",
        lat=39.85,
        lng=3.12,
        distance_type=DistanceType.FULL,
        difficulty=Difficulty.HARD,
    )
    api_db.add(row)
    api_db.commit()
    return row


def _post(api: TestClient, headers, slug: str, *, title="Aid station moved", gpx=True):
    files = {"file": ("route.gpx", TRACE, "application/gpx+xml")} if gpx else None
    return api.post(
        f"/api/v1/courses/{slug}/crowd-reports",
        headers=headers,
        data={
            "category": "aid_station",
            "title": title,
            "body": "The second aid station is now 400 m further up the climb.",
        },
        files=files,
    )


def test_an_athlete_can_submit_a_finding_with_a_trace(api: TestClient, api_db, course) -> None:
    headers = _athlete(api, "reporter@example.com")

    response = _post(api, headers, course.slug)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["upload_count"] == 1


def test_ten_athletes_reporting_one_thing_make_one_finding(
    api: TestClient, api_db, course
) -> None:
    """The alternative shape — ten findings with one upload each — looks like
    ten unrelated problems and never reaches the promotion threshold."""
    for index in range(4):
        _post(api, _athlete(api, f"crowd{index}@example.com"), course.slug)

    reports = api_db.scalar(
        select(func.count()).select_from(CrowdReport).where(CrowdReport.course_id == course.id)
    )
    report = api_db.scalar(select(CrowdReport).where(CrowdReport.course_id == course.id))

    assert reports == 1
    assert report.upload_count == 4


def test_one_athlete_submitting_four_times_is_one_observation(
    api: TestClient, api_db, course
) -> None:
    """Confidence must not be manufacturable by repetition."""
    headers = _athlete(api, "eager@example.com")

    for _ in range(4):
        _post(api, headers, course.slug)

    report = api_db.scalar(select(CrowdReport).where(CrowdReport.course_id == course.id))
    uploads = api_db.scalar(
        select(func.count())
        .select_from(CrowdReportUpload)
        .where(CrowdReportUpload.crowd_report_id == report.id)
    )
    assert uploads == 1
    assert report.upload_count == 1


def test_a_submission_never_changes_the_course(api: TestClient, api_db, course) -> None:
    """It produces evidence. Promotion stays an admin act."""
    headers = _athlete(api, "reporter@example.com")
    before = course.updated_at

    _post(api, headers, course.slug)
    api_db.expire_all()

    report = api_db.scalar(select(CrowdReport).where(CrowdReport.course_id == course.id))
    assert report.status.value == "pending"
    assert api_db.get(Course, course.id).updated_at == before


def test_a_file_that_is_not_gpx_is_refused(api: TestClient, api_db, course) -> None:
    headers = _athlete(api, "reporter@example.com")

    response = api.post(
        f"/api/v1/courses/{course.slug}/crowd-reports",
        headers=headers,
        data={"category": "route", "title": "Route changed", "body": "It goes the other way."},
        files={"file": ("notes.txt", b"not a gpx at all", "text/plain")},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_a_finding_needs_a_body_somebody_can_act_on(api: TestClient, api_db, course) -> None:
    headers = _athlete(api, "reporter@example.com")

    response = api.post(
        f"/api/v1/courses/{course.slug}/crowd-reports",
        headers=headers,
        data={"category": "route", "title": "Something", "body": "   "},
    )

    assert response.status_code == 422


def test_an_unknown_course_is_a_404(api: TestClient) -> None:
    headers = _athlete(api, "reporter@example.com")

    assert _post(api, headers, "no-such-course", gpx=False).status_code == 404


def test_the_trace_is_optional(api: TestClient, api_db, course) -> None:
    """Not every finding has a route. A moved aid station is a sentence."""
    headers = _athlete(api, "reporter@example.com")

    assert _post(api, headers, course.slug, gpx=False).status_code == 201


def test_an_athlete_can_see_what_became_of_their_report(
    api: TestClient, api_db, course
) -> None:
    """A contribution surface with no outcome teaches people to stop."""
    headers = _athlete(api, "reporter@example.com")
    _post(api, headers, course.slug)

    mine = api.get("/api/v1/crowd-reports/mine", headers=headers).json()

    assert len(mine) == 1
    assert mine[0]["status"] == "pending"
    assert mine[0]["submitted_at"]


def test_signing_out_is_required_to_report(api: TestClient, api_db, course) -> None:
    """Anonymous evidence is unweighable: one athlete is one observation only
    if there is an athlete to count."""

    response = api.post(
        f"/api/v1/courses/{course.slug}/crowd-reports",
        data={"category": "route", "title": "Changed", "body": "It moved."},
    )

    assert response.status_code == 401
