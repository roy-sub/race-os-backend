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

from raceos.api.deps import Config, get_db
from raceos.api.schemas.course import (
    BundleDetail,
    BundleHistoryEntry,
    CourseDetail,
    Page,
)
from raceos.domain.enums import DistanceType
from raceos.services import course_service

router = APIRouter(prefix="/api/v1/courses", tags=["courses"])

DbSession = Annotated[Session, Depends(get_db)]


@router.get("", summary="Race directory")
def list_courses(
    session: DbSession,
    dist: Annotated[DistanceType | None, Query(description="Filter by distance type")] = None,
    q: Annotated[str | None, Query(description="Search name and place")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page:
    courses, total = course_service.list_courses(
        session, distance_type=dist, query=q, limit=limit, offset=offset
    )
    return Page(
        data=[c.model_dump(mode="json") for c in courses],
        meta={"total": total, "limit": limit, "offset": offset, "returned": len(courses)},
    )


@router.get("/{course_ref}", summary="Course detail")
def get_course(course_ref: str, session: DbSession) -> CourseDetail:
    return course_service.get_course(session, course_ref)


@router.get("/{course_ref}/bundle", summary="The bundle a client should read")
def get_bundle(course_ref: str, session: DbSession, response: Response) -> BundleDetail:
    bundle = course_service.get_active_bundle(session, course_ref)
    # A published bundle is immutable, so its version is a sound ETag and the
    # client can revalidate cheaply at race-mode check-in.
    response.headers["ETag"] = f'"{bundle.id}:{bundle.version}"'
    return bundle


@router.get("/{course_ref}/bundle/history", summary="Version list with changelogs")
def get_bundle_history(course_ref: str, session: DbSession) -> list[BundleHistoryEntry]:
    return course_service.get_bundle_history(session, course_ref)


@router.get("/{course_ref}/recon", summary="Everything the free recon page shows")
def get_recon(course_ref: str, session: DbSession, settings: Config) -> dict[str, object]:
    """Public and free, deliberately.

    The course library is the front door. Putting recon behind a paywall makes
    the product impossible to evaluate, and no athlete data is involved here —
    these numbers describe the course, not anyone racing it.
    """
    return course_service.course_recon(session, course_ref, settings)


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
