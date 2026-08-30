"""The notification inbox, its preference matrix, and the delivery rules.

**In-app is the floor for critical types.** A drift alert and a cut-off
warning cannot be switched off — the athlete chooses the *channel*, not
whether the warning exists. Enforced here, server-side, rather than by a
disabled toggle in the UI, because a UI-only rule is one API call away from
being untrue.

**Nothing is delivered inside the race window.** An athlete standing in a swim
start does not need a push notification telling them their bike target moved:
they cannot act on it, and it is the worst possible moment to introduce doubt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raceos.api.errors import NotFound
from raceos.config import Settings
from raceos.db.models import Course, Notification, NotificationPreference, Race, User
from raceos.domain.enums import (
    CRITICAL_NOTIFICATION_TYPES,
    DriftSensitivity,
    NotificationSeverity,
    NotificationType,
    RaceStatus,
)
from raceos.logging import get_logger

logger = get_logger(__name__)

#: Channel defaults for a user who has never opened the settings screen.
#: Email on, in-app on, push off — push needs an explicit browser grant, so
#: defaulting it on would promise a delivery that silently never happens.
DEFAULT_CHANNELS: dict[NotificationType, tuple[bool, bool, bool]] = {
    NotificationType.DRIFT: (True, False, True),
    NotificationType.CUTOFF: (True, False, True),
    NotificationType.WEEK: (True, False, True),
    NotificationType.BUNDLE: (True, False, True),
    NotificationType.ANALYSIS: (True, False, True),
    NotificationType.DIGEST: (False, False, True),
}


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


def preferences_for(session: Session, *, user: User) -> list[NotificationPreference]:
    """Every type, always. Missing rows are created at their defaults.

    Returning a complete matrix means the settings screen never has to guess
    what an absent row meant, and a type added later cannot silently inherit
    "off".
    """
    existing = {
        row.type_key: row
        for row in session.scalars(
            select(NotificationPreference).where(NotificationPreference.user_id == user.id)
        )
    }
    out: list[NotificationPreference] = []
    for type_key in NotificationType:
        row = existing.get(type_key)
        if row is None:
            email, push, inapp = DEFAULT_CHANNELS[type_key]
            row = NotificationPreference(
                user_id=user.id,
                type_key=type_key,
                channel_email=email,
                channel_push=push,
                channel_inapp=inapp,
                drift_sensitivity=DriftSensitivity.BALANCED,
            )
            session.add(row)
        out.append(row)
    session.flush()
    return out


def update_preference(
    session: Session,
    *,
    user: User,
    type_key: NotificationType,
    channel_email: bool | None = None,
    channel_push: bool | None = None,
    channel_inapp: bool | None = None,
    drift_sensitivity: DriftSensitivity | None = None,
) -> NotificationPreference:
    """Apply a change, then re-assert the floor.

    A request that turns in-app off for a critical type is not rejected: it is
    *clamped*, and the response shows the clamped value. Rejecting would make
    the settings screen argue with the user; clamping tells them the truth
    about what the system will do.
    """
    row = session.scalar(
        select(NotificationPreference).where(
            NotificationPreference.user_id == user.id,
            NotificationPreference.type_key == type_key,
        )
    )
    if row is None:
        preferences_for(session, user=user)
        row = session.scalar(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user.id,
                NotificationPreference.type_key == type_key,
            )
        )
    if row is None:  # pragma: no cover - just created above
        raise NotFound(f"No preference row for {type_key.value}.")

    if channel_email is not None:
        row.channel_email = channel_email
    if channel_push is not None:
        row.channel_push = channel_push
    if channel_inapp is not None:
        row.channel_inapp = channel_inapp
    if drift_sensitivity is not None:
        row.drift_sensitivity = drift_sensitivity

    if type_key in CRITICAL_NOTIFICATION_TYPES and not row.channel_inapp:
        # The floor. The user chooses the channel; they do not choose whether
        # a cut-off warning exists.
        row.channel_inapp = True

    session.flush()
    return row


# ---------------------------------------------------------------------------
# The race window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SuppressionWindow:
    """When a race is close enough that a notification would only be noise."""

    race_id: UUID
    opens_at: datetime
    closes_at: datetime
    reason: str


def _race_start_utc(session: Session, race: Race) -> datetime | None:
    course = session.get(Course, race.course_id)
    if course is None:  # pragma: no cover - FK RESTRICT
        return None
    try:
        zone = ZoneInfo(course.timezone)
    except Exception:  # pragma: no cover - a bad tz is a bundle problem
        return None
    local = datetime.combine(race.event_date, race.start_time_local, tzinfo=zone)
    return local.astimezone(UTC)


def suppression_windows(
    session: Session, *, user: User, settings: Settings, now: datetime | None = None
) -> list[SuppressionWindow]:
    """Windows currently open across this athlete's upcoming races.

    Bounded to races within a week so the query does not walk a whole season
    to answer a question about today.
    """
    moment = now or datetime.now(UTC)
    buffer_hours = settings.race_notification_suppression_buffer_hours
    races = session.scalars(
        select(Race).where(
            Race.user_id == user.id,
            Race.status == RaceStatus.UPCOMING,
            Race.event_date >= (moment - timedelta(days=2)).date(),
            Race.event_date <= (moment + timedelta(days=7)).date(),
        )
    )

    windows: list[SuppressionWindow] = []
    for race in races:
        start = _race_start_utc(session, race)
        if start is None:
            continue
        opens = start - timedelta(hours=buffer_hours)
        # Through the end of race day: a full-distance athlete can still be on
        # course seventeen hours after the gun.
        closes = start + timedelta(hours=24)
        if opens <= moment <= closes:
            windows.append(
                SuppressionWindow(
                    race_id=race.id,
                    opens_at=opens,
                    closes_at=closes,
                    reason=(
                        "This race is under way or about to start. Alerts resume " "afterwards."
                    ),
                )
            )
    return windows


def is_suppressed(
    session: Session,
    *,
    user: User,
    settings: Settings,
    race_id: UUID | None,
    now: datetime | None = None,
) -> bool:
    """Whether a notification about *race_id* should be held back.

    Scoped to the race it is about. A drift alert for August's race is still
    worth sending while the athlete is racing in June — it is only the race
    they are standing in that must go quiet.
    """
    if race_id is None:
        return False
    return any(
        window.race_id == race_id
        for window in suppression_windows(session, user=user, settings=settings, now=now)
    )


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryResult:
    notification: Notification | None
    delivered_inapp: bool
    queued_email: bool
    queued_push: bool
    suppressed: bool
    reason: str = ""


def notify(
    session: Session,
    *,
    user: User,
    settings: Settings,
    type_key: NotificationType,
    severity: NotificationSeverity,
    title: str,
    body: str,
    tag: str | None = None,
    race_id: UUID | None = None,
    plan_id: UUID | None = None,
    deltas: list[dict[str, Any]] | None = None,
    cta_label: str | None = None,
    cta_href: str | None = None,
    now: datetime | None = None,
) -> DeliveryResult:
    """The single entry point for every notification the system produces.

    One function so the preference matrix, the critical floor and the race
    window are applied in exactly one place. A second delivery path would be a
    second place for those rules to be forgotten.
    """
    if is_suppressed(session, user=user, settings=settings, race_id=race_id, now=now):
        logger.info(
            "notification.suppressed",
            extra={"type_key": type_key.value, "race_id": str(race_id)},
        )
        return DeliveryResult(
            notification=None,
            delivered_inapp=False,
            queued_email=False,
            queued_push=False,
            suppressed=True,
            reason="the athlete is inside this race's window",
        )

    preference = _preference(session, user=user, type_key=type_key)
    wants_inapp = preference.channel_inapp or type_key in CRITICAL_NOTIFICATION_TYPES

    notification: Notification | None = None
    if wants_inapp:
        notification = Notification(
            user_id=user.id,
            type_key=type_key,
            tag=tag,
            severity=severity,
            race_id=race_id,
            plan_id=plan_id,
            title=title,
            body=body,
            # The structured numbers are stored beside the prose so the
            # phrasing boundary stays auditable: the body is derived from
            # these, never the other way round.
            deltas=deltas or [],
            cta_label=cta_label,
            cta_href=cta_href,
        )
        session.add(notification)
        session.flush()

    return DeliveryResult(
        notification=notification,
        delivered_inapp=notification is not None,
        queued_email=preference.channel_email and settings.email_enabled,
        queued_push=preference.channel_push and settings.push_enabled,
        suppressed=False,
    )


def _preference(
    session: Session, *, user: User, type_key: NotificationType
) -> NotificationPreference:
    row = session.scalar(
        select(NotificationPreference).where(
            NotificationPreference.user_id == user.id,
            NotificationPreference.type_key == type_key,
        )
    )
    if row is not None:
        return row
    email, push, inapp = DEFAULT_CHANNELS[type_key]
    created = NotificationPreference(
        user_id=user.id,
        type_key=type_key,
        channel_email=email,
        channel_push=push,
        channel_inapp=inapp,
    )
    session.add(created)
    session.flush()
    return created


# ---------------------------------------------------------------------------
# The inbox
# ---------------------------------------------------------------------------


def list_notifications(
    session: Session,
    *,
    user: User,
    unread_only: bool = False,
    type_key: NotificationType | None = None,
    race_id: UUID | None = None,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[Notification], int]:
    """A real paginated, queryable resource — not a toast that vanished."""
    filters = [Notification.user_id == user.id]
    if unread_only:
        filters.append(Notification.read.is_(False))
    if type_key is not None:
        filters.append(Notification.type_key == type_key)
    if race_id is not None:
        filters.append(Notification.race_id == race_id)

    total = session.scalar(select(func.count()).select_from(Notification).where(*filters))
    rows = session.scalars(
        select(Notification)
        .where(*filters)
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows), int(total or 0)


def unread_count(session: Session, *, user: User) -> int:
    total = session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user.id, Notification.read.is_(False))
    )
    return int(total or 0)


def mark_read(session: Session, *, user: User, notification_id: UUID) -> Notification:
    notification = session.get(Notification, notification_id)
    if notification is None or notification.user_id != user.id:
        raise NotFound("Notification not found.")
    notification.read = True
    session.flush()
    return notification


def mark_all_read(session: Session, *, user: User) -> int:
    rows = list(
        session.scalars(
            select(Notification).where(
                Notification.user_id == user.id, Notification.read.is_(False)
            )
        )
    )
    for row in rows:
        row.read = True
    session.flush()
    return len(rows)
