"""The coach domain and share links.

Two of the three structural guarantees live here, and each is tested by
*attempting the forbidden action through every path that exists* rather than
by asserting a flag.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import (
    CoachAthleteLink,
    Constraint,
    Course,
    CourseBundle,
    Plan,
    Race,
    ShareLink,
    User,
)
from raceos.domain.enums import CoachLinkStatus, PlanStatus, ShareScope, UserTier
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


@pytest.fixture
def pair(api: TestClient, signed_up, migrated_engine, api_db, paywall):
    """An athlete with a solved plan, and a coach-tier coach."""
    from sqlalchemy.orm import sessionmaker

    from raceos.db.models import Subscription
    from raceos.domain.enums import SubscriptionStatus

    if not TRAMUNTANA.is_file():
        pytest.skip("generated bundles are git-ignored build artefacts")

    with sessionmaker(bind=migrated_engine)() as session:
        load_bundle_file(session, TRAMUNTANA)
        session.commit()
        course = session.scalar(select(Course).where(Course.slug == "tramuntana-full"))
        bundle = session.scalar(select(CourseBundle))
        course_id, bundle_id = course.id, bundle.id

    athlete_headers = signed_up["headers"]
    for key, value in ATHLETE_M.items():
        api.put(f"/api/v1/constraints/{key}", headers=athlete_headers, json={"value": value})

    athlete = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    race = Race(
        user_id=athlete.id,
        course_id=course_id,
        course_bundle_id=bundle_id,
        event_date=datetime.now(UTC).date() + timedelta(days=5),
        start_time_local=time(7, 0),
    )
    api_db.add(race)
    api_db.commit()

    draft = api.post("/api/v1/plans", headers=athlete_headers, json={"race_id": str(race.id)})
    buy_plan(api, athlete_headers, draft.json()["id"])
    plan = api.post(
        f"/api/v1/plans/{draft.json()['id']}/solve", headers=athlete_headers, json={}
    ).json()

    created = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "jonas.feldt@example.com",
            "password": "correct-horse-battery",
            "name": "Jonas Feldt",
        },
    ).json()
    coach = api_db.scalar(select(User).where(User.email == "jonas.feldt@example.com"))
    coach.tier = UserTier.COACH
    api_db.add(
        Subscription(user_id=coach.id, tier=UserTier.COACH, status=SubscriptionStatus.ACTIVE)
    )
    api_db.commit()

    return {
        "athlete_headers": athlete_headers,
        "athlete_id": athlete.id,
        "coach_headers": {"Authorization": f"Bearer {created['access_token']}"},
        "coach_id": coach.id,
        "race_id": race.id,
        "plan": plan,
        "plan_id": plan["id"],
    }


def _link(api, pair, *, accept=True, **perms):
    invite = api.post(
        "/api/v1/coach/invites",
        headers=pair["coach_headers"],
        json={"athlete_email": "elena.marsh@example.com"},
    )
    assert invite.status_code == 201, invite.text
    body = invite.json()
    if not accept:
        return body
    accepted = api.post(
        "/api/v1/coach/invites/accept",
        headers=pair["athlete_headers"],
        json={"token": body["invite_token"]},
    )
    assert accepted.status_code == 200, accepted.text
    link_id = accepted.json()["id"]
    if perms:
        granted = api.patch(
            f"/api/v1/coach/links/{link_id}/permissions",
            headers=pair["athlete_headers"],
            json=perms,
        )
        assert granted.status_code == 200, granted.text
    return {**body, "link_id": link_id}


# ---------------------------------------------------------------------------
# Invite and accept
# ---------------------------------------------------------------------------


@needs_bundle
def test_an_invite_grants_nothing_until_the_athlete_accepts(pair, api: TestClient) -> None:
    _link(api, pair, accept=False)

    board = api.get("/api/v1/coach/board", headers=pair["coach_headers"]).json()
    assert len(board) == 1
    assert board[0]["can_view_plans"] is False
    assert "not been accepted" in board[0]["withheld_reason"]
    assert board[0]["projected_minutes"] is None


@needs_bundle
def test_accepting_still_grants_nothing_until_permissions_are_set(pair, api: TestClient) -> None:
    """Acceptance links the accounts. It does not share anything."""
    _link(api, pair)

    board = api.get("/api/v1/coach/board", headers=pair["coach_headers"]).json()
    assert board[0]["can_view_plans"] is False
    assert "not granted plan access" in board[0]["withheld_reason"]


@needs_bundle
def test_only_the_named_athlete_can_accept_an_invite(pair, api: TestClient) -> None:
    """A forwarded invite must not tell the wrong person it is real."""
    body = _link(api, pair, accept=False)
    other = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "stranger@example.com",
            "password": "correct-horse-battery",
            "name": "Stranger",
        },
    ).json()
    response = api.post(
        "/api/v1/coach/invites/accept",
        headers={"Authorization": f"Bearer {other['access_token']}"},
        json={"token": body["invite_token"]},
    )
    assert response.status_code == 404


@needs_bundle
def test_an_invite_token_is_spent_on_acceptance(pair, api: TestClient) -> None:
    body = _link(api, pair)
    again = api.post(
        "/api/v1/coach/invites/accept",
        headers=pair["athlete_headers"],
        json={"token": body["invite_token"]},
    )
    assert again.status_code == 404


@needs_bundle
def test_an_expired_invite_is_refused(pair, api: TestClient, api_db) -> None:
    body = _link(api, pair, accept=False)
    link = api_db.scalar(select(CoachAthleteLink))
    link.invite_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    api_db.commit()

    response = api.post(
        "/api/v1/coach/invites/accept",
        headers=pair["athlete_headers"],
        json={"token": body["invite_token"]},
    )
    assert response.status_code == 409
    assert "expired" in response.json()["error"]["message"]


@needs_bundle
def test_seat_limits_count_pending_invites(pair, api: TestClient, api_db) -> None:
    """Otherwise a coach at their limit could hold an unbounded queue of
    invitations, every one of which would exceed it on acceptance."""
    coach = api_db.get(User, pair["coach_id"])
    coach.tier = UserTier.SEASON  # one seat
    api_db.commit()

    _link(api, pair, accept=False)
    api.post(
        "/api/v1/auth/signup",
        json={
            "email": "second@example.com",
            "password": "correct-horse-battery",
            "name": "Second",
        },
    )
    second = api.post(
        "/api/v1/coach/invites",
        headers=pair["coach_headers"],
        json={"athlete_email": "second@example.com"},
    )
    assert second.status_code == 403
    assert "1 athlete" in second.json()["error"]["message"]


# ---------------------------------------------------------------------------
# STRUCTURAL GUARANTEE 1 — no coach path reaches a constraint
# ---------------------------------------------------------------------------


@needs_bundle
def test_no_coach_endpoint_can_read_or_write_a_constraint(pair, api: TestClient, api_db) -> None:
    """Attempted through every path that exists, with full permissions."""
    link = _link(api, pair, plans=True, build=True, analysis=True)
    coach = pair["coach_headers"]
    athlete_id = pair["athlete_id"]

    # 1. The athlete's own constraint endpoints, as the coach.
    assert api.get("/api/v1/constraints", headers=coach).status_code == 200
    mine = api.get("/api/v1/constraints", headers=coach).json()
    values = {row["key"]: row.get("value") for row in mine}
    assert (
        values.get("bike_threshold_power") != 224
    ), "the coach read the athlete's value from their own endpoint"

    # 2. Writing the athlete's constraint through the constraint endpoint —
    #    it can only ever address the caller's own row.
    api.put("/api/v1/constraints/bike_threshold_power", headers=coach, json={"value": 400})
    athlete_value = api_db.scalar(
        select(Constraint).where(
            Constraint.user_id == athlete_id,
            Constraint.key == "bike_threshold_power",
        )
    )
    api_db.refresh(athlete_value)
    assert float(athlete_value.value) == 224.0

    # 3. Any coach route that mentions the athlete by id.
    for path in (
        f"/api/v1/coach/athletes/{athlete_id}/constraints",
        f"/api/v1/coach/athletes/{athlete_id}/constraints/bike_threshold_power",
    ):
        assert api.get(path, headers=coach).status_code == 404, f"{path} exists and it must not"
        assert api.put(path, headers=coach, json={"value": 400}).status_code in (
            404,
            405,
        )

    assert link["link_id"]


def test_the_permission_vocabulary_has_exactly_three_entries() -> None:
    """A fourth would have to be added in three independent places: the enum
    here, a column on the table, and a field on the request schema."""
    from raceos.api.schemas.coach import PermissionPatch
    from raceos.services.coach_service import COACH_PERMISSIONS

    assert COACH_PERMISSIONS == ("plans", "build", "analysis")
    assert set(PermissionPatch.model_fields) == {"plans", "build", "analysis"}

    columns = {column.name for column in CoachAthleteLink.__table__.columns}
    assert "perm_constraints" not in columns
    assert {c for c in columns if c.startswith("perm_")} == {
        "perm_plans",
        "perm_build",
        "perm_analysis",
    }


def test_asking_for_a_constraints_permission_is_a_programming_error() -> None:
    """Not a silent False. A caller passing "constraints" gets told why."""
    from raceos.services import coach_service

    with pytest.raises(ValueError, match="never will be") as error:
        coach_service.require_permission(
            session=None,  # type: ignore[arg-type]
            coach=None,  # type: ignore[arg-type]
            athlete_id=UUID(int=0),
            permission="constraints",
        )
    assert "never will be" in str(error.value)


def test_the_coach_router_never_imports_the_constraint_writer() -> None:
    """A guarantee that depends on nobody calling a function is weaker than
    one where the function is not reachable from the module at all."""
    from raceos.api.routers import coach as coach_router
    from raceos.services import coach_service

    for module in (coach_router, coach_service):
        source = inspect.getsource(module)
        assert (
            "write_constraint" not in source
        ), f"{module.__name__} references the constraint writer"


# ---------------------------------------------------------------------------
# Permissions are live
# ---------------------------------------------------------------------------


@needs_bundle
def test_plan_access_appears_and_disappears_with_the_permission(pair, api: TestClient) -> None:
    """Checked per request, never cached at link time."""
    link = _link(api, pair, plans=True)
    coach = pair["coach_headers"]
    path = f"/api/v1/coach/athletes/{pair['athlete_id']}/plans/{pair['plan_id']}"

    assert api.get(path, headers=coach).status_code == 200

    api.patch(
        f"/api/v1/coach/links/{link['link_id']}/permissions",
        headers=pair["athlete_headers"],
        json={"plans": False},
    )
    assert api.get(path, headers=coach).status_code == 403


@needs_bundle
def test_revoking_a_link_cuts_access_immediately(pair, api: TestClient) -> None:
    link = _link(api, pair, plans=True)
    coach = pair["coach_headers"]
    path = f"/api/v1/coach/athletes/{pair['athlete_id']}/plans/{pair['plan_id']}"
    assert api.get(path, headers=coach).status_code == 200

    api.post(f"/api/v1/coach/links/{link['link_id']}/revoke", headers=pair["athlete_headers"])
    assert api.get(path, headers=coach).status_code == 404


@needs_bundle
def test_a_coach_cannot_grant_themselves_permissions(pair, api: TestClient) -> None:
    link = _link(api, pair)
    response = api.patch(
        f"/api/v1/coach/links/{link['link_id']}/permissions",
        headers=pair["coach_headers"],
        json={"plans": True, "build": True},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Building on the athlete's behalf
# ---------------------------------------------------------------------------


@needs_bundle
def test_a_coach_built_plan_waits_for_the_athletes_approval(pair, api: TestClient, api_db) -> None:
    """It is not their plan until they say so."""
    _link(api, pair, plans=True, build=True)
    built = api.post(
        f"/api/v1/coach/athletes/{pair['athlete_id']}/plans/{pair['plan_id']}/build",
        headers=pair["coach_headers"],
    )
    assert built.status_code == 200, built.text
    body = built.json()
    assert body["status"] == "pending_athlete_approval"

    plan = api_db.get(Plan, UUID(body["id"]))
    api_db.refresh(plan)
    assert plan.built_by_coach_id == pair["coach_id"]
    assert plan.approved_at is None

    approved = api.post(f"/api/v1/plans/{body['id']}/approve", headers=pair["athlete_headers"])
    assert approved.status_code == 200
    assert approved.json()["status"] == "active"


@needs_bundle
def test_building_needs_the_build_permission_specifically(pair, api: TestClient) -> None:
    _link(api, pair, plans=True)
    response = api.post(
        f"/api/v1/coach/athletes/{pair['athlete_id']}/plans/{pair['plan_id']}/build",
        headers=pair["coach_headers"],
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["permission"] == "build"


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


@needs_bundle
def test_a_note_is_escaped_on_write_not_on_render(pair, api: TestClient, api_db) -> None:
    """Escaping at render time means every renderer has to remember."""
    from raceos.db.models import CoachNote

    _link(api, pair, plans=True)
    api.post(
        f"/api/v1/coach/athletes/{pair['athlete_id']}/notes",
        headers=pair["coach_headers"],
        json={"body": "<script>alert('x')</script> ride steady"},
    )
    stored = api_db.scalar(select(CoachNote))
    assert "<script>" not in stored.body
    assert "&lt;script&gt;" in stored.body


@needs_bundle
def test_the_athlete_reads_notes_written_about_them(pair, api: TestClient) -> None:
    _link(api, pair, plans=True)
    api.post(
        f"/api/v1/coach/athletes/{pair['athlete_id']}/notes",
        headers=pair["coach_headers"],
        json={"body": "Hold the first hour."},
    )
    notes = api.get("/api/v1/coach/notes", headers=pair["athlete_headers"]).json()
    assert len(notes) == 1
    assert notes[0]["body"] == "Hold the first hour."


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------


@needs_bundle
def test_the_board_shows_the_athletes_real_numbers(pair, api: TestClient) -> None:
    _link(api, pair, plans=True)
    row = api.get("/api/v1/coach/board", headers=pair["coach_headers"]).json()[0]

    assert row["can_view_plans"] is True
    assert row["plan_id"] == pair["plan_id"]
    assert row["projected_minutes"] == pair["plan"]["projected_minutes"]
    assert row["days_away"] == 5
    assert row["withheld_reason"] is None


@needs_bundle
def test_the_board_is_a_coach_tier_action(pair, api: TestClient, api_db) -> None:
    from raceos.db.models import Subscription

    coach = api_db.get(User, pair["coach_id"])
    coach.tier = UserTier.SEASON
    for subscription in api_db.scalars(
        select(Subscription).where(Subscription.user_id == coach.id)
    ):
        api_db.delete(subscription)
    api_db.commit()

    response = api.get("/api/v1/coach/board", headers=pair["coach_headers"])
    assert response.status_code == 402


@needs_bundle
def test_compare_reads_the_same_rows_as_the_board(pair, api: TestClient) -> None:
    """Built from the board rather than a second query, so the two cannot
    disagree about a margin."""
    _link(api, pair, plans=True)
    board = api.get("/api/v1/coach/board", headers=pair["coach_headers"]).json()
    compared = api.post(
        "/api/v1/coach/compare",
        headers=pair["coach_headers"],
        json={"athlete_ids": [str(pair["athlete_id"])]},
    ).json()

    assert len(compared) == 1
    assert compared[0]["worst_margin_minutes"] == board[0]["worst_margin_minutes"]
    assert compared[0]["margin_label"] == board[0]["margin_label"]


# ---------------------------------------------------------------------------
# STRUCTURAL GUARANTEE 2 — no share scope exposes constraints or account data
# ---------------------------------------------------------------------------


@needs_bundle
@pytest.mark.parametrize("scope", [s.value for s in ShareScope])
def test_no_share_scope_exposes_a_constraint_or_account_data(
    pair, api: TestClient, scope: str
) -> None:
    """Every scope, including full_plan, checked against the actual body."""
    created = api.post(
        f"/api/v1/plans/{pair['plan_id']}/share",
        headers=pair["athlete_headers"],
        json={"scope": scope, "expires_in_days": 7},
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]

    body = api.get(f"/api/v1/shared/{token}").json()
    flattened = repr(body)

    # No constraint key appears anywhere in the body, at any depth — not in
    # a bag item's reason, not in a fuelling binding key.
    for key in ATHLETE_M:
        assert key not in flattened, f"{scope} leaked the constraint {key}"
    assert "224" not in str(body.get("shared_by", ""))
    assert "elena.marsh@example.com" not in flattened
    assert "constraint" not in flattened.lower()
    assert "weight" not in flattened.lower()
    assert "tier" not in flattened.lower()
    assert "email" not in flattened.lower()
    # Only a first name travels: a share link is often forwarded.
    assert body["shared_by"] == "Elena"


def test_the_share_allow_list_is_the_only_source_of_fields() -> None:
    """A field added to the plan serialiser later must be *chosen* into a
    share response; it cannot leak by having been forgotten."""
    from raceos.services.share_service import (
        BLOCK_FIELDS,
        SCOPE_BLOCKS,
        SHAREABLE_PLAN_FIELDS,
    )

    for scope, blocks in SCOPE_BLOCKS.items():
        assert blocks <= SHAREABLE_PLAN_FIELDS, f"{scope} draws outside the allow-list"

    forbidden = {"constraints", "constraint_refs", "email", "weight", "tier", "user_id"}
    assert forbidden.isdisjoint(SHAREABLE_PLAN_FIELDS)

    # And the same holds one level down: the "Why this?" content and the
    # binding keys that name a constraint are outside every block's list.
    for block, fields in BLOCK_FIELDS.items():
        assert "reason_constraint_key" not in fields, block
        assert "reason_text" not in fields, block
        assert not any(field.startswith("binding_") for field in fields), block


def test_every_block_the_scopes_reference_has_a_field_list() -> None:
    """A block with no field list would fall through the projection."""
    from raceos.services.share_service import BLOCK_FIELDS, SCOPE_BLOCKS

    referenced = set().union(*SCOPE_BLOCKS.values())
    assert referenced <= set(BLOCK_FIELDS)


def test_the_projection_selects_rather_than_redacts() -> None:
    """An unknown field is dropped, not carried through."""
    from raceos.services.share_service import _project_block

    projected = _project_block(
        "bags",
        [
            {
                "key": "morning",
                "name": "Morning bag",
                "when_label": "Race morning",
                "item_count": 1,
                "secret_new_field": "should not survive",
                "items": [
                    {
                        "ordinal": 0,
                        "name": "Goggles",
                        "qty": "1",
                        "reason_constraint_key": "swim_threshold_pace",
                        "reason_text": "Swim leg planned at 1:56/100m.",
                    }
                ],
            }
        ],
    )
    assert "secret_new_field" not in projected[0]
    item = projected[0]["items"][0]
    assert item == {"ordinal": 0, "name": "Goggles", "qty": "1"}


@needs_bundle
def test_the_why_this_drawer_is_not_shared(pair, api: TestClient) -> None:
    """`constraint_refs` carries the athlete's numbers with their names on."""
    token = api.post(
        f"/api/v1/plans/{pair['plan_id']}/share",
        headers=pair["athlete_headers"],
        json={"scope": "full_plan", "expires_in_days": 7},
    ).json()["token"]

    body = api.get(f"/api/v1/shared/{token}").json()
    assert "constraint_refs" not in body


