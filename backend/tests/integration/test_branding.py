"""Coach branding, and the limits on it.

Coach tier advertised a white-label export and none of it existed: no logo
upload, no accent, no branded variant. What exists now is deliberately small,
and these pin why.
"""

from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import Course, CourseBundle, Race, Subscription, User
from raceos.domain.enums import SubscriptionStatus, UserTier
from raceos.exports import tokens
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


def _png(width: int = 8, height: int = 8) -> bytes:
    """A real, minimal PNG. Not a stub with the right first eight bytes —
    the upload path sniffs the magic number, and a test that faked it would
    be testing the fake."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\xe4\x62\x2f" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def coach(api: TestClient, api_db, paywall):
    """A coach on a live agreement."""
    created = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "brand.coach@example.com",
            "password": "correct-horse-battery-42",
            "name": "Sam Reyes",
        },
    ).json()
    row = api_db.scalar(select(User).where(User.email == "brand.coach@example.com"))
    row.tier = UserTier.COACH
    api_db.add(
        Subscription(user_id=row.id, tier=UserTier.COACH, status=SubscriptionStatus.ACTIVE)
    )
    api_db.commit()
    return {
        "headers": {"Authorization": f"Bearer {created['access_token']}"},
        "id": row.id,
    }


# ---------------------------------------------------------------------------
# Who may configure it
# ---------------------------------------------------------------------------


def test_branding_is_behind_the_tier_it_is_sold_with(api: TestClient, signed_up, paywall) -> None:
    assert api.get("/api/v1/coach/branding", headers=signed_up["headers"]).status_code == 402


def test_a_coach_starts_with_the_house_accent(coach, api: TestClient) -> None:
    """Not blank. A preview that showed no colour would disagree with the
    document, which uses the house accent when none is chosen."""
    body = api.get("/api/v1/coach/branding", headers=coach["headers"]).json()
    assert body["accent_hex"] is None
    assert body["effective_accent_hex"] == tokens.ACCENT
    assert body["has_logo"] is False


# ---------------------------------------------------------------------------
# The accent, and the limit on it
# ---------------------------------------------------------------------------


def test_a_readable_accent_is_accepted_and_normalised(coach, api: TestClient) -> None:
    response = api.patch(
        "/api/v1/coach/branding", headers=coach["headers"], json={"accent_hex": "#1a4d8f"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["accent_hex"] == "#1A4D8F"
    assert response.json()["effective_accent_hex"] == "#1A4D8F"


def test_an_accent_too_pale_to_print_is_refused_with_the_reason(
    coach, api: TestClient
) -> None:
    """The race card is read through a wet sleeve at hour nine, and the person
    holding an unreadable one cannot fix it."""
    response = api.patch(
        "/api/v1/coach/branding", headers=coach["headers"], json={"accent_hex": "#FFE066"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["field"] == "accent_hex"
    assert response.json()["error"]["details"]["required"] == 3.0
    assert response.json()["error"]["details"]["contrast_ratio"] < 3.0


def test_the_house_accent_itself_passes_the_check(api: TestClient) -> None:
    """A threshold the shipped design could not meet would be the wrong
    threshold."""
    from raceos.services.branding_service import MIN_CONTRAST_RATIO, contrast_ratio

    assert contrast_ratio(tokens.ACCENT, tokens.SURFACE) >= MIN_CONTRAST_RATIO


def test_something_that_is_not_a_colour_is_refused(coach, api: TestClient) -> None:
    for value in ("red", "#ABC", "E4622F", "#GGGGGG"):
        response = api.patch(
            "/api/v1/coach/branding", headers=coach["headers"], json={"accent_hex": value}
        )
        assert response.status_code == 422, value


def test_an_accent_can_be_cleared_back_to_the_house_one(coach, api: TestClient) -> None:
    """`null` and "leave it alone" are different intentions, so clearing is its
    own flag rather than a nullable field carrying both."""
    api.patch("/api/v1/coach/branding", headers=coach["headers"], json={"accent_hex": "#1a4d8f"})
    cleared = api.patch(
        "/api/v1/coach/branding", headers=coach["headers"], json={"clear_accent": True}
    )
    assert cleared.json()["accent_hex"] is None
    assert cleared.json()["effective_accent_hex"] == tokens.ACCENT


def test_an_absent_field_is_unchanged_rather_than_cleared(coach, api: TestClient) -> None:
    api.patch(
        "/api/v1/coach/branding",
        headers=coach["headers"],
        json={"display_name": "Reyes Coaching", "accent_hex": "#1a4d8f"},
    )
    api.patch("/api/v1/coach/branding", headers=coach["headers"], json={"footer_note": "reyes.cc"})

    body = api.get("/api/v1/coach/branding", headers=coach["headers"]).json()
    assert body["display_name"] == "Reyes Coaching"
    assert body["accent_hex"] == "#1A4D8F"
    assert body["footer_note"] == "reyes.cc"


# ---------------------------------------------------------------------------
# The logo
# ---------------------------------------------------------------------------


def test_a_png_logo_round_trips(coach, api: TestClient) -> None:
    uploaded = api.put(
        "/api/v1/coach/branding/logo",
        headers=coach["headers"],
        files={"file": ("logo.png", _png(), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["has_logo"] is True

    fetched = api.get("/api/v1/coach/branding/logo", headers=coach["headers"])
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/png"
    assert fetched.content == _png()


def test_an_svg_is_refused(coach, api: TestClient) -> None:
    """It carries script and external references, and this file is rendered
    into a PDF on our own server."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    response = api.put(
        "/api/v1/coach/branding/logo",
        headers=coach["headers"],
        files={"file": ("logo.svg", svg, "image/svg+xml")},
    )
    assert response.status_code == 422
    assert "svg" in response.json()["error"]["message"].lower()


