"""Course and bundle reads. Public — no athlete data is involved.

Every response that carries geometry also carries ``attribution``. ODbL
obliges attribution wherever the derived data is displayed, and a client
cannot render what it was not given, so handing them together is what makes
the obligation structural rather than a note in a document.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from raceos.api.deps import Config, OptionalUser, get_db
from raceos.api.errors import NotFound, PaymentRequired
from raceos.api.schemas.course import (
    BundleDetail,
    BundleHistoryEntry,
    ConditionsHistoryOut,
    ConditionsObservationOut,
    ConditionsSummaryOut,
    CourseDetail,
    Page,
)
from raceos.domain.enums import DistanceType
from raceos.services import conditions_service, course_service, terrain_service

router = APIRouter(prefix="/api/v1/courses", tags=["courses"])

DbSession = Annotated[Session, Depends(get_db)]


@router.get("", summary="Race directory")
def list_courses(
    session: DbSession,
    settings: Config,
    viewer: OptionalUser,
    dist: Annotated[DistanceType | None, Query(description="Filter by distance type")] = None,
    q: Annotated[str | None, Query(description="Search name and place")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page:
    """Public, and *narrower* when signed in rather than wider.

    A signed-out visitor sees the marketing showcase alongside the season; a
    signed-in athlete sees the season and their own submitted courses, and not
    the showcase — see ``course_service.visible_to`` for why.
    """
    courses, total = course_service.list_courses(
        session,
        distance_type=dist,
        query=q,
        limit=limit,
        offset=offset,
        viewer=viewer,
        settings=settings,
    )
    return Page(
        data=[c.model_dump(mode="json") for c in courses],
        meta={"total": total, "limit": limit, "offset": offset, "returned": len(courses)},
    )


@router.get("/{course_ref}", summary="Course detail")
def get_course(
    course_ref: str, session: DbSession, settings: Config, viewer: OptionalUser
) -> CourseDetail:
    return course_service.get_course(session, course_ref, viewer, settings)


@router.get("/{course_ref}/bundle", summary="The bundle a client should read")
def get_bundle(course_ref: str, session: DbSession, response: Response) -> BundleDetail:
    bundle = course_service.get_active_bundle(session, course_ref)
    # A published bundle is immutable, so its version is a sound ETag and the
    # client can revalidate cheaply at race-mode check-in.
    response.headers["ETag"] = f'"{bundle.id}:{bundle.version}"'
    return bundle


@router.get("/{course_ref}/conditions-history", summary="What race day has actually been like")
def get_conditions_history(
    course_ref: str, session: DbSession, settings: Config, viewer: OptionalUser
) -> ConditionsHistoryOut:
    """Public, like the rest of recon. **Observed, never modelled.**

    Every figure is reanalysis from the weather archive for this course's own
    coordinates, at the race's own start hour, on the day the race was held.
    Where a value is unavailable it is absent: a lake course has no
    sea-surface temperature, so it has no water temperature and therefore no
    wetsuit likelihood, rather than one inferred from the air.

    There is no finish-time distribution. It needs actual results, which this
    system does not have and cannot obtain, and the prototype's was drawn.
    """
    course = course_service.load_visible_course(session, course_ref, viewer)
    rows = conditions_service.history_for(session, course=course)
    summary = conditions_service.summarise(rows)

    empty_reason: str | None = None
    if not rows:
        empty_reason = (
            "no_edition_date" if course.next_edition_date is None else "not_collected_yet"
        )

    return ConditionsHistoryOut(
        course_slug=course.slug,
        course_name=course.name,
        summary=ConditionsSummaryOut.model_validate(summary),
        observations=[ConditionsObservationOut.model_validate(row) for row in rows],
        finish_times_available=False,
        empty_reason=empty_reason,
    )


@router.get("/{course_ref}/bundle/history", summary="Version list with changelogs")
def get_bundle_history(course_ref: str, session: DbSession) -> list[BundleHistoryEntry]:
    return course_service.get_bundle_history(session, course_ref)


@router.get("/{course_ref}/recon", summary="Everything the recon page shows")
def get_recon(
    course_ref: str, session: DbSession, settings: Config, viewer: OptionalUser
) -> dict[str, object]:
    """Public, with the map itself gated.

    What a race is chosen on — where, how far, how much climbing, the tightest
    cut-off — is free to everyone including signed-out visitors, because a
    directory nobody can evaluate is not a front door. The surveyed geometry
    and the furniture around it are part of the race plan, and arrive with
    ``access.map_unlocked`` false and a reason rather than silently missing.
    """
    return course_service.course_recon(session, course_ref, settings, viewer)


@router.get("/{course_ref}/terrain", summary="The 3D map's terrain field")
def get_terrain(
    course_ref: str, session: DbSession, settings: Config, viewer: OptionalUser
) -> dict[str, object]:
    """Everything the 3D course map needs, already in slab coordinates.

    The browser does no geography: it receives a height grid, a water mask, a
    distance-to-shore field and three route lines, all projected, scaled and
    draped here. See :mod:`raceos.services.terrain_service` — and
    ``course-map-3d/TRACKS.md`` §5-6, which is the specification this
    implements.

    Behind the same gate as the rest of the map. The showcase course is open
    to everyone, because a marketing map nobody can see advertises nothing.
    """
    course = course_service._load_course(session, course_ref)
    if not course_service.visible_to(course, viewer):
        raise NotFound(f"No course {course_ref!r}.")
    unlocked, reason = course_service.map_access(session, course, viewer, settings)
    if not unlocked:
        raise PaymentRequired(reason or "This course's map is part of a race plan.")

    bundle = course_service._active_bundle(session, course.id)
    if bundle is None:
        raise NotFound(f"{course.name} has no course data yet.")
    return terrain_service.field_for(session, course=course, bundle=bundle, settings=settings)


class CutoffQuery(BaseModel):
    projected_minutes: float = Field(gt=0, le=2400)


@router.post("/{course_ref}/cutoff-check", summary="The free cut-off calculator")
def cutoff_check(course_ref: str, payload: CutoffQuery, session: DbSession) -> dict[str, object]:
    """ "If I finish in this time, which cut-offs am I near?"

    A straight-line estimate from the published limits, not a solve — and it
    says so in every row. It answers the question someone deciding whether to
    enter is actually asking, without an account.
    """
    bundle = course_service.get_active_bundle(session, course_ref)
    rows = course_service.cutoff_feasibility(
        barriers=list(bundle.barriers or []),
        projected_minutes=payload.projected_minutes,
    )
    return {
        "course_ref": course_ref,
        "bundle_version": bundle.version,
        "projected_minutes": payload.projected_minutes,
        "barriers": rows,
        "at_risk_count": sum(1 for row in rows if row["at_risk"]),
    }