# ---------------------------------------------------------------------------
# Share link mechanics
# ---------------------------------------------------------------------------


@needs_bundle
def test_a_share_link_must_expire(pair, api: TestClient, api_db) -> None:
    """There is no "never" to send, and the column is NOT NULL."""
    refused = api.post(
        f"/api/v1/plans/{pair['plan_id']}/share",
        headers=pair["athlete_headers"],
        json={"expires_in_days": 0},
    )
    assert refused.status_code == 422

    too_long = api.post(
        f"/api/v1/plans/{pair['plan_id']}/share",
        headers=pair["athlete_headers"],
        json={"expires_in_days": 3650},
    )
    assert too_long.status_code == 422

    assert ShareLink.__table__.columns["expires_at"].nullable is False


@needs_bundle
def test_revoking_a_link_works_on_a_page_already_open(pair, api: TestClient) -> None:
    """Every resolve re-reads the row, so revocation is immediate."""
    created = api.post(
        f"/api/v1/plans/{pair['plan_id']}/share",
        headers=pair["athlete_headers"],
        json={"expires_in_days": 7},
    ).json()
    token = created["token"]
    assert api.get(f"/api/v1/shared/{token}").status_code == 200

    api.post(
        f"/api/v1/share-links/{created['link']['id']}/revoke",
        headers=pair["athlete_headers"],
    )
    assert api.get(f"/api/v1/shared/{token}").status_code == 404