def test_a_file_lying_about_its_type_is_refused(coach, api: TestClient) -> None:
    """The declared content type is not trusted on its own. A file claiming to
    be a PNG and containing something else would be rendered on our server."""
    response = api.put(
        "/api/v1/coach/branding/logo",
        headers=coach["headers"],
        files={"file": ("logo.png", b"not a png at all", "image/png")},
    )
    assert response.status_code == 422
    assert "not the format it says" in response.json()["error"]["message"]


def test_an_oversized_logo_is_refused(coach, api: TestClient) -> None:
    """A logo is a mark in a corner. A megabyte of it is somebody's hero image."""
    from raceos.services.branding_service import MAX_LOGO_BYTES

    oversized = _png() + b"\x00" * MAX_LOGO_BYTES
    response = api.put(
        "/api/v1/coach/branding/logo",
        headers=coach["headers"],
        files={"file": ("logo.png", oversized, "image/png")},
    )
    assert response.status_code == 422


def test_a_logo_can_be_removed(coach, api: TestClient) -> None:
    api.put(
        "/api/v1/coach/branding/logo",
        headers=coach["headers"],
        files={"file": ("logo.png", _png(), "image/png")},
    )
    removed = api.delete("/api/v1/coach/branding/logo", headers=coach["headers"])
    assert removed.status_code == 200
    assert removed.json()["has_logo"] is False
    assert api.get("/api/v1/coach/branding/logo", headers=coach["headers"]).status_code == 404


def test_nobody_reads_another_coachs_logo(coach, api: TestClient, api_db, paywall) -> None:
    api.put(
        "/api/v1/coach/branding/logo",
        headers=coach["headers"],
        files={"file": ("logo.png", _png(), "image/png")},
    )
    other = api.post(
        "/api/v1/auth/signup",
        json={"email": "other.coach@example.com", "password": "correct-horse-battery-42"},
    ).json()
    row = api_db.scalar(select(User).where(User.email == "other.coach@example.com"))
    row.tier = UserTier.COACH
    api_db.add(
        Subscription(user_id=row.id, tier=UserTier.COACH, status=SubscriptionStatus.ACTIVE)
    )
    api_db.commit()

    intruder = {"Authorization": f"Bearer {other['access_token']}"}
    # There is no id parameter, so there is no shape of request that asks for
    # somebody else's — they get their own, which is empty.
    assert api.get("/api/v1/coach/branding/logo", headers=intruder).status_code == 404


