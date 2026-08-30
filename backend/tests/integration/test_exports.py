"""Exports end to end: PDFs, course files, calendar.

The plan is solved by the real solver over the real Tramuntana bundle, so
every waypoint position and every printed number in these assertions came
through the same path a user's would.
"""

from __future__ import annotations

import re
from datetime import date, time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.db.models import Course, CourseBundle, Race, User
from raceos.exports import files, tokens
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

EVENT_DATE = date(2026, 9, 19)


@pytest.fixture
def solved_plan(api: TestClient, signed_up, migrated_engine, api_db, paywall):
    """A fully solved plan on the real course, ready to export."""
    from sqlalchemy.orm import sessionmaker

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
        assert (
            api.put(f"/api/v1/constraints/{key}", headers=headers, json={"value": value})
        ).status_code == 200

    user = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    race = Race(
        user_id=user.id,
        course_id=course_id,
        course_bundle_id=bundle_id,
        event_date=EVENT_DATE,
        start_time_local=time(7, 0),
    )
    api_db.add(race)
    api_db.commit()

    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": str(race.id)})
    assert draft.status_code == 201, draft.text
    buy_plan(api, headers, draft.json()["id"])
    solved = api.post(f"/api/v1/plans/{draft.json()['id']}/solve", headers=headers, json={})
    assert solved.status_code == 200, solved.text
    return {"headers": headers, "plan": solved.json(), "plan_id": solved.json()["id"]}


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


@needs_bundle
def test_the_manifest_lists_every_export_and_how_to_import_it(solved_plan, api: TestClient) -> None:
    """Writing the file is only half the promise; the rest is instructions."""
    body = api.get(
        f"/api/v1/plans/{solved_plan['plan_id']}/export", headers=solved_plan["headers"]
    ).json()

    keys = {export["key"] for export in body["exports"]}
    assert keys == {
        "race_card_pdf",
        "bag_manifests_pdf",
        "course_fit",
        "course_gpx",
        "race_week_ics",
    }
    assert body["attribution"], "ODbL attribution travels with the geometry"
    assert "garmin" in body["import_instructions"]
    assert "BIKE" in body["legs_with_geometry"]


@needs_bundle
def test_a_draft_cannot_be_exported(api: TestClient, signed_up, solved_plan) -> None:
    """409, not 404: the plan exists, its state is what makes this wrong."""
    headers = solved_plan["headers"]
    race_id = solved_plan["plan"]["race_id"]
    draft = api.post("/api/v1/plans", headers=headers, json={"race_id": race_id})
    response = api.get(f"/api/v1/plans/{draft.json()['id']}/export/race-card.pdf", headers=headers)
    assert response.status_code == 409
    assert "solved" in response.json()["error"]["message"].lower()


# ---------------------------------------------------------------------------
# PDFs
# ---------------------------------------------------------------------------