@needs_bundle
def test_an_expired_link_stops_resolving(pair, api: TestClient, api_db) -> None:
    created = api.post(
        f"/api/v1/plans/{pair['plan_id']}/share",
        headers=pair["athlete_headers"],
        json={"expires_in_days": 1},
    ).json()
    link = api_db.get(ShareLink, UUID(created["link"]["id"]))
    link.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    api_db.commit()

    assert api.get(f"/api/v1/shared/{created['token']}").status_code == 404


@needs_bundle
def test_an_unknown_a_revoked_and_an_expired_link_are_indistinguishable(
    pair, api: TestClient
) -> None:
    """A probe must learn nothing about which links exist."""
    created = api.post(
        f"/api/v1/plans/{pair['plan_id']}/share",
        headers=pair["athlete_headers"],
        json={"expires_in_days": 7},
    ).json()
    api.post(
        f"/api/v1/share-links/{created['link']['id']}/revoke",
        headers=pair["athlete_headers"],
    )

    revoked = api.get(f"/api/v1/shared/{created['token']}")
    unknown = api.get("/api/v1/shared/AAAAAAAAAAAAAAAAAAAAAAAA")
    assert revoked.status_code == unknown.status_code == 404
    assert revoked.json()["error"]["message"] == unknown.json()["error"]["message"]


