"""Coaches: invite, accept, permissions, notes, and the race-week board.

**A coach never holds a constraint permission.** There is no
``perm_constraints`` column and no code path here that writes one — the three
permissions are ``plans``, ``build`` and ``analysis``. Constraints are not a
permission that happens to be off; they are structurally unreachable, which is
the first structural guarantee expressed as an absence.

Permissions are checked **live, per request**, never cached at link time. A
revocation has to take effect on a page that is already open, and a cached
grant is exactly how that fails.

Linking is always athlete-initiated in its final step: a coach invites, the
athlete accepts. There is no coach-side path that silently attaches an
athlete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raceos.api.errors import Conflict, Forbidden, InvalidInput, NotFound
from raceos.config import Settings
from raceos.db.models import (
    CoachAthleteLink,
    CoachNote,
    Plan,
    Race,
    User,
)
from raceos.domain.entitlements import athlete_seats
from raceos.domain.enums import (
    CoachLinkStatus,
    NotificationSeverity,
    NotificationType,
    PlanStatus,
    RaceStatus,
)
from raceos.logging import get_logger
from raceos.services import notification_service, security

logger = get_logger(__name__)

#: The three permissions a coach can hold. Enumerated here so a future
#: "constraints" string cannot be smuggled in through a request body.
COACH_PERMISSIONS: tuple[str, ...] = ("plans", "build", "analysis")

#: How long an invite stays open. Long enough for an athlete on a training
#: camp; short enough that a forwarded email is not a permanent key.
INVITE_TTL_DAYS = 14

#: Race week, for the board.
BOARD_HORIZON_DAYS = 7


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Invite:
    link: CoachAthleteLink
    #: Returned once, at creation. Only its hash is stored.
    token: str


def _seat_count(session: Session, coach_id: UUID) -> int:
    total = session.scalar(
        select(func.count())
        .select_from(CoachAthleteLink)
        .where(
            CoachAthleteLink.coach_id == coach_id,
            CoachAthleteLink.status.in_((CoachLinkStatus.ACTIVE, CoachLinkStatus.PENDING)),
        )
    )
    return int(total or 0)


def invite(
    session: Session,
    *,
    coach: User,
    athlete_email: str,
    settings: Settings,
) -> Invite:
    """Create a pending link and a one-time invite token.

    A pending invite occupies a seat. Otherwise a coach at their limit could
    hold fifteen athletes and an unbounded queue of outstanding invitations,
    every one of which would exceed the limit the moment it was accepted.
    """
    athlete = session.scalar(select(User).where(User.email == athlete_email.lower()))
    if athlete is None:
        # Deliberately the same message whichever way it fails: whether an
        # email address has an account is not a coach's business to enumerate.
        raise NotFound(
            "No athlete could be invited with that address. Ask them to sign "
            "up first, then invite them again."
        )
    if athlete.id == coach.id:
        raise InvalidInput("You cannot invite yourself.", field="athlete_email")

    seats = athlete_seats(coach.tier)
    if seats <= 0:
        raise Forbidden(
            "Managing athletes needs a Season or Coach subscription.",
            details={"required_tiers": ["season", "coach"]},
        )
    existing = session.scalar(
        select(CoachAthleteLink).where(
            CoachAthleteLink.coach_id == coach.id,
            CoachAthleteLink.athlete_id == athlete.id,
        )
    )
    if existing is not None and existing.status is not CoachLinkStatus.REVOKED:
        raise Conflict(
            f"You already have {'an active link with' if existing.status is CoachLinkStatus.ACTIVE else 'a pending invite to'} this athlete."
        )
    if _seat_count(session, coach.id) >= seats:
        raise Forbidden(
            f"Your plan covers {seats} athlete{'s' if seats != 1 else ''}, "
            f"including pending invites. Revoke one to invite another.",
            details={"seats": seats},
        )

    issued = security.issue_token()
    now = datetime.now(UTC)
    link = existing or CoachAthleteLink(coach_id=coach.id, athlete_id=athlete.id)
    link.status = CoachLinkStatus.PENDING
    link.invited_at = now
    link.accepted_at = None
    link.revoked_at = None
    link.invite_token_hash = issued.hashed
    link.invite_expires_at = now + timedelta(days=INVITE_TTL_DAYS)
    # Retained only while email delivery is a no-op, so support can hand the
    # invite over manually. Admin-only; never in a public response.
    link.invite_delivery_link = f"{settings.app_base_url}/coach/accept?token={issued.raw}"
    # Permissions always start empty. An accepted invite grants nothing until
    # the athlete chooses what to grant.
    link.perm_plans = False
    link.perm_build = False
    link.perm_analysis = False
    if existing is None:
        session.add(link)
    session.flush()

    notification_service.notify(
        session,
        user=athlete,
        settings=settings,
        type_key=NotificationType.DIGEST,
        severity=NotificationSeverity.INFO,
        title=f"{coach.name or 'A coach'} invited you to link accounts.",
        body=(
            "Linking lets them see the plans you choose to share. They can "
            "never see or change your constraints."
        ),
        tag="COACH INVITE",
        cta_label="Review the invite",
        cta_href="/settings?tab=coach",
    )

    logger.info(
        "coach.invited",
        extra={"coach_id": str(coach.id), "athlete_id": str(athlete.id)},
    )
    return Invite(link=link, token=issued.raw)


def accept_invite(
    session: Session, *, athlete: User, token: str, settings: Settings
) -> CoachAthleteLink:
    """**Only the athlete can accept.** There is no coach-side shortcut."""
    link = session.scalar(
        select(CoachAthleteLink).where(
            CoachAthleteLink.invite_token_hash == security.hash_token(token)
        )
    )
    if link is None or link.status is not CoachLinkStatus.PENDING:
        raise NotFound("That invite is not valid.")
    if link.athlete_id != athlete.id:
        # The token is valid but belongs to somebody else. Same message: a
        # forwarded invite must not tell the wrong person it is real.
        raise NotFound("That invite is not valid.")
    if link.invite_expires_at is None or link.invite_expires_at <= datetime.now(UTC):
        raise Conflict("That invite has expired. Ask your coach to send a new one.")

    link.status = CoachLinkStatus.ACTIVE
    link.accepted_at = datetime.now(UTC)
    # Spent immediately: an accepted invite must not be replayable.
    link.invite_token_hash = None
    link.invite_delivery_link = None
    session.flush()

    coach = session.get(User, link.coach_id)
    if coach is not None:
        notification_service.notify(
            session,
            user=coach,
            settings=settings,
            type_key=NotificationType.DIGEST,
            severity=NotificationSeverity.OK,
            title=f"{athlete.name or 'An athlete'} accepted your invite.",
            body="They control what you can see. Nothing is shared until they grant it.",
            tag="COACH LINK",
            cta_label="Open the board",
            cta_href="/coach",
        )
    logger.info("coach.invite_accepted", extra={"link_id": str(link.id)})
    return link


def decline_invite(session: Session, *, athlete: User, link_id: UUID) -> CoachAthleteLink:
    link = session.get(CoachAthleteLink, link_id)
    if link is None or link.athlete_id != athlete.id:
        raise NotFound("Invite not found.")
    if link.status is not CoachLinkStatus.PENDING:
        raise Conflict(f"That invite is already {link.status.value}.")
    link.status = CoachLinkStatus.REVOKED
    link.revoked_at = datetime.now(UTC)
    link.invite_token_hash = None
    link.invite_delivery_link = None
    session.flush()
    return link


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def set_permissions(
    session: Session,
    *,
    athlete: User,
    link_id: UUID,
    plans: bool | None = None,
    build: bool | None = None,
    analysis: bool | None = None,
) -> CoachAthleteLink:
    """**Only the athlete sets these.** A coach cannot grant themselves access.

    There is deliberately no constraints permission to set. The request schema
    has no field for one, this function has no parameter for one, and the
    table has no column for one — three independent places a mistake would
    have to survive.
    """
    link = session.get(CoachAthleteLink, link_id)
    if link is None or link.athlete_id != athlete.id:
        raise NotFound("Coach link not found.")
    if link.status is not CoachLinkStatus.ACTIVE:
        raise Conflict(f"That link is {link.status.value}.")

    if plans is not None:
        link.perm_plans = plans
    if build is not None:
        link.perm_build = build
    if analysis is not None:
        link.perm_analysis = analysis
    session.flush()
    logger.info(
        "coach.permissions_changed",
        extra={
            "link_id": str(link.id),
            "perm_plans": link.perm_plans,
            "perm_build": link.perm_build,
            "perm_analysis": link.perm_analysis,
        },
    )
    return link


def revoke(session: Session, *, actor: User, link_id: UUID) -> CoachAthleteLink:
    """Either side can end the relationship, immediately."""
    link = session.get(CoachAthleteLink, link_id)
    if link is None or actor.id not in (link.coach_id, link.athlete_id):
        raise NotFound("Coach link not found.")
    if link.status is CoachLinkStatus.REVOKED:
        return link
    link.status = CoachLinkStatus.REVOKED
    link.revoked_at = datetime.now(UTC)
    link.perm_plans = False
    link.perm_build = False
    link.perm_analysis = False
    link.invite_token_hash = None
    link.invite_delivery_link = None
    session.flush()
    logger.info("coach.revoked", extra={"link_id": str(link.id)})
    return link


def active_link(session: Session, *, coach_id: UUID, athlete_id: UUID) -> CoachAthleteLink | None:
    """Read fresh on every request. Never cached at issuance."""
    return session.scalar(
        select(CoachAthleteLink).where(
            CoachAthleteLink.coach_id == coach_id,
            CoachAthleteLink.athlete_id == athlete_id,
            CoachAthleteLink.status == CoachLinkStatus.ACTIVE,
        )
    )


def require_permission(
    session: Session, *, coach: User, athlete_id: UUID, permission: str
) -> CoachAthleteLink:
    """The one gate every coach-scoped read and write goes through.

    ``permission`` is validated against :data:`COACH_PERMISSIONS` before it is
    used, so a caller passing ``"constraints"`` gets a programming error here
    rather than an attribute lookup that happens to return something.
    """
    if permission not in COACH_PERMISSIONS:
        raise ValueError(
            f"{permission!r} is not a coach permission. There are exactly "
            f"three: {', '.join(COACH_PERMISSIONS)}. Constraints are not one "
            f"of them and never will be."
        )
    link = active_link(session, coach_id=coach.id, athlete_id=athlete_id)
    if link is None:
        raise NotFound("Athlete not found.")
    if not getattr(link, f"perm_{permission}"):
        raise Forbidden(
            f"This athlete has not granted you {permission} access.",
            details={"permission": permission},
        )
    return link


def list_athletes(session: Session, *, coach: User) -> list[CoachAthleteLink]:
    return list(
        session.scalars(
            select(CoachAthleteLink)
            .where(
                CoachAthleteLink.coach_id == coach.id,
                CoachAthleteLink.status != CoachLinkStatus.REVOKED,
            )
            .order_by(CoachAthleteLink.invited_at.desc())
        )
    )


def list_coaches(session: Session, *, athlete: User) -> list[CoachAthleteLink]:
    return list(
        session.scalars(
            select(CoachAthleteLink)
            .where(
                CoachAthleteLink.athlete_id == athlete.id,
                CoachAthleteLink.status != CoachLinkStatus.REVOKED,
            )
            .order_by(CoachAthleteLink.invited_at.desc())
        )
    )


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


#: Characters that turn a note into markup. Escaped on write rather than on
#: render, because there are several renderers — the app, the PDF, an email —
#: and only one writer.
def sanitize_note(body: str) -> str:
    """Escape on write. Stored XSS is the failure mode this prevents.

    Escaping at render time would mean every current and future renderer has
    to remember; escaping here means the stored value is inert everywhere.
    """
    cleaned = body.strip()
    if not cleaned:
        raise InvalidInput("A note cannot be empty.", field="body")
    if len(cleaned) > 4000:
        raise InvalidInput("A note is limited to 4000 characters.", field="body")
    return escape(cleaned)


def write_note(session: Session, *, coach: User, athlete_id: UUID, body: str) -> CoachNote:
    require_permission(session, coach=coach, athlete_id=athlete_id, permission="plans")
    note = CoachNote(coach_id=coach.id, athlete_id=athlete_id, body=sanitize_note(body))
    session.add(note)
    session.flush()
    return note


def list_notes(session: Session, *, athlete_id: UUID) -> list[CoachNote]:
    return list(
        session.scalars(
            select(CoachNote)
            .where(CoachNote.athlete_id == athlete_id)
            .order_by(CoachNote.created_at.desc())
        )
    )


# ---------------------------------------------------------------------------
# The race-week board
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoardRow:
    """One athlete's next race, as the coach board shows it."""

    athlete_id: UUID
    athlete_name: str | None
    link_id: UUID
    can_view_plans: bool
    can_build: bool
    can_view_analysis: bool
    race_id: UUID | None = None
    course_name: str | None = None
    event_date: object = None
    days_away: int | None = None
    plan_id: UUID | None = None
    plan_version: int | None = None
    feasibility: str | None = None
    projected_minutes: float | None = None
    worst_margin_minutes: float | None = None
    has_pending_drift: bool = False
    readiness_fraction: float | None = None
    #: Present when the athlete has not granted plan access. The coach sees
    #: that the athlete exists and nothing else, which is the honest state.
    withheld_reason: str | None = None