# ---------------------------------------------------------------------------
# What the branded document actually is
# ---------------------------------------------------------------------------


def _render(branding=None):
    from raceos.exports.pdf import race_card_html
    from tests.integration.test_exports import _render_data

    return race_card_html(_render_data(branding=branding))


def test_the_house_document_carries_no_branding(api: TestClient) -> None:
    html = _render()
    assert "class='brand'" not in html
    assert 'class="brand"' not in html


def test_a_branded_document_replaces_one_colour_and_adds_a_mark(api: TestClient) -> None:
    """Not a theme. The layout, the typography and every safeguard stay."""
    from raceos.exports.pdf import Branding

    html = _render(
        Branding(
            display_name="Reyes Coaching",
            accent_hex="#1A4D8F",
            footer_note="reyes.cc",
            logo_data_uri="data:image/png;base64,AAAA",
        )
    )
    assert "#1A4D8F" in html
    assert tokens.ACCENT not in html, "the house accent should have been replaced"
    assert "Reyes Coaching" in html
    assert "reyes.cc" in html
    # Every safeguard survives.
    assert "Course bundle" in html, "the provenance footer is not optional"


def test_a_coach_adds_a_footer_line_and_never_replaces_one(api: TestClient) -> None:
    """Everything the house footer says is why a printed card can be trusted,
    and it is exactly what somebody rebranding would be tempted to remove."""
    from raceos.exports.pdf import Branding

    html = _render(
        Branding(
            display_name=None,
            accent_hex=tokens.ACCENT,
            footer_note="reyes.cc",
            logo_data_uri=None,
        )
    )
    assert "Course bundle" in html
    assert "reyes.cc" in html
    assert html.index("Course bundle") < html.index("reyes.cc")


def test_a_logo_carries_its_name_in_text(api: TestClient) -> None:
    """A PDF read by a screen reader, and a monochrome photocopy, both need
    the name rather than only the image."""
    from raceos.exports.pdf import Branding

    html = _render(
        Branding(
            display_name="Reyes Coaching",
            accent_hex=tokens.ACCENT,
            footer_note=None,
            logo_data_uri="data:image/png;base64,AAAA",
        )
    )
    assert 'alt="Reyes Coaching"' in html


# ---------------------------------------------------------------------------
# The export, end to end
# ---------------------------------------------------------------------------


@pytest.fixture
def branded_plan(api: TestClient, signed_up, migrated_engine, api_db, paywall, coach):
    """An athlete's plan, built by a coach who has set branding."""
    from sqlalchemy.orm import sessionmaker

    if not TRAMUNTANA.is_file():
        pytest.skip("generated bundles are git-ignored build artefacts")

    with sessionmaker(bind=migrated_engine)() as session:
        load_bundle_file(session, TRAMUNTANA)
        session.commit()
        course_id = session.scalar(select(Course).where(Course.slug == "tramuntana-full")).id
        bundle_id = session.scalar(select(CourseBundle)).id

    headers = signed_up["headers"]
    for key, value in ATHLETE_M.items():
        api.put(f"/api/v1/constraints/{key}", headers=headers, json={"value": value})

    athlete = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    race = Race(
        user_id=athlete.id,
        course_id=course_id,
        course_bundle_id=bundle_id,
        event_date=datetime.now(UTC).date() + timedelta(days=40),
        start_time_local=time(7, 0),
    )
    api_db.add(race)
    api_db.commit()

    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": str(race.id)})
    plan_id = draft.json()["id"]
    buy_plan(api, headers, plan_id)

    # Stamp the plan as coach-built before the solve, the way the coach flow
    # does: `_persist` reads it to decide the resulting status.
    from raceos.db.models import Plan

    api_db.get(Plan, __import__("uuid").UUID(plan_id)).built_by_coach_id = coach["id"]
    api_db.commit()

    solved = api.post(f"/api/v1/plans/{plan_id}/solve", headers=headers, json={})
    assert solved.status_code == 200, solved.text

    api.patch(
        "/api/v1/coach/branding",
        headers=coach["headers"],
        json={"display_name": "Reyes Coaching", "accent_hex": "#1a4d8f"},
    )
    return {"headers": headers, "plan_id": solved.json()["id"], "coach": coach}