@needs_bundle
def test_the_race_card_is_a_pdf_and_downloads_rather_than_opens(
    solved_plan, api: TestClient
) -> None:
    response = api.get(
        f"/api/v1/plans/{solved_plan['plan_id']}/export/race-card.pdf",
        headers=solved_plan["headers"],
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "tramuntana-full-2026-09-19-race-card.pdf" in disposition
    assert response.headers["cache-control"] == "private, no-store"


@needs_bundle
def test_the_bag_manifests_are_a_pdf_with_a_page_for_every_bag(
    solved_plan, api: TestClient
) -> None:
    from weasyprint import HTML

    from raceos.api.serialise import plan_detail
    from raceos.db.session import session_scope
    from raceos.services import export_service

    response = api.get(
        f"/api/v1/plans/{solved_plan['plan_id']}/export/bags.pdf",
        headers=solved_plan["headers"],
    )
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")

    # Page count is asserted through the same renderer rather than by parsing
    # the PDF back: five bags, always, even when one of them is empty.
    from uuid import UUID

    from raceos.db.models import Plan

    with session_scope() as session:
        plan = session.get(Plan, UUID(solved_plan["plan_id"]))
        context = export_service.load_context(session, plan=plan)
        data = export_service.build_render_data(context, plan_detail(session, plan))

    from raceos.exports import pdf as pdf_module

    document = HTML(string=pdf_module.bag_manifest_html(data)).render()
    assert len(data.bags) == 5
    assert len(document.pages) >= 5


@needs_bundle
def test_every_pdf_carries_the_provenance_footer(solved_plan) -> None:
    """Its absence was a real past incident (Part 6.2), so it is a test."""
    from uuid import UUID

    from raceos.api.serialise import plan_detail
    from raceos.db.models import Plan
    from raceos.db.session import session_scope
    from raceos.exports import pdf as pdf_module
    from raceos.services import export_service

    with session_scope() as session:
        plan = session.get(Plan, UUID(solved_plan["plan_id"]))
        context = export_service.load_context(session, plan=plan)
        data = export_service.build_render_data(context, plan_detail(session, plan))

    for markup in (pdf_module.race_card_html(data), pdf_module.bag_manifest_html(data)):
        assert 'id="provenance"' in markup
        assert data.bundle_version in markup
        assert data.attribution.split("·")[0].strip()[:12] in markup


def test_the_footer_marks_estimated_values_with_a_glyph_not_a_colour() -> None:
    from raceos.exports.pdf import PROVENANCE_MARK, _footer

    data = _render_data(
        constraint_refs=[{"key": "ftp_w", "source_label": "estimated"}],
        assumed_fields=["athlete.sweat_rate_l_per_hr"],
    )
    footer = _footer(data)
    assert PROVENANCE_MARK in footer
    assert "ftp w" in footer
    assert "sweat_rate_l_per_hr" in footer


def test_no_gate_state_is_communicated_by_colour_alone() -> None:
    """A race card is read through a wet sleeve, and printed in mono."""
    from raceos.exports.pdf import race_card_html

    markup = race_card_html(
        _render_data(
            gates=[
                {
                    "name": "bike_cutoff",
                    "limit_minutes": 330.0,
                    "eta_minutes": 316.0,
                    "state": state,
                    "margin_label": "+0:14",
                }
                for state in ("clear", "tight", "bad")
            ]
        )
    )
    for state, glyph in tokens.STATE_GLYPHS.items():
        colour = tokens.STATE_COLOURS[state]
        assert colour in markup
        # Every coloured cell also carries its glyph.
        assert f"<span class='glyph'>{glyph}</span>" in markup


def test_the_card_never_sets_type_below_the_legible_floor() -> None:
    from raceos.exports.pdf import race_card_html

    sizes = [
        float(size)
        for size in re.findall(r"font-size:\s*([\d.]+)pt", race_card_html(_render_data()))
    ]
    assert sizes, "the stylesheet sets type sizes in points"
    body_sizes = [size for size in sizes if size >= tokens.MIN_PRINT_PT]
    # Only the provenance footer is allowed under the floor: it is a
    # reference line, not something read at speed.
    assert len(sizes) - len(body_sizes) <= 1


def _render_data(**overrides):
    from raceos.exports.pdf import PlanRenderData

    base = {
        "athlete_name": "Elena Marsh",
        "course_name": "Tramuntana",
        "course_place": "Mallorca",
        "event_date": "2026-09-19",
        "start_time": "07:00",
        "bundle_version": "2026.3.1",
        "bundle_provenance": "OFFICIAL",
        "attribution": "© OpenStreetMap contributors",
        "projected_label": "10:41",
        "feasibility": "TIGHT",
        "splits": [],
        "gates": [],
        "segments": [],
        "fuelling": {},
        "aid_actions": [],
        "bags": [],
        "constraint_refs": [],
        "assumed_fields": [],
    }
    base.update(overrides)
    return PlanRenderData(**base)


# ---------------------------------------------------------------------------
# Course files
# ---------------------------------------------------------------------------


@needs_bundle
def test_the_fit_course_is_a_valid_fit_file_with_waypoints(solved_plan, api: TestClient) -> None:
    """A head unit rejects a file whose CRC is wrong, so the CRC is asserted."""
    response = api.get(
        f"/api/v1/plans/{solved_plan['plan_id']}/export/course.fit?leg=BIKE",
        headers=solved_plan["headers"],
    )
    assert response.status_code == 200
    body = response.content
    assert body[8:12] == b".FIT"
    assert files._fit_crc(body[:-2]) == int.from_bytes(body[-2:], "little")
    # Waypoint names are written as UTF-8 strings in course_point messages.
    assert b"Aid" in body or b"aid" in body or b"cut" in body.lower()


@needs_bundle
def test_the_fit_and_gpx_describe_the_same_geometry(solved_plan, api: TestClient) -> None:
    from uuid import UUID

    from raceos.db.models import Plan
    from raceos.db.session import session_scope
    from raceos.domain.enums import Leg
    from raceos.services import export_service

    with session_scope() as session:
        plan = session.get(Plan, UUID(solved_plan["plan_id"]))
        context = export_service.load_context(session, plan=plan)
        points = export_service.route_points(context.bundle, Leg.BIKE)

    gpx = api.get(
        f"/api/v1/plans/{solved_plan['plan_id']}/export/course.gpx?leg=BIKE",
        headers=solved_plan["headers"],
    ).content.decode()
    assert gpx.count("<trkpt") == len(points)
    assert f'lat="{points[0].lat:.6f}"' in gpx
    # ODbL: attribution travels with the derived data, into the file.
    assert "OpenStreetMap" in gpx or "Mapterhorn" in gpx


@needs_bundle
def test_waypoints_land_on_the_route_at_their_declared_distance(solved_plan) -> None:
    from uuid import UUID

    from raceos.db.models import Plan
    from raceos.db.session import session_scope
    from raceos.domain.enums import Leg
    from raceos.services import export_service

    with session_scope() as session:
        plan = session.get(Plan, UUID(solved_plan["plan_id"]))
        context = export_service.load_context(session, plan=plan)
        points = export_service.route_points(context.bundle, Leg.BIKE)
        waypoints = export_service.leg_waypoints(context.bundle, Leg.BIKE, points)

    assert waypoints, "the bike leg has aid stations and cut-offs"
    assert [w.distance_m for w in waypoints] == sorted(w.distance_m for w in waypoints)
    for waypoint in waypoints:
        nearest = min(points, key=lambda p: abs(p.distance_m - waypoint.distance_m))
        # Interpolated between adjacent vertices, so it sits within one node
        # spacing of the nearest vertex and inside the leg.
        assert abs(nearest.distance_m - waypoint.distance_m) <= 60.0
        assert 0.0 <= waypoint.distance_m <= points[-1].distance_m + 1.0


@needs_bundle
def test_the_swim_leg_is_not_offered_as_a_course_file(solved_plan, api: TestClient) -> None:
    """A head unit follows one route; SWIM is not a leg anyone navigates."""
    body = api.get(
        f"/api/v1/plans/{solved_plan['plan_id']}/export", headers=solved_plan["headers"]
    ).json()
    assert "SWIM" not in body["legs_with_geometry"]


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


@needs_bundle
def test_race_week_is_anchored_to_the_event_date(solved_plan, api: TestClient) -> None:
    response = api.get(
        f"/api/v1/plans/{solved_plan['plan_id']}/export/race-week.ics",
        headers=solved_plan["headers"],
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert body.startswith("BEGIN:VCALENDAR")
    assert body.rstrip().endswith("END:VCALENDAR")
    assert "DTSTART;VALUE=DATE:20260919" in body, "race day is the event date"
    assert "DTSTART;VALUE=DATE:20260916" in body, "registration is three days before"


def test_calendar_dates_move_with_the_event_not_with_weekday_names() -> None:
    """The mock's "Thursday: check-in" is a rendering of "two days before"."""
    midweek = files.race_week_events(
        event_date=date(2026, 6, 3), course_name="X", has_special_needs=False
    )
    assert [event.on_date for event in midweek] == [
        date(2026, 5, 31),
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 3),
    ]


def test_ics_escapes_the_characters_that_would_break_the_format() -> None:
    body = files.render_ics(
        events=[
            files.CalendarEvent(
                summary="Race; day, at last",
                description="Line one\nline two",
                on_date=date(2026, 6, 3),
            )
        ],
        calendar_name="X",
    ).decode()
    assert "SUMMARY:Race\\; day\\, at last" in body
    assert "DESCRIPTION:Line one\\nline two" in body


# ---------------------------------------------------------------------------
# Authorization — every export path
# ---------------------------------------------------------------------------


EXPORT_PATHS = (
    "/export",
    "/export/race-card.pdf",
    "/export/bags.pdf",
    "/export/course.fit",
    "/export/course.gpx",
    "/export/race-week.ics",
)


@needs_bundle
@pytest.mark.parametrize("suffix", EXPORT_PATHS)
def test_every_export_rejects_an_absent_token(solved_plan, api: TestClient, suffix: str) -> None:
    assert api.get(f"/api/v1/plans/{solved_plan['plan_id']}{suffix}").status_code == 401


@needs_bundle
@pytest.mark.parametrize("suffix", EXPORT_PATHS)
def test_another_athlete_cannot_download_this_plan(
    solved_plan, api: TestClient, suffix: str
) -> None:
    """A race card names the athlete and says where they will be at 09:00."""
    other = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "jonas.feldt@example.com",
            "password": "correct-horse-battery",
            "name": "Jonas Feldt",
        },
    ).json()
    headers = {"Authorization": f"Bearer {other['access_token']}"}
    response = api.get(f"/api/v1/plans/{solved_plan['plan_id']}{suffix}", headers=headers)
    assert response.status_code == 403
