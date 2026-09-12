"""The command palette's two sources: one query, and the help library.

Before these existed the palette's input in the header was wired to nothing,
and the only search in the product was `GET /courses?q=`, which searches
courses alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import Course
from raceos.domain.enums import CourseAvailability
from raceos.ingest.bundle_loader import load_bundle_file

pytestmark = pytest.mark.integration

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"
TRAMUNTANA = BUNDLE_DIR / "tramuntana-full.bundle.json"
needs_bundle = pytest.mark.skipif(
    not TRAMUNTANA.is_file(), reason="generated bundles are git-ignored build artefacts"
)


@pytest.fixture
def seeded(api: TestClient, migrated_engine):
    from sqlalchemy.orm import sessionmaker

    if not TRAMUNTANA.is_file():
        pytest.skip("generated bundles are git-ignored build artefacts")
    with sessionmaker(bind=migrated_engine)() as session:
        load_bundle_file(session, TRAMUNTANA)
        session.scalar(
            select(Course).where(Course.slug == "tramuntana-full")
        ).availability = CourseAvailability.AVAILABLE
        session.commit()


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def test_the_help_library_is_readable_without_signing_in(api: TestClient) -> None:
    """Help that needs a session is help nobody can reach when locked out, and
    "I cannot sign in" is exactly when someone reads it."""
    response = api.get("/api/v1/help")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert rows, "the library is empty"
    assert all(row["summary"] and row["title"] for row in rows)
    # The list is for rendering cards, so it deliberately carries no bodies.
    assert all("body" not in row for row in rows)


def test_articles_come_back_in_reading_order_not_alphabetical(api: TestClient) -> None:
    """"Getting started" before "Billing" is the whole point of having an order."""
    categories = [row["category"] for row in api.get("/api/v1/help").json()]
    published = api.get("/api/v1/help/categories").json()

    seen = [c for i, c in enumerate(categories) if i == 0 or categories[i - 1] != c]
    assert seen == [c for c in published if c in seen]


def test_one_article_comes_back_whole(api: TestClient) -> None:
    slug = api.get("/api/v1/help").json()[0]["slug"]
    article = api.get(f"/api/v1/help/{slug}")
    assert article.status_code == 200, article.text
    assert article.json()["body"], "an article with no body is a broken promise"
    assert article.json()["read_minutes"] >= 1


def test_an_unknown_article_is_a_404_not_an_empty_page(api: TestClient) -> None:
    assert api.get("/api/v1/help/no-such-article").status_code == 404


def test_help_can_be_searched_on_its_own(api: TestClient) -> None:
    rows = api.get("/api/v1/help", params={"q": "cancel"}).json()
    assert rows
    assert any("cancel" in row["title"].lower() for row in rows)


def test_no_help_article_quotes_a_figure_the_product_cannot_produce(api: TestClient) -> None:
    """The one rule this library is written under.

    Every number an athlete reads comes from the solver. An article carrying
    its own statistic would be indistinguishable, on the page, from one.
    """
    import re

    from raceos.api.help_content import HELP_ARTICLES

    for article in HELP_ARTICLES.values():
        found = re.findall(r"\b\d+(?:[.,]\d+)?\s*(?:%|g/hr|w\b|km\b|kg\b|°)", article.body)
        assert not found, f"{article.slug} quotes {found}"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_a_single_keystroke_returns_nothing(api: TestClient) -> None:
    """One character matches most of the library and is almost always on the
    way to a real query."""
    assert api.get("/api/v1/search", params={"q": "t"}).json() == []
    assert api.get("/api/v1/search", params={"q": ""}).json() == []


@needs_bundle
def test_search_finds_a_course_for_a_signed_out_visitor(seeded, api: TestClient) -> None:
    rows = api.get("/api/v1/search", params={"q": "tramuntana"}).json()
    assert any(row["kind"] == "course" and row["ref"] == "tramuntana-full" for row in rows)


def test_search_finds_help_for_a_signed_out_visitor(api: TestClient) -> None:
    rows = api.get("/api/v1/search", params={"q": "forecast"}).json()
    assert any(row["kind"] == "help" for row in rows)


@needs_bundle
def test_a_signed_out_visitor_sees_nobody_elses_races(seeded, api: TestClient, signed_up) -> None:
    """Races and plans are scoped to their owner in SQL, not filtered after."""
    api.post(
        "/api/v1/races",
        headers=signed_up["headers"],
        json={
            "course_ref": "tramuntana-full",
            "event_date": (datetime.now(UTC).date() + timedelta(days=90)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    anonymous = api.get("/api/v1/search", params={"q": "tramuntana"}).json()
    assert all(row["kind"] not in ("race", "plan") for row in anonymous)


@needs_bundle
def test_a_signed_in_athlete_finds_their_own_race_and_plan(
    seeded, api: TestClient, signed_up
) -> None:
    headers = signed_up["headers"]
    race = api.post(
        "/api/v1/races",
        headers=headers,
        json={
            "course_ref": "tramuntana-full",
            "event_date": (datetime.now(UTC).date() + timedelta(days=90)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    assert race.status_code in (200, 201), race.text
    created = api.post(
        "/api/v1/plans", headers=headers, json={"race_id": race.json()["id"]}
    )
    assert created.status_code == 201, created.text

    rows = api.get("/api/v1/search", params={"q": "tramuntana"}, headers=headers).json()
    kinds = {row["kind"] for row in rows}
    assert "race" in kinds
    assert "plan" in kinds
    # The athlete's own things rank above the catalogue entry.
    assert rows[0]["kind"] in ("plan", "race")


@needs_bundle
def test_a_search_cannot_surface_a_course_the_directory_would_not_list(
    seeded, api: TestClient, api_db, signed_up
) -> None:
    """Search reuses the directory's own visibility filter rather than a second
    one that could drift from it."""
    from raceos.domain.enums import CourseVisibility

    course = api_db.scalar(select(Course).where(Course.slug == "tramuntana-full"))
    course.visibility = CourseVisibility.RETIRED
    api_db.commit()

    rows = api.get("/api/v1/search", params={"q": "tramuntana"}, headers=signed_up["headers"])
    assert all(row["kind"] != "course" for row in rows.json())


@needs_bundle
def test_nobody_finds_another_athletes_race(seeded, api: TestClient, signed_up) -> None:
    api.post(
        "/api/v1/races",
        headers=signed_up["headers"],
        json={
            "course_ref": "tramuntana-full",
            "event_date": (datetime.now(UTC).date() + timedelta(days=90)).isoformat(),
            "start_time_local": "07:00",
        },
    )
    other = api.post(
        "/api/v1/auth/signup",
        json={"email": "stranger.search@example.com", "password": "correct-horse-battery-42"},
    )
    intruder = {"Authorization": f"Bearer {other.json()['access_token']}"}

    rows = api.get("/api/v1/search", params={"q": "tramuntana"}, headers=intruder).json()
    assert all(row["kind"] not in ("race", "plan") for row in rows)


def test_search_returns_no_navigation_targets(api: TestClient) -> None:
    """Deliberate: "open settings" is a frontend route, the frontend owns the
    one list of them, and a round trip to be told a page exists is a round trip
    for nothing."""
    rows = api.get("/api/v1/search", params={"q": "settings"}).json()
    assert all(row["kind"] in ("course", "race", "plan", "help") for row in rows)


def test_the_result_limit_is_honoured(api: TestClient) -> None:
    rows = api.get("/api/v1/search", params={"q": "plan", "limit": 2}).json()
    assert len(rows) <= 2


def test_search_is_stable_between_identical_queries(api: TestClient) -> None:
    """A palette whose list reshuffles under the cursor loses the selection."""
    first = api.get("/api/v1/search", params={"q": "forecast"}).json()
    second = api.get("/api/v1/search", params={"q": "forecast"}).json()
    assert first == second
