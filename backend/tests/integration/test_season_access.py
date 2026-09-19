"""The whole season, held to one access rule.

`test_catalogue.py` proves the rules against courses it builds itself. This
file proves them against **every race the catalogue actually ships**, loaded
from its real bundle, because the failure this guards against is not a broken
rule — it is a race that quietly misses one.

The concern is concrete. Course geometry, the barrier ladder, the aid stations,
the named segments and the elevation profile are the course work an athlete
buys. One race serving those to a signed-out visitor is a hole in the product,
and it would not show up in a test that only ever looks at one course.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from raceos.db.catalogue import CATALOGUE
from raceos.domain.enums import CourseAvailability, CourseVisibility
from raceos.ingest.bundle_loader import load_bundle_file

pytestmark = pytest.mark.integration

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"

#: Every catalogue race an athlete is meant to be able to buy.
SEASON = [
    entry
    for entry in CATALOGUE
    if entry.availability is CourseAvailability.AVAILABLE
    and entry.visibility is CourseVisibility.CATALOGUE
]

#: The keys that carry paid course work. Empty or absent unless unlocked.
PAID_KEYS = ("barriers", "aid_stations", "waypoints", "segments")


needs_bundles = pytest.mark.skipif(
    not BUNDLE_DIR.is_dir() or not list(BUNDLE_DIR.glob("*.bundle.json")),
    reason="generated bundles are git-ignored build artefacts; none in this checkout",
)


@pytest.fixture
def season(api: TestClient, api_db) -> list[str]:
    """The real seed, in miniature: load the bundles, then apply the manifest.

    Both halves matter. The loader supplies the geometry; `apply_catalogue`
    supplies `availability`, `visibility` and the announced date, which are
    catalogue columns and not bundle ones. Loading without applying produces
    exactly the bug this file exists to catch — a course with a course in it
    that no athlete can enter.
    """
    from raceos.db.seed import apply_catalogue

    # Every bundled row, not just the season: `apply_catalogue` writes only
    # the manifest's own columns over a bundled course and leaves name, place
    # and geometry to the loader, so a bundled entry whose file was never
    # loaded would be inserted with nulls where the schema requires values.
    for entry in CATALOGUE:
        if entry.bundle_slug is None:
            continue
        path = BUNDLE_DIR / f"{entry.bundle_slug}.bundle.json"
        if not path.is_file():
            pytest.fail(f"{entry.slug} names {path.name}, which was never generated")
        load_bundle_file(api_db, path)
    apply_catalogue(api_db)
    loaded = [entry.slug for entry in SEASON]
    api_db.commit()
    return loaded


@needs_bundles
def test_the_season_is_not_empty() -> None:
    """A guard on the guards: these tests pass trivially against no races."""
    assert len(SEASON) >= 7


@needs_bundles
@pytest.mark.parametrize("slug", [e.slug for e in SEASON])
def test_a_signed_out_visitor_is_shown_the_race_but_not_the_course(
    api: TestClient, season: list[str], slug: str
) -> None:
    """Listed, described, and locked — the free evaluation of *which* race."""
    body = api.get(f"/api/v1/courses/{slug}/recon").json()

    assert body["access"]["map_unlocked"] is False
    assert body["access"]["map_locked_reason"], "a locked map must say why"
    # Not a showcase, so it must not claim to be an illustration.
    assert body["access"]["illustrative_map"] is False

    for leg in body["legs"]:
        assert leg["coordinates"] == [], f"{slug}: {leg['leg']} geometry leaked"
        # The facts someone picks a race on still ship.
        assert leg["distance_m"] > 0
    for key in PAID_KEYS:
        assert body[key] == [], f"{slug}: {key} leaked to a signed-out visitor"
    assert body["elevation_profile"] == {}
    assert body["terrain_pmtiles_key"] is None

    # The headline cut-off is deliberately free — the cut-off calculator runs
    # on it — so its absence would be a regression too.
    assert body["totals"]["final_cutoff_minutes"] is not None


@needs_bundles
@pytest.mark.parametrize("slug", [e.slug for e in SEASON])
def test_signing_in_alone_does_not_unlock_the_course(
    api: TestClient, season: list[str], slug: str, signed_up: dict
) -> None:
    """An account is not a purchase. This is the paywall, per race."""
    body = api.get(f"/api/v1/courses/{slug}/recon", headers=signed_up["headers"]).json()
    assert body["access"]["map_unlocked"] is False
    for leg in body["legs"]:
        assert leg["coordinates"] == []
    for key in PAID_KEYS:
        assert body[key] == []


@needs_bundles
@pytest.mark.parametrize("slug", [e.slug for e in SEASON])
def test_the_terrain_field_cannot_be_read_around_the_locked_map(
    api: TestClient, season: list[str], slug: str, signed_up: dict
) -> None:
    """The 3D map renders from `/terrain`. A locked map that still served its
    terrain would be a paywall with a door beside it."""
    for headers in ({}, signed_up["headers"]):
        response = api.get(f"/api/v1/courses/{slug}/terrain", headers=headers)
        assert (
            response.status_code == 402
        ), f"{slug}: terrain served behind a locked map ({response.status_code})"
        assert response.json()["error"]["code"] == "PAYMENT_REQUIRED"


@needs_bundles
@pytest.mark.parametrize("slug", [e.slug for e in SEASON])
def test_every_race_can_actually_be_entered(
    api: TestClient, season: list[str], slug: str, signed_up: dict
) -> None:
    """The other half of the promise: gated is not the same as unusable.

    `AVAILABLE` means an athlete can put this race on their calendar, which is
    the step every purchase starts from.
    """
    from datetime import UTC, datetime, timedelta

    response = api.post(
        "/api/v1/races",
        headers=signed_up["headers"],
        json={
            "course_ref": slug,
            "event_date": (datetime.now(UTC).date() + timedelta(days=60)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    assert response.status_code == 201, f"{slug}: {response.text}"


@needs_bundles
@pytest.mark.parametrize("slug", [e.slug for e in SEASON])
def test_every_race_carries_the_course_facts_the_directory_prints(
    api: TestClient, season: list[str], slug: str
) -> None:
    """A row with a null where a number belongs reads as a broken page.

    This is the "no race left on placeholders" check: three real legs, a real
    distance, a real cut-off and the attribution the licence obliges.
    """
    body = api.get(f"/api/v1/courses/{slug}/recon").json()

    assert {leg["leg"] for leg in body["legs"]} == {"SWIM", "BIKE", "RUN"}
    assert body["totals"]["distance_m"] > 0
    assert body["totals"]["elevation_gain_m"] >= 0
    assert body["bundle"]["provenance"]
    assert body["bundle"]["elevation_source"] == "terrain"
    # ODbL obliges attribution wherever the derived data is displayed.
    assert "OpenStreetMap" in (body["bundle"]["attribution"] or "")
    assert body["course"]["place"]
    assert body["course"]["timezone"]


# ---------------------------------------------------------------------------
# The other end of the journey
# ---------------------------------------------------------------------------

#: A mid-pack athlete, feasible on every distance in the catalogue.
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


@needs_bundles
@pytest.mark.parametrize("slug", [e.slug for e in SEASON])
def test_buying_a_plan_unlocks_the_course_it_was_bought_for(
    api: TestClient, season: list[str], slug: str, signed_up: dict, paywall: None
) -> None:
    """Discover, sign in, enter, buy, solve, open — end to end, on every race.

    The tests above prove the door is shut. This proves it opens, and opens
    for the race that was paid for rather than for the catalogue: a per-race
    purchase that unlocked every course would be a season pass sold at the
    price of one race.

    It also exercises the solver against every shipping bundle, which is the
    other thing a new course can quietly fail at — a course that lists and
    locks correctly and then cannot be planned is not a race we can sell.
    """
    from datetime import UTC, datetime, timedelta

    headers = signed_up["headers"]
    for key, value in ATHLETE_M.items():
        assert api.put(
            f"/api/v1/constraints/{key}", headers=headers, json={"value": value}
        ).status_code in (200, 201)

    entered = api.post(
        "/api/v1/races",
        headers=headers,
        json={
            "course_ref": slug,
            "event_date": (datetime.now(UTC).date() + timedelta(days=60)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    assert entered.status_code == 201, entered.text
    race_id = entered.json()["id"]

    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": race_id})
    assert draft.status_code == 201, draft.text

    bought = api.post(
        "/api/v1/checkout/authorize",
        headers=headers,
        json={"plan_id": draft.json()["id"], "currency": "GBP"},
    )
    assert bought.status_code == 201, bought.text
    assert bought.json()["purchase"]["status"] == "authorized"

    # The hold is captured by the solve, not by the authorize: an athlete is
    # charged when the plan they paid for exists, which is also what turns
    # `RACE_PAID_OR_PAYING` into the `RACE_PURCHASE` the map asks for.
    solved = api.post(f"/api/v1/plans/{draft.json()['id']}/solve", headers=headers, json={})
    assert solved.status_code in (200, 201), solved.text

    opened = api.get(f"/api/v1/courses/{slug}/recon", headers=headers).json()
    assert opened["access"]["map_unlocked"] is True, f"{slug}: paid for and still locked"
    assert opened["access"]["map_locked_reason"] is None
    assert any(leg["coordinates"] for leg in opened["legs"]), "no route to draw"
    assert opened["barriers"], "no cut-off ladder"
    assert opened["aid_stations"], "no aid stations"
    assert opened["segments"], "no named segments"
    assert opened["elevation_profile"], "no elevation profile"
    assert opened["terrain_pmtiles_key"], "no terrain for the 3D map"

    # The 3D map's own endpoint, which is behind the same decision.
    assert api.get(f"/api/v1/courses/{slug}/terrain", headers=headers).status_code == 200

    # And nothing else in the catalogue came with it.
    others = [other for other in season if other != slug]
    if others:
        neighbour = api.get(f"/api/v1/courses/{others[0]}/recon", headers=headers).json()
        assert neighbour["access"]["map_unlocked"] is False, f"buying {slug} unlocked {others[0]}"