def board(session: Session, *, coach: User, today: object | None = None) -> list[BoardRow]:
    """Every linked athlete, soonest race first.

    An athlete who has not granted plan access still appears — with their
    numbers withheld and a reason. Hiding them entirely would make a coach
    think the invite failed.
    """
    from datetime import date as date_type

    from raceos.db.models import Course, PlanDriftEvent
    from raceos.domain.enums import DriftStatus

    day: date_type = today if isinstance(today, date_type) else datetime.now(UTC).date()
    rows: list[BoardRow] = []

    for link in list_athletes(session, coach=coach):
        athlete = session.get(User, link.athlete_id)
        if athlete is None:  # pragma: no cover - FK CASCADE
            continue
        if link.status is not CoachLinkStatus.ACTIVE or not link.perm_plans:
            rows.append(
                BoardRow(
                    athlete_id=link.athlete_id,
                    athlete_name=athlete.name,
                    link_id=link.id,
                    can_view_plans=False,
                    can_build=link.perm_build,
                    can_view_analysis=link.perm_analysis,
                    withheld_reason=(
                        "This athlete has not granted plan access."
                        if link.status is CoachLinkStatus.ACTIVE
                        else "This invite has not been accepted yet."
                    ),
                )
            )
            continue

        race = session.scalar(
            select(Race)
            .where(
                Race.user_id == athlete.id,
                Race.status == RaceStatus.UPCOMING,
                Race.event_date >= day,
            )
            .order_by(Race.event_date)
            .limit(1)
        )
        if race is None:
            rows.append(
                BoardRow(
                    athlete_id=athlete.id,
                    athlete_name=athlete.name,
                    link_id=link.id,
                    can_view_plans=True,
                    can_build=link.perm_build,
                    can_view_analysis=link.perm_analysis,
                )
            )
            continue

        plan = session.scalar(
            select(Plan)
            .where(Plan.race_id == race.id, Plan.status == PlanStatus.ACTIVE)
            .order_by(Plan.version.desc())
            .limit(1)
        )
        course = session.get(Course, race.course_id)
        drift = (
            session.scalar(
                select(PlanDriftEvent).where(
                    PlanDriftEvent.plan_id == plan.id,
                    PlanDriftEvent.status == DriftStatus.PENDING,
                )
            )
            if plan is not None
            else None
        )
        rows.append(
            BoardRow(
                athlete_id=athlete.id,
                athlete_name=athlete.name,
                link_id=link.id,
                can_view_plans=True,
                can_build=link.perm_build,
                can_view_analysis=link.perm_analysis,
                race_id=race.id,
                course_name=course.name if course else None,
                event_date=race.event_date,
                days_away=(race.event_date - day).days,
                plan_id=plan.id if plan else None,
                plan_version=plan.version if plan else None,
                feasibility=plan.feasibility.value if plan else None,
                projected_minutes=float(plan.projected_minutes)
                if plan and plan.projected_minutes is not None
                else None,
                worst_margin_minutes=float(plan.worst_margin_minutes)
                if plan and plan.worst_margin_minutes is not None
                else None,
                has_pending_drift=drift is not None,
                readiness_fraction=float(plan.readiness_fraction)
                if plan and plan.readiness_fraction is not None
                else None,
            )
        )

    # Race week first, then by date. An athlete with no race sorts last.
    rows.sort(key=lambda row: (row.days_away is None, row.days_away or 0))
    return rows


def compare(
    session: Session, *, coach: User, athlete_ids: list[UUID], today: object | None = None
) -> list[BoardRow]:
    """The same rows, restricted to a chosen set, for side-by-side reading.

    Built from :func:`board` rather than a second query so the compare view
    and the board can never disagree about an athlete's margin.
    """
    wanted = set(athlete_ids)
    if not wanted:
        raise InvalidInput("Choose at least one athlete to compare.", field="athlete_ids")
    rows = [row for row in board(session, coach=coach, today=today) if row.athlete_id in wanted]
    missing = wanted - {row.athlete_id for row in rows}
    if missing:
        raise NotFound("One of those athletes is not linked to you.")
    return rows
