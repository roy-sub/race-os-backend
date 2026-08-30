"""Publishing a course bundle, and the cascade that follows.

Republishing a bundle changes the ground under every plan pinned to the course.
Three rules govern it:

1. **Blast radius is previewable.** An operator sees how many athletes, races
   and plans a publish will touch *before* it happens, because "we did not
   realise it affected 400 plans" is not a recoverable mistake.
2. **The freeze window.** No publish lands Thursday to Sunday. Athletes are
   travelling, packing and racing, and a course change in that window arrives
   when they can least act on it.
3. **Law 3 all the way down.** Publishing does not rewrite a single plan. It
   raises a pending drift event per affected plan; the athlete decides.

The re-solve that follows an accepted drift is always free — the athlete did
not ask for the change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raceos.api.errors import Conflict, FreezeWindow, InvalidInput, NotFound
from raceos.config import Settings
from raceos.db.models import (
    Course,
    CourseBundle,
    CourseBundleDiff,
    CourseBundleLeg,
    Plan,
    Race,
    User,
)
from raceos.domain.enums import (
    BundleStatus,
    DriftCause,
    PlanStatus,
    RaceStatus,
)
from raceos.logging import get_logger
from raceos.services import drift_service

logger = get_logger(__name__)

#: Bundle fields whose change is worth showing an operator and an athlete.
#: Geometry is compared by leg totals rather than vertex-by-vertex: a
#: resampled line with the same length and climb is not a course change.
_COMPARED_BUNDLE_FIELDS = ("elevation_source", "attribution", "provenance")


@dataclass(frozen=True)
class AffectedPlan:
    plan_id: UUID
    race_id: UUID
    user_id: UUID
    event_date: Any
    days_away: int


@dataclass(frozen=True)
class BlastRadius:
    """What a publish would touch. Computed without writing anything."""

    course_id: UUID
    course_name: str
    from_bundle_version: str | None
    to_bundle_version: str
    athletes: int
    races: int
    plans: int
    #: Races inside the next week, which is where a course change hurts most.
    races_in_race_week: int
    field_deltas: list[dict[str, Any]]
    affected: list[AffectedPlan]
    freeze_blocked: bool
    freeze_reason: str = ""


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------


def _leg_totals(bundle: CourseBundle) -> dict[str, tuple[float, float]]:
    return {
        leg.leg.value: (float(leg.distance_m), float(leg.elevation_gain_m)) for leg in bundle.legs
    }


def _barrier_map(bundle: CourseBundle) -> dict[str, float]:
    out: dict[str, float] = {}
    for barrier in bundle.barriers or []:
        if isinstance(barrier, dict) and barrier.get("name") is not None:
            limit = barrier.get("limit_minutes_from_start")
            if isinstance(limit, int | float):
                out[str(barrier["name"])] = float(limit)
    return out


def _clock(minutes: float) -> str:
    total = int(round(minutes))
    return f"{total // 60}:{total % 60:02d}"


def diff_bundles(previous: CourseBundle | None, incoming: CourseBundle) -> list[dict[str, Any]]:
    """Field-level changes between two bundles, in the order they matter.

    Barriers first: a moved cut-off is the change that ends races. Then leg
    geometry, then metadata.
    """
    if previous is None:
        return [
            {
                "key": "bundle",
                "label": "Course bundle",
                "from": "none",
                "to": incoming.version,
            }
        ]

    deltas: list[dict[str, Any]] = []

    before_barriers = _barrier_map(previous)
    after_barriers = _barrier_map(incoming)
    for name in sorted(set(before_barriers) | set(after_barriers)):
        before = before_barriers.get(name)
        after = after_barriers.get(name)
        if before == after:
            continue
        deltas.append(
            {
                "key": f"barrier.{name}",
                "label": name.replace("_", " ").capitalize(),
                "from": _clock(before) if before is not None else "none",
                "to": _clock(after) if after is not None else "removed",
            }
        )

    before_legs = _leg_totals(previous)
    after_legs = _leg_totals(incoming)
    for leg in sorted(set(before_legs) | set(after_legs)):
        before_leg = before_legs.get(leg)
        after_leg = after_legs.get(leg)
        if before_leg is None or after_leg is None or before_leg == after_leg:
            continue
        if abs(after_leg[0] - before_leg[0]) >= 1.0:
            deltas.append(
                {
                    "key": f"leg.{leg.lower()}.distance_m",
                    "label": f"{leg.capitalize()} distance",
                    "from": f"{before_leg[0] / 1000:.2f} km",
                    "to": f"{after_leg[0] / 1000:.2f} km",
                }
            )
        if abs(after_leg[1] - before_leg[1]) >= 1.0:
            deltas.append(
                {
                    "key": f"leg.{leg.lower()}.elevation_gain_m",
                    "label": f"{leg.capitalize()} climb",
                    "from": f"{before_leg[1]:.0f} m",
                    "to": f"{after_leg[1]:.0f} m",
                }
            )

    before_aid = len(previous.aid_stations or [])
    after_aid = len(incoming.aid_stations or [])
    if before_aid != after_aid:
        deltas.append(
            {
                "key": "aid_stations",
                "label": "Aid stations",
                "from": str(before_aid),
                "to": str(after_aid),
            }
        )

    for field in _COMPARED_BUNDLE_FIELDS:
        before_value = getattr(previous, field)
        after_value = getattr(incoming, field)
        before_text = getattr(before_value, "value", before_value)
        after_text = getattr(after_value, "value", after_value)
        if before_text != after_text:
            deltas.append(
                {
                    "key": field,
                    "label": field.replace("_", " ").capitalize(),
                    "from": str(before_text),
                    "to": str(after_text),
                }
            )

    return deltas


# ---------------------------------------------------------------------------
# The freeze window
# ---------------------------------------------------------------------------


def freeze_check(settings: Settings, *, now: datetime | None = None) -> tuple[bool, str]:
    """Whether publishing is currently frozen, and why.

    Returns the reason rather than raising, so the blast-radius preview can
    *show* an operator that the window is closed instead of erroring when they
    ask a read-only question.
    """
    moment = now or datetime.now(UTC)
    day = moment.strftime("%a")
    if day in settings.freeze_day_set:
        days = ", ".join(sorted(settings.freeze_day_set))
        return True, (
            f"Course bundles are frozen on {days}. Athletes are travelling, "
            f"packing and racing, and a course change now arrives when they "
            f"can least act on it."
        )
    return False, ""


# ---------------------------------------------------------------------------
# Blast radius
# ---------------------------------------------------------------------------


def _affected_plans(session: Session, *, course_id: UUID, today: Any) -> list[AffectedPlan]:
    """Active plans on upcoming races pinned to this course.

    Past races are excluded: republishing a bundle does not change a race that
    has already happened, and telling an athlete their September plan drifted
    in November is noise.
    """
    rows = session.execute(
        select(Plan.id, Race.id, Plan.user_id, Race.event_date)
        .join(Race, Race.id == Plan.race_id)
        .where(
            Race.course_id == course_id,
            Race.status == RaceStatus.UPCOMING,
            Race.event_date >= today,
            Plan.status == PlanStatus.ACTIVE,
        )
    ).all()
    return [
        AffectedPlan(
            plan_id=plan_id,
            race_id=race_id,
            user_id=user_id,
            event_date=event_date,
            days_away=(event_date - today).days,
        )
        for plan_id, race_id, user_id, event_date in rows
    ]


def blast_radius(
    session: Session,
    *,
    bundle: CourseBundle,
    settings: Settings,
    now: datetime | None = None,
) -> BlastRadius:
    """What publishing *bundle* would touch. Writes nothing."""
    moment = now or datetime.now(UTC)
    course = session.get(Course, bundle.course_id)
    if course is None:  # pragma: no cover - FK RESTRICT
        raise NotFound("Course not found.")

    previous = current_active(session, course_id=bundle.course_id, exclude_id=bundle.id)
    affected = _affected_plans(session, course_id=bundle.course_id, today=moment.date())
    frozen, reason = freeze_check(settings, now=moment)

    return BlastRadius(
        course_id=course.id,
        course_name=course.name,
        from_bundle_version=previous.version if previous else None,
        to_bundle_version=bundle.version,
        athletes=len({item.user_id for item in affected}),
        races=len({item.race_id for item in affected}),
        plans=len(affected),
        races_in_race_week=len({item.race_id for item in affected if 0 <= item.days_away <= 7}),
        field_deltas=diff_bundles(previous, bundle),
        affected=affected,
        freeze_blocked=frozen,
        freeze_reason=reason,
    )


def current_active(
    session: Session, *, course_id: UUID, exclude_id: UUID | None = None
) -> CourseBundle | None:
    """The bundle this course is *currently served from*.

    Prefers `published`, and falls back to the newest `draft` — deliberately
    matching what :func:`course_service._active_bundle` serves. Pipeline
    bundles arrive as drafts and races get pinned to them, so a publish that
    only ever looked for a published predecessor would leave the first real
    publish with nothing to supersede: the draft everyone's plans were
    actually solved against would stay `draft` forever and keep winning the
    fallback. The diff would also be empty, telling athletes "the course
    bundle changed" while naming nothing.

    ``exclude_id`` keeps the incoming bundle out of its own comparison; it is
    a draft on the same course and could otherwise sort first.
    """
    not_itself = [CourseBundle.id != exclude_id] if exclude_id is not None else []

    published = session.scalar(
        select(CourseBundle)
        .where(
            CourseBundle.course_id == course_id,
            CourseBundle.status == BundleStatus.PUBLISHED,
            *not_itself,
        )
        .order_by(CourseBundle.published_at.desc().nullslast(), CourseBundle.version.desc())
        .limit(1)
    )
    if published is not None:
        return published
    return session.scalar(
        select(CourseBundle)
        .where(
            CourseBundle.course_id == course_id,
            CourseBundle.status == BundleStatus.DRAFT,
            *not_itself,
        )
        .order_by(CourseBundle.version.desc())
        .limit(1)
    )


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishResult:
    bundle: CourseBundle
    superseded: CourseBundle | None
    plans_affected: int
    drift_events_raised: int
    field_deltas: list[dict[str, Any]]


def publish(
    session: Session,
    *,
    bundle: CourseBundle,
    actor: User,
    settings: Settings,
    override_freeze: bool = False,
    now: datetime | None = None,
) -> PublishResult:
    """Publish, supersede, record the diff, and cascade drift events.

    ``override_freeze`` exists because a bundle correcting a *wrong* cut-off is
    more dangerous to withhold than to publish. It is a deliberate, audited
    act, not a default.
    """
    moment = now or datetime.now(UTC)
    if bundle.status is BundleStatus.PUBLISHED:
        raise Conflict(f"Bundle {bundle.version} is already published.")
    if bundle.status is BundleStatus.SUPERSEDED:
        raise Conflict(
            f"Bundle {bundle.version} was superseded and cannot be republished. "
            f"Create a new version."
        )

    frozen, reason = freeze_check(settings, now=moment)
    if frozen and not override_freeze:
        raise FreezeWindow(reason)

    if not bundle.legs:
        raise InvalidInput(
            f"Bundle {bundle.version} has no legs, so nothing would be solvable " f"against it."
        )

    radius = blast_radius(session, bundle=bundle, settings=settings, now=moment)
    previous = current_active(session, course_id=bundle.course_id, exclude_id=bundle.id)

    if previous is not None and previous.id != bundle.id:
        previous.status = BundleStatus.SUPERSEDED
        # Flushed before the new row is promoted: the published-bundle lookup
        # orders by `published_at`, and two rows briefly claiming the same
        # status is how a reader gets the wrong one.
        session.flush()
        session.add(
            CourseBundleDiff(
                from_bundle_id=previous.id,
                to_bundle_id=bundle.id,
                field_deltas=radius.field_deltas,
                computed_at=moment,
            )
        )

    bundle.status = BundleStatus.PUBLISHED
    bundle.published_at = moment
    bundle.plans_affected_count = radius.plans
    session.flush()

    raised = _cascade(session, bundle=bundle, affected=radius.affected, settings=settings)

    logger.info(
        "bundle.published",
        extra={
            "bundle_id": str(bundle.id),
            "version": bundle.version,
            "actor_user_id": str(actor.id),
            "plans_affected": radius.plans,
            "drift_events_raised": raised,
            "freeze_overridden": frozen and override_freeze,
        },
    )
    return PublishResult(
        bundle=bundle,
        superseded=previous if previous and previous.id != bundle.id else None,
        plans_affected=radius.plans,
        drift_events_raised=raised,
        field_deltas=radius.field_deltas,
    )


def _cascade(
    session: Session,
    *,
    bundle: CourseBundle,
    affected: list[AffectedPlan],
    settings: Settings,
) -> int:
    """Raise a pending drift event per affected plan. **Rewrites nothing.**

    Each plan is shadow-recomputed against the new bundle, so the athlete is
    told what actually changes for *them* rather than that "the course
    changed" — which for most of them means nothing at all.
    """
    raised = 0
    for item in affected:
        plan = session.get(Plan, item.plan_id)
        if plan is None:  # pragma: no cover - read a moment ago
            continue
        user = session.get(User, plan.user_id)
        race = session.get(Race, plan.race_id)
        if user is None or race is None:  # pragma: no cover - FK RESTRICT
            continue

        # Re-pin the race so the recompute reads the new geometry. This is the
        # one write the cascade makes, and it is not the plan: the plan keeps
        # every number it was solved with until the athlete applies the drift.
        previous_bundle_id = race.course_bundle_id
        race.course_bundle_id = bundle.id
        session.flush()

        assessment = drift_service.shadow_recompute(
            session,
            plan=plan,
            user=user,
            settings=settings,
            cause=DriftCause.COURSE_BUNDLE_CHANGE,
        )
        if not assessment.material:
            # Nothing moved for this athlete. Leave the race on the new
            # bundle — it is the current truth — but do not manufacture an
            # alert about a change with no consequences.
            logger.debug(
                "bundle.cascade_no_change",
                extra={"plan_id": str(plan.id), "from_bundle": str(previous_bundle_id)},
            )
            continue

        if drift_service.record(
            session, plan=plan, user=user, settings=settings, assessment=assessment
        ):
            raised += 1
    return raised


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def bundle_or_404(session: Session, bundle_id: UUID) -> CourseBundle:
    bundle = session.get(CourseBundle, bundle_id)
    if bundle is None:
        raise NotFound("Course bundle not found.")
    return bundle


def leg_count(session: Session, bundle_id: UUID) -> int:
    total = session.scalar(
        select(func.count())
        .select_from(CourseBundleLeg)
        .where(CourseBundleLeg.bundle_id == bundle_id)
    )
    return int(total or 0)
