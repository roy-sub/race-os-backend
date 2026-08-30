"""Exports: race card, bag manifests, course files, race-week calendar.

Every export is generated on request from the plan as it currently stands and
streamed straight back — nothing is stored, so an export cannot go stale
behind the plan it describes.

All five are owner-only. There is no unauthenticated download path and no
guessable URL: a race card names the athlete, their target power and where
they will be at nine in the morning, which is exactly the sort of thing that
must not be readable by anyone holding a plan id.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response

from raceos.api.deps import CurrentUser, DbSession
from raceos.api.serialise import plan_detail
from raceos.domain.entitlements import EntitlementAction
from raceos.domain.enums import Leg
from raceos.exports import files, pdf
from raceos.services import billing_service, export_service, plan_service

router = APIRouter(prefix="/api/v1/plans", tags=["exports"])


def _download(content: bytes, *, media_type: str, filename: str) -> Response:
    """`attachment` rather than `inline`.

    A PDF rendered inside the browser tab is a PDF the athlete has not saved,
    and the point of these files is that they exist on race morning when the
    venue has no signal.
    """
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Generated per request from live data; a cached copy would be a
            # stale plan wearing a current URL.
            "Cache-Control": "private, no-store",
        },
    )


def _context(session: DbSession, plan_id: UUID, user: CurrentUser) -> export_service.ExportContext:
    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)
    export_service.require_exportable(plan)
    # Exports are part of the race plan the athlete bought, so the entitlement
    # is the captured purchase for this race — which outlives a cancelled
    # subscription. A plan you paid for stays yours.
    billing_service.require(
        session, user=user, action=EntitlementAction.EXPORT_PLAN, race_id=plan.race_id
    )
    return export_service.load_context(session, plan=plan)


@router.get("/{plan_id}/export", summary="What this plan can be exported as")
def list_exports(plan_id: UUID, session: DbSession, user: CurrentUser) -> dict[str, object]:
    """The manifest the download drawer renders.

    It also carries the head-unit import instructions, because writing the
    file is only half of the promise — the athlete still has to get it onto
    the device, and RaceOS has no integration that could do it for them.
    """
    context = _context(session, plan_id, user)
    return {
        "plan_id": str(plan_id),
        "plan_version": context.plan.version,
        "course_bundle_version": context.bundle.version,
        "attribution": context.bundle.attribution,
        "exports": export_service.available_exports(plan_id),
        "legs_with_geometry": [
            leg.value
            for leg in export_service.EXPORTABLE_LEGS
            if any(row.leg is leg for row in context.bundle.legs)
        ],
        "import_instructions": files.FIT_IMPORT_INSTRUCTIONS,
    }


@router.get(
    "/{plan_id}/export/race-card.pdf",
    response_class=Response,
    summary="The race card, as a printable A5 page",
)
def export_race_card(plan_id: UUID, session: DbSession, user: CurrentUser) -> Response:
    context = _context(session, plan_id, user)
    detail = plan_detail(session, context.plan)
    document = pdf.render_race_card(export_service.build_render_data(context, detail))
    return _download(
        document,
        media_type="application/pdf",
        filename=export_service.export_filename(
            course_slug=context.course.slug,
            event_date=context.race.event_date,
            suffix="race-card.pdf",
        ),
    )


@router.get(
    "/{plan_id}/export/bags.pdf",
    response_class=Response,
    summary="Bag manifests, one page per bag",
)
def export_bag_manifests(plan_id: UUID, session: DbSession, user: CurrentUser) -> Response:
    context = _context(session, plan_id, user)
    detail = plan_detail(session, context.plan)
    document = pdf.render_bag_manifests(export_service.build_render_data(context, detail))
    return _download(
        document,
        media_type="application/pdf",
        filename=export_service.export_filename(
            course_slug=context.course.slug,
            event_date=context.race.event_date,
            suffix="bags.pdf",
        ),
    )


@router.get(
    "/{plan_id}/export/course.fit",
    response_class=Response,
    summary="A head-unit course file with waypoints",
)
def export_fit(
    plan_id: UUID,
    session: DbSession,
    user: CurrentUser,
    leg: Annotated[Leg, Query(description="Which leg to export")] = Leg.BIKE,
) -> Response:
    context = _context(session, plan_id, user)
    points = export_service.route_points(context.bundle, leg)
    document = files.render_fit_course(
        course_name=f"{context.course.name} {leg.value.lower()}",
        points=points,
        waypoints=export_service.leg_waypoints(context.bundle, leg, points),
    )
    return _download(
        document,
        media_type="application/vnd.ant.fit",
        filename=export_service.export_filename(
            course_slug=context.course.slug,
            event_date=context.race.event_date,
            suffix=f"{leg.value.lower()}.fit",
        ),
    )


@router.get(
    "/{plan_id}/export/course.gpx",
    response_class=Response,
    summary="The route as GPX, for anything that will not take a .fit",
)
def export_gpx(
    plan_id: UUID,
    session: DbSession,
    user: CurrentUser,
    leg: Annotated[Leg, Query(description="Which leg to export")] = Leg.BIKE,
) -> Response:
    context = _context(session, plan_id, user)
    document = files.render_gpx(
        course_name=f"{context.course.name} {leg.value.lower()}",
        points=export_service.route_points(context.bundle, leg),
        attribution=context.bundle.attribution,
    )
    return _download(
        document,
        media_type="application/gpx+xml",
        filename=export_service.export_filename(
            course_slug=context.course.slug,
            event_date=context.race.event_date,
            suffix=f"{leg.value.lower()}.gpx",
        ),
    )


@router.get(
    "/{plan_id}/export/race-week.ics",
    response_class=Response,
    summary="Race week as calendar events",
)
def export_calendar(plan_id: UUID, session: DbSession, user: CurrentUser) -> Response:
    context = _context(session, plan_id, user)
    document = files.render_ics(
        events=export_service.calendar_events(context),
        calendar_name=f"{context.course.name} race week",
    )
    return _download(
        document,
        media_type="text/calendar",
        filename=export_service.export_filename(
            course_slug=context.course.slug,
            event_date=context.race.event_date,
            suffix="race-week.ics",
        ),
    )