@needs_bundle
def test_only_the_hash_is_stored(pair, api: TestClient, api_db) -> None:
    created = api.post(
        f"/api/v1/plans/{pair['plan_id']}/share",
        headers=pair["athlete_headers"],
        json={"expires_in_days": 7},
    ).json()
    token = created["token"]

    link = api_db.scalar(select(ShareLink))
    assert link.token_hash != token
    assert token not in link.token_hash
    assert link.token_prefix == token[:12]


@needs_bundle
def test_the_access_code_is_a_second_factor_not_the_gate(pair, api: TestClient) -> None:
    created = api.post(
        f"/api/v1/plans/{pair['plan_id']}/share",
        headers=pair["athlete_headers"],
        json={"expires_in_days": 7, "access_code": "MARSH1"},
    ).json()
    token = created["token"]

    assert api.get(f"/api/v1/shared/{token}").status_code == 422
    assert api.get(f"/api/v1/shared/{token}?access_code=WRONG1").status_code == 422
    assert api.get(f"/api/v1/shared/{token}?access_code=MARSH1").status_code == 200

    # And the code alone opens nothing: the token is the boundary.
    assert api.get("/api/v1/shared/MARSH1?access_code=MARSH1").status_code == 404


@needs_bundle
def test_opens_are_counted_and_the_ip_is_hashed(pair, api: TestClient, api_db) -> None:
    from raceos.db.models import ShareLinkOpen

    created = api.post(
        f"/api/v1/plans/{pair['plan_id']}/share",
        headers=pair["athlete_headers"],
        json={"expires_in_days": 7},
    ).json()
    api.get(f"/api/v1/shared/{created['token']}")
    api.get(f"/api/v1/shared/{created['token']}")

    link = api_db.get(ShareLink, UUID(created["link"]["id"]))
    api_db.refresh(link)
    assert link.opens_count == 2
    assert link.last_opened_at is not None

    opens = api_db.scalars(select(ShareLinkOpen)).all()
    assert len(opens) == 2
    for entry in opens:
        assert entry.ip_hash is None or len(entry.ip_hash) == 64


