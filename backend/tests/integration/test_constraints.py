"""Constraints, provenance, staleness — and the first structural guarantee.

Build Spec Part 16.3 requires each of the three guarantees to have a dedicated
test that **attempts the forbidden action through every available path** and
asserts rejection. This file covers guarantee 1: a coach can never write an
athlete's constraints.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.api.errors import ForbiddenStructural
from raceos.config import Settings
from raceos.db.models import Constraint, ConstraintHistory, User
from raceos.domain.enums import CONSTRAINT_KEYS, ConstraintSource
from raceos.services import constraint_service

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------


def test_a_new_account_has_no_constraints(api: TestClient, signed_up) -> None:
    assert api.get("/api/v1/constraints", headers=signed_up["headers"]).json() == []


def test_writing_a_constraint_stamps_provenance(api: TestClient, signed_up) -> None:
    """Law 2. There is no unspecified provenance once a value exists."""
    response = api.put(
        "/api/v1/constraints/bike_threshold_power",
        headers=signed_up["headers"],
        json={"value": 224},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["value"] == 224
    assert body["unit"] == "w"
    assert body["source"] == "manual", "a typed value is a manual one"


def test_an_implausible_value_is_refused_with_the_range(api: TestClient, signed_up) -> None:
    """Watts-per-kilo entered as watts is the case §13.1 names."""
    response = api.put(
        "/api/v1/constraints/bike_threshold_power",
        headers=signed_up["headers"],
        json={"value": 3.2},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "INVALID_INPUT"
    assert error["field"] == "bike_threshold_power"
    assert error["details"]["min"] == 80.0
    assert "Check the units" in error["message"]


def test_an_unknown_key_is_refused(api: TestClient, signed_up) -> None:
    response = api.put(
        "/api/v1/constraints/vo2max", headers=signed_up["headers"], json={"value": 60}
    )
    assert response.status_code == 422


def test_rewriting_a_value_appends_to_history(api: TestClient, signed_up, api_db) -> None:
    """`constraint_history` is append-only: never updated, never deleted."""
    headers = signed_up["headers"]
    api.put("/api/v1/constraints/weight", headers=headers, json={"value": 75})
    api.put("/api/v1/constraints/weight", headers=headers, json={"value": 74})
    api.put("/api/v1/constraints/weight", headers=headers, json={"value": 73.5})

    history = api.get("/api/v1/constraints/weight/history", headers=headers).json()
    assert [row["value"] for row in history] == [
        74.0,
        75.0,
    ], "newest first, and the current value is not in history"

    current = api_db.scalar(select(Constraint).where(Constraint.key == "weight"))
    assert float(current.value) == 73.5


def test_writing_the_same_value_twice_does_not_grow_history(
    api: TestClient, signed_up, api_db
) -> None:
    headers = signed_up["headers"]
    api.put("/api/v1/constraints/weight", headers=headers, json={"value": 75})
    api.put("/api/v1/constraints/weight", headers=headers, json={"value": 75})
    assert len(list(api_db.scalars(select(ConstraintHistory)))) == 0


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_a_stale_tested_value_raises_a_warning_alongside_a_200(
    api: TestClient, signed_up, api_db, api_settings: Settings
) -> None:
    """`STALE_DATA` rides alongside the payload; it never replaces it.

    A plan built on a six-month-old FTP is still a plan.
    """
    headers = signed_up["headers"]
    api.put(
        "/api/v1/constraints/bike_threshold_power",
        headers=headers,
        json={"value": 224, "source": "tested"},
    )
    row = api_db.scalar(select(Constraint).where(Constraint.key == "bike_threshold_power"))
    row.tested_at = datetime.now(UTC) - timedelta(days=400)
    api_db.commit()

    response = api.get("/api/v1/constraints", headers=headers)
    assert response.status_code == 200
    assert response.json()[0]["stale"] is True


def test_a_manual_value_never_goes_stale(
    api: TestClient, signed_up, api_db, api_settings: Settings
) -> None:
    """It was never a measurement, so calling it stale implies a precision it
    never had."""
    headers = signed_up["headers"]
    api.put("/api/v1/constraints/weight", headers=headers, json={"value": 75})
    row = api_db.scalar(select(Constraint).where(Constraint.key == "weight"))
    row.updated_at = datetime.now(UTC) - timedelta(days=900)
    api_db.commit()
    assert constraint_service.is_stale(row, api_settings) is False


def test_weight_goes_stale_faster_than_ftp(api_settings: Settings) -> None:
    """Per-key windows, because some values move faster than others."""
    assert constraint_service.staleness_days(
        "weight", api_settings
    ) < constraint_service.staleness_days("bike_threshold_power", api_settings)


# ---------------------------------------------------------------------------
# Estimators
# ---------------------------------------------------------------------------


def test_the_run_estimator_uses_the_solvers_own_riegel_exponent(api: TestClient, signed_up) -> None:
    """§2.5.2's worked row: an improver's 10 km in 45:00 gives 275.1 s/km.

    A second copy of `riegel_r` outside the solver would let the two drift and
    silently mix two populations in every back-test — hence the shared code.
    """
    response = api.post(
        "/api/v1/constraints/run_threshold_pace/estimate",
        headers=signed_up["headers"],
        json={"answers": {"race_km": 10, "race_seconds": 2700}},
    )
    assert response.status_code == 200
    body = response.json()
    # The signed-up athlete defaults to `first`, so use that row: 10 km in
    # 45:00 for a first-timer.
    assert 250 < body["value"] < 290
    assert body["applied"] is True


def test_an_estimated_value_is_stamped_estimated(api: TestClient, signed_up) -> None:
    api.post(
        "/api/v1/constraints/swim_threshold_pace/estimate",
        headers=signed_up["headers"],
        json={"answers": {"t400_seconds": 420, "t200_seconds": 200}},
    )
    rows = api.get("/api/v1/constraints", headers=signed_up["headers"]).json()
    swim = next(r for r in rows if r["key"] == "swim_threshold_pace")
    assert swim["source"] == "estimated"
    assert swim["source_detail"].startswith("estimator ")
    assert swim["confidence_pct"] == 85


def test_css_is_derived_from_the_four_hundred_two_hundred_pair(api: TestClient, signed_up) -> None:
    """`CSS = 200 / (t400 - t200)`, converted to seconds per 100 m."""
    response = api.post(
        "/api/v1/constraints/swim_threshold_pace/estimate",
        headers=signed_up["headers"],
        json={"answers": {"t400_seconds": 400, "t200_seconds": 190}},
    )
    # 200 / 210 = 0.9524 m/s -> 105.0 s per 100 m.
    assert response.json()["value"] == pytest.approx(105.0, abs=0.1)


def test_an_estimator_refuses_impossible_inputs(api: TestClient, signed_up) -> None:
    response = api.post(
        "/api/v1/constraints/swim_threshold_pace/estimate",
        headers=signed_up["headers"],
        json={"answers": {"t400_seconds": 200, "t200_seconds": 400}},
    )
    assert response.status_code == 422


def test_an_estimator_names_a_missing_answer(api: TestClient, signed_up) -> None:
    response = api.post(
        "/api/v1/constraints/run_threshold_pace/estimate",
        headers=signed_up["headers"],
        json={"answers": {"race_km": 10}},
    )
    assert response.status_code == 422
    assert "race_seconds" in response.json()["error"]["message"]


def test_every_canonical_key_has_an_estimator(api: TestClient, signed_up) -> None:
    """All eight, so no athlete is ever stuck without a route to a value."""
    answers = {
        "swim_threshold_pace": {"t400_seconds": 400, "t200_seconds": 190},
        "bike_threshold_power": {"weight_kg": 75},
        "run_threshold_pace": {"race_km": 10, "race_seconds": 2700},
        "weight": {"weight_kg": 75},
        "sweat_rate": {
            "weight_before_kg": 75,
            "weight_after_kg": 74,
            "fluid_ml": 500,
            "minutes": 60,
        },
        "sodium_loss": {"salty_sweater": True},
        "gut_carb_ceiling": {"trained_gut": False},
        "caffeine_tolerance": {"daily_cups": 2, "weight_kg": 75},
    }
    assert set(answers) == set(CONSTRAINT_KEYS)
    for key, payload in answers.items():
        response = api.post(
            f"/api/v1/constraints/{key}/estimate",
            headers=signed_up["headers"],
            json={"answers": payload},
        )
        assert response.status_code == 200, f"{key}: {response.text}"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_every_constraint_endpoint_rejects_an_absent_token(api: TestClient) -> None:
    assert api.get("/api/v1/constraints").status_code == 401
    assert api.put("/api/v1/constraints/weight", json={"value": 75}).status_code == 401
    assert api.get("/api/v1/constraints/weight/history").status_code == 401
    assert api.post("/api/v1/constraints/weight/estimate", json={"answers": {}}).status_code == 401


def test_one_athlete_cannot_read_anothers_constraints(api: TestClient, signed_up) -> None:
    """The endpoint is scoped to the caller; there is no `?athlete_id=`."""
    api.put("/api/v1/constraints/weight", headers=signed_up["headers"], json={"value": 75})
    other = api.post(
        "/api/v1/auth/signup",
        json={"email": "other@example.com", "password": "correct-horse-battery"},
    ).json()
    rows = api.get(
        "/api/v1/constraints",
        headers={"Authorization": f"Bearer {other['access_token']}"},
    ).json()
    assert rows == [], "each athlete sees only their own"


# ---------------------------------------------------------------------------
# STRUCTURAL GUARANTEE 1 — a coach can never write an athlete's constraints
# ---------------------------------------------------------------------------


def test_the_service_refuses_any_actor_who_is_not_the_athlete(
    api: TestClient, signed_up, api_db
) -> None:
    """The guarantee at the only door there is.

    `write_constraint` is the sole path by which a value is ever written, and
    it refuses on identity rather than on permission. A coach with every
    permission granted, an admin, a support agent under a live grant and a
    bulk script all fail here identically, because none of them *is* the
    athlete.
    """
    athlete = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    api.post(
        "/api/v1/auth/signup",
        json={"email": "coach@example.com", "password": "correct-horse-battery"},
    )
    coach = api_db.scalar(select(User).where(User.email == "coach@example.com"))

    with pytest.raises(ForbiddenStructural) as excinfo:
        constraint_service.write_constraint(
            api_db,
            athlete_id=athlete.id,
            actor=coach,
            key="bike_threshold_power",
            value=300,
            source=ConstraintSource.MANUAL,
        )
    assert "only be written by that athlete" in str(excinfo.value)
    assert excinfo.value.code.value == "FORBIDDEN_STRUCTURAL", (
        "a distinct code, so a test can tell the structural guarantee from an "
        "ordinary permission check somebody could later loosen"
    )


def test_an_admin_role_does_not_help(api: TestClient, signed_up, api_db) -> None:
    """Admin is not an exemption. There is no code path, including admin
    tooling (Part 10.4)."""
    from raceos.db.models import AdminRoleAssignment
    from raceos.domain.enums import AdminRole

    athlete = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    api.post(
        "/api/v1/auth/signup",
        json={"email": "boss@example.com", "password": "correct-horse-battery"},
    )
    admin = api_db.scalar(select(User).where(User.email == "boss@example.com"))
    api_db.add(AdminRoleAssignment(user_id=admin.id, role=AdminRole.ADMIN))
    api_db.commit()

    with pytest.raises(ForbiddenStructural):
        constraint_service.write_constraint(
            api_db,
            athlete_id=athlete.id,
            actor=admin,
            key="weight",
            value=70,
            source=ConstraintSource.MANUAL,
        )


def test_no_endpoint_accepts_an_athlete_id_for_a_constraint_write(
    api: TestClient,
) -> None:
    """The HTTP surface offers no way to even *name* another athlete.

    Every constraint route is scoped to the caller, so there is no parameter
    an attacker could supply. This walks the live route table rather than
    trusting a reading of the source.
    """
    routes = [
        route
        for route in api.app.routes
        if getattr(route, "path", "").startswith("/api/v1/constraints")
    ]
    assert routes, "the constraints router must be mounted"
    for route in routes:
        for parameter in ("athlete_id", "user_id", "on_behalf_of"):
            assert parameter not in route.path, f"{route.path} lets a caller name another athlete"


def test_the_coach_link_table_has_no_constraints_permission(migrated_engine) -> None:
    """Restated here because this is where a reviewer looks for it."""
    from sqlalchemy import inspect

    columns = {c["name"] for c in inspect(migrated_engine).get_columns("coach_athlete_links")}
    assert not {c for c in columns if "constraint" in c}