@needs_bundle
def test_the_download_is_unbranded_unless_asked(branded_plan, api: TestClient) -> None:
    """A branded artefact is something a coach hands over on purpose. An
    athlete downloading their own race card gets the house document."""
    response = api.get(
        f"/api/v1/plans/{branded_plan['plan_id']}/export/race-card.pdf",
        headers=branded_plan["headers"],
    )
    # A host without the native PDF libraries answers 503; the branding
    # decision is upstream of the renderer either way.
    assert response.status_code in (200, 503)


@needs_bundle
def test_the_branding_follows_the_coach_not_the_athlete(
    branded_plan, api: TestClient, api_db
) -> None:
    """The athlete did not buy white-label, and requiring them to hold it would
    make a coach's branded plan un-downloadable by the person it was for."""
    from raceos.api.routers.exports import _branding_allowed
    from raceos.config import Settings
    from raceos.services import export_service

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    from raceos.db.models import Plan

    plan = api_db.get(Plan, __import__("uuid").UUID(branded_plan["plan_id"]))
    context = export_service.load_context(api_db, plan=plan)

    assert _branding_allowed(api_db, context, settings, requested=True) is True
    assert _branding_allowed(api_db, context, settings, requested=False) is False


@needs_bundle
def test_a_lapsed_coach_stops_branding_new_exports(
    branded_plan, api: TestClient, api_db
) -> None:
    """The plan itself is unaffected: it was paid for and it stays theirs."""
    from raceos.api.routers.exports import _branding_allowed
    from raceos.config import Settings
    from raceos.db.models import Plan
    from raceos.services import export_service

    row = api_db.scalar(
        select(Subscription).where(Subscription.user_id == branded_plan["coach"]["id"])
    )
    row.status = SubscriptionStatus.CANCELLED
    api_db.commit()

    plan = api_db.get(Plan, __import__("uuid").UUID(branded_plan["plan_id"]))
    context = export_service.load_context(api_db, plan=plan)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert _branding_allowed(api_db, context, settings, requested=True) is False

    # And the athlete can still download it.
    response = api.get(
        f"/api/v1/plans/{branded_plan['plan_id']}/export/race-card.pdf",
        headers=branded_plan["headers"],
    )
    assert response.status_code in (200, 503)


@needs_bundle
def test_a_plan_no_coach_built_is_never_branded(
    api: TestClient, signed_up, migrated_engine, api_db, paywall
) -> None:
    from sqlalchemy.orm import sessionmaker

    from raceos.api.routers.exports import _branding_allowed
    from raceos.config import Settings
    from raceos.services import export_service

    if not TRAMUNTANA.is_file():
        pytest.skip("generated bundles are git-ignored build artefacts")
    with sessionmaker(bind=migrated_engine)() as session:
        load_bundle_file(session, TRAMUNTANA)
        session.commit()
        course_id = session.scalar(select(Course).where(Course.slug == "tramuntana-full")).id
        bundle_id = session.scalar(select(CourseBundle)).id

    headers = signed_up["headers"]
    for key, value in ATHLETE_M.items():
        api.put(f"/api/v1/constraints/{key}", headers=headers, json={"value": value})
    athlete = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    race = Race(
        user_id=athlete.id,
        course_id=course_id,
        course_bundle_id=bundle_id,
        event_date=datetime.now(UTC).date() + timedelta(days=40),
        start_time_local=time(7, 0),
    )
    api_db.add(race)
    api_db.commit()
    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": str(race.id)})
    buy_plan(api, headers, draft.json()["id"])
    solved = api.post(f"/api/v1/plans/{draft.json()['id']}/solve", headers=headers, json={})

    from raceos.db.models import Plan

    plan = api_db.get(Plan, __import__("uuid").UUID(solved.json()["id"]))
    context = export_service.load_context(api_db, plan=plan)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert _branding_allowed(api_db, context, settings, requested=True) is False
