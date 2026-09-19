"""Setting up another race the way one was already set up.

"Duplicate" was a listed action with nothing behind it. What it cannot mean is
a second plan beside this one: a race holds one draft and one active version,
enforced by a partial unique index. What it does mean is the thing an athlete
wants — *I raced Kalmar like this; set Roth up the same way* — and these tests
pin the line between the inputs that travel and the results that must not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.integration.conftest import buy_plan
from tests.integration.test_notifications_and_status import ATHLETE_M, needs_bundle, seeded

__all__ = ["seeded"]  # re-exported so the fixture resolves in this module

pytestmark = pytest.mark.integration


def _duplicate(api: TestClient, solved, race_id: str | None = None):
    return api.post(
        f"/api/v1/plans/{solved['plan_id']}/duplicate",
        headers=solved["headers"],
        json={"race_id": race_id or solved["target_race"]},
    )


def _race(api: TestClient, headers, *, days: int) -> str:
    response = api.post(
        "/api/v1/races",
        headers=headers,
        json={
            "course_ref": "tramuntana-full",
            "event_date": (datetime.now(UTC).date() + timedelta(days=days)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    assert response.status_code == 201, response.text[:300]
    return response.json()["id"]


@pytest.fixture
def solved(seeded, api: TestClient, signed_up):
    """A solved plan on one race, and a second empty race to copy onto."""
    headers = signed_up["headers"]
    for key, value in ATHLETE_M.items():
        api.put(f"/api/v1/constraints/{key}", headers=headers, json={"value": value})

    source_race = _race(api, headers, days=90)
    plan_id = api.post("/api/v1/plans", headers=headers, json={"race_id": source_race}).json()["id"]
    api.patch(
        f"/api/v1/plans/{plan_id}/draft",
        headers=headers,
        json={"goal_minutes": 660, "risk": "aggressive"},
    )
    buy_plan(api, headers, plan_id)
    api.post(f"/api/v1/plans/{plan_id}/solve", headers=headers, json={})
    return {
        "headers": headers,
        "plan_id": plan_id,
        "source_race": source_race,
        "target_race": _race(api, headers, days=180),
    }


@needs_bundle
def test_the_goal_and_risk_travel(api: TestClient, solved) -> None:
    response = _duplicate(api, solved)

    assert response.status_code == 201
    body = response.json()
    assert body["goal_minutes"] == 660
    assert body["race_id"] == solved["target_race"]


@needs_bundle
def test_the_solved_numbers_do_not_travel(api: TestClient, solved) -> None:
    """Splits and a feasibility verdict belong to the solve that made them and
    to the course it was made for. Carrying them onto another race would
    attach a Mallorca profile to a different course and still call it
    provenance."""
    body = _duplicate(api, solved).json()

    assert body["status"] == "draft"
    assert body["feasibility"] == "NOT_SOLVED"
    assert body.get("solved_at") is None
    assert not body.get("splits")


@needs_bundle
def test_duplicating_onto_its_own_race_is_refused(api: TestClient, solved) -> None:
    """A race holds one plan. The refusal says what to do instead."""
    response = _duplicate(api, solved, solved["source_race"])

    assert response.status_code == 409
    assert "new version" in response.json()["error"]["message"]


@needs_bundle
def test_another_athletes_plan_cannot_be_duplicated(api: TestClient, solved) -> None:
    intruder = api.post(
        "/api/v1/auth/signup",
        json={"email": "intruder@example.com", "password": "correct-horse-battery"},
    ).json()
    headers = {"Authorization": f"Bearer {intruder['access_token']}"}

    response = api.post(
        f"/api/v1/plans/{solved['plan_id']}/duplicate",
        headers=headers,
        json={"race_id": solved["target_race"]},
    )

    assert response.status_code in (403, 404)


@needs_bundle
def test_a_race_that_is_not_yours_is_not_a_target(api: TestClient, solved) -> None:
    """The target is checked for ownership too, not just the source."""
    other = api.post(
        "/api/v1/auth/signup",
        json={"email": "other@example.com", "password": "correct-horse-battery"},
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    for key, value in ATHLETE_M.items():
        api.put(f"/api/v1/constraints/{key}", headers=other_headers, json={"value": value})
    foreign_race = _race(api, other_headers, days=120)

    response = api.post(
        f"/api/v1/plans/{solved['plan_id']}/duplicate",
        headers=solved["headers"],
        json={"race_id": foreign_race},
    )

    assert response.status_code == 404


@needs_bundle
def test_duplicating_twice_reuses_the_one_draft(api: TestClient, solved) -> None:
    """A race holds one draft, so the second call updates it rather than
    colliding with the unique index."""
    first = _duplicate(api, solved).json()
    second = _duplicate(api, solved).json()

    assert first["id"] == second["id"]