@needs_bundle
def test_only_the_owner_manages_a_share_link(pair, api: TestClient) -> None:
    created = api.post(
        f"/api/v1/plans/{pair['plan_id']}/share",
        headers=pair["athlete_headers"],
        json={"expires_in_days": 7},
    ).json()
    coach = pair["coach_headers"]

    assert (
        api.post(f"/api/v1/share-links/{created['link']['id']}/revoke", headers=coach).status_code
        == 404
    )
    assert api.get(f"/api/v1/plans/{pair['plan_id']}/share", headers=coach).status_code == 403


@needs_bundle
def test_a_draft_cannot_be_shared(pair, api: TestClient) -> None:
    draft = api.post(
        "/api/v1/plans",
        headers=pair["athlete_headers"],
        json={"race_id": str(pair["race_id"])},
    )
    if draft.status_code == 201:
        response = api.post(
            f"/api/v1/plans/{draft.json()['id']}/share",
            headers=pair["athlete_headers"],
            json={"expires_in_days": 7},
        )
        assert response.status_code == 409


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/coach/invites"),
        ("POST", "/api/v1/coach/invites/accept"),
        ("GET", "/api/v1/coach/athletes"),
        ("GET", "/api/v1/coach/coaches"),
        ("GET", "/api/v1/coach/board"),
        ("POST", "/api/v1/coach/compare"),
        ("GET", "/api/v1/coach/notes"),
        ("PATCH", f"/api/v1/coach/links/{UUID(int=0)}/permissions"),
        ("POST", f"/api/v1/coach/links/{UUID(int=0)}/revoke"),
        ("POST", f"/api/v1/plans/{UUID(int=0)}/share"),
        ("GET", f"/api/v1/plans/{UUID(int=0)}/share"),
        ("POST", f"/api/v1/share-links/{UUID(int=0)}/revoke"),
    ],
)
def test_every_coach_and_share_endpoint_rejects_an_absent_token(
    api: TestClient, method: str, path: str
) -> None:
    response = api.request(method, path, json={"athlete_email": "x@example.com", "token": "x" * 24})
    assert response.status_code == 401


def test_resolving_a_share_link_is_deliberately_public(api: TestClient) -> None:
    """A share link is opened by someone with no account."""
    assert api.get("/api/v1/shared/AAAAAAAAAAAAAAAAAAAAAAAA").status_code == 404


def test_a_plan_status_vocabulary_that_supports_coach_approval() -> None:
    assert PlanStatus.PENDING_ATHLETE_APPROVAL.value == "pending_athlete_approval"
    assert CoachLinkStatus.ACTIVE.value == "active"
