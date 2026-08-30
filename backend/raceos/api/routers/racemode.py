"""Race Mode: everything needed on course, in one request.

**Race day makes zero network requests.** The client caches this payload at
bike check-in and reads nothing afterwards, so a dead cell tower in a Mallorcan
valley changes nothing. That is only true if the payload is genuinely complete,
which is why this is one endpoint returning everything rather than a screen
that fetches as it goes.

It carries an ``ETag`` and a ``cached_at`` so the client can tell the athlete
how old their copy is — a stale plan that admits its age is safe; one that
looks current is not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Response

from raceos.api.deps import Config, CurrentUser, DbSession
from raceos.api.errors import Conflict, NotFound
from raceos.api.serialise import plan_detail
from raceos.db.models import Course, CourseBundle, Race
from raceos.domain.entitlements import EntitlementAction
from raceos.domain.enums import PlanStatus
from raceos.services import billing_service, drift_service, plan_service

router = APIRouter(prefix="/api/v1/plans", tags=["race-mode"])


@router.get("/{plan_id}/race-mode", summary="The complete offline race payload")
def race_mode(
    plan_id: UUID,
    response: Response,
    session: DbSession,
    user: CurrentUser,
    settings: Config,
) -> dict[str, object]:
    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)
    if plan.status is PlanStatus.DRAFT or plan.solved_at is None:
        raise Conflict("Solve this plan before taking it to the start line.")
    billing_service.require(
        session, user=user, action=EntitlementAction.RACE_MODE, race_id=plan.race_id
    )

    race = session.get(Race, plan.race_id)
    if race is None:  # pragma: no cover - FK RESTRICT
        raise NotFound("Race not found.")
    course = session.get(Course, race.course_id)
    bundle = session.get(CourseBundle, race.course_bundle_id)
    if course is None or bundle is None:  # pragma: no cover - FK RESTRICT
        raise NotFound("Course not found.")

    detail = plan_detail(session, plan).model_dump(mode="json")
    pending = drift_service.list_pending(session, plan=plan)
    cached_at = datetime.now(UTC)

    payload: dict[str, object] = {
        "cached_at": cached_at.isoformat(),
        "plan": {
            "id": str(plan.id),
            "version": plan.version,
            "solved_at": plan.solved_at.isoformat(),
            "projected_label": detail.get("projected_label"),
            "feasibility": plan.feasibility.value,
            "splits": detail.get("splits", []),
            "segments": detail.get("segments", []),
            "gates": detail.get("gates", []),
            "fuelling": detail.get("fuelling"),
            "aid_actions": detail.get("aid_actions", []),
            "bags": detail.get("bags", []),
            # The "Why this?" drawer travels too: it is the athlete's own
            # data, and on course with no signal is exactly when they most
            # want to know why a number is what it is.
            "constraint_refs": detail.get("constraint_refs", []),
            "assumed_fields": list(plan.assumed_fields or []),
        },
        "race": {
            "id": str(race.id),
            "event_date": race.event_date.isoformat(),
            "start_time_local": race.start_time_local.strftime("%H:%M"),
            "bib": race.bib,
            "timezone": course.timezone,
        },
        "course": {
            "id": str(course.id),
            "name": course.name,
            "place": course.place,
            "distance_type": course.distance_type.value,
            "lat": float(course.lat),
            "lng": float(course.lng),
        },
        "bundle": {
            "id": str(bundle.id),
            "version": bundle.version,
            "provenance": bundle.provenance.value,
            # ODbL travels with the geometry onto the athlete's phone.
            "attribution": bundle.attribution,
            "barriers": bundle.barriers,
            "aid_stations": bundle.aid_stations,
            "waypoints": bundle.waypoints,
            "segments": bundle.segments,
            "terrain_pmtiles_key": bundle.terrain_pmtiles_key,
        },
        "forecast": plan.forecast_snapshot or {},
        # Surfaced rather than hidden: if a drift is outstanding when the
        # athlete caches, they should know their copy predates a change they
        # have not applied — not discover it at kilometre ninety.
        "pending_drift": [
            {
                "id": str(event.id),
                "cause": event.cause.value,
                "severity": event.severity.value,
                "detected_at": event.detected_at.isoformat(),
                "field_deltas": event.field_deltas,
            }
            for event in pending
        ],
        "offline": {
            "complete": True,
            "note": (
                "Everything on this screen is in this payload. Race day makes "
                "no network requests."
            ),
        },
    }

    # A published bundle is immutable and a solved version never changes, so
    # the pair is a sound ETag: the client can revalidate cheaply at check-in
    # and skip the download when nothing moved.
    response.headers["ETag"] = f'"{plan.id}:{plan.version}:{bundle.version}"'
    # Cached by the client deliberately, but never by a shared proxy: this is
    # one athlete's race.
    response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    return payload
