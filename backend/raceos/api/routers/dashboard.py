"""Dashboard, My Plans and the notification inbox.

The dashboard is assembled in one request because its cards have to agree
with each other. A "next race" derived from a different read than the season
list is how a screen ends up showing two different next races.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, status
from pydantic import BaseModel, ConfigDict, Field

from raceos.api.deps import Config, CurrentUser, DbSession
from raceos.api.schemas.dashboard import (
    DashboardOut,
    MyPlansOut,
    NotificationOut,
    NotificationPage,
    PreferenceOut,
    PreferencePatch,
    RaceCardOut,
)
from raceos.api.schemas.plan import format_hm
from raceos.domain.enums import (
    CRITICAL_NOTIFICATION_TYPES,
    NotificationType,
    PlanStatus,
)
from raceos.services import dashboard_service, notification_service, push_service

router = APIRouter(prefix="/api/v1", tags=["dashboard"])


def _card(card: dashboard_service.RaceCard) -> RaceCardOut:
    out = RaceCardOut.model_validate(asdict(card))
    out.goal_label = format_hm(card.goal_minutes)
    out.projected_label = format_hm(card.projected_minutes)
    if card.worst_margin_minutes is not None:
        sign = "+" if card.worst_margin_minutes >= 0 else "-"
        out.margin_label = f"{sign}{format_hm(abs(card.worst_margin_minutes))}"
    return out


@router.get("/dashboard", summary="Everything the dashboard renders")
def get_dashboard(session: DbSession, user: CurrentUser, settings: Config) -> DashboardOut:
    payload = dashboard_service.dashboard(session, user=user, settings=settings)
    races = [_card(card) for card in payload["races"]]
    next_race = _card(payload["next_race"]) if payload["next_race"] else None
    return DashboardOut.model_validate({**payload, "races": races, "next_race": next_race})


@router.get("/my-plans", summary="Plans grouped as the screen groups them")
def get_my_plans(session: DbSession, user: CurrentUser) -> MyPlansOut:
    upcoming = [_card(card) for card in dashboard_service.season(session, user=user)]
    past = [_card(card) for card in dashboard_service.past_races(session, user=user)]

    unsolved = {PlanStatus.DRAFT.value, "none"}
    return MyPlansOut(
        active=[card for card in upcoming if card.plan_status not in unsolved],
        draft=[card for card in upcoming if card.plan_status in unsolved],
        past=past,
    )


# ---------------------------------------------------------------------------
# The inbox
# ---------------------------------------------------------------------------


@router.get("/notifications", summary="The inbox, paginated and queryable")
def list_notifications(
    session: DbSession,
    user: CurrentUser,
    unread: Annotated[bool, Query(description="Only unread items")] = False,
    type_key: Annotated[
        NotificationType | None, Query(alias="type", description="Filter by type")
    ] = None,
    race_id: Annotated[UUID | None, Query(description="Filter to one race")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> NotificationPage:
    rows, total = notification_service.list_notifications(
        session,
        user=user,
        unread_only=unread,
        type_key=type_key,
        race_id=race_id,
        limit=limit,
        offset=offset,
    )
    return NotificationPage(
        data=[NotificationOut.model_validate(row) for row in rows],
        total=total,
        unread=notification_service.unread_count(session, user=user),
        limit=limit,
        offset=offset,
    )


@router.post("/notifications/{notification_id}/read", summary="Mark one as read")
def mark_read(notification_id: UUID, session: DbSession, user: CurrentUser) -> NotificationOut:
    notification = notification_service.mark_read(
        session, user=user, notification_id=notification_id
    )
    session.commit()
    return NotificationOut.model_validate(notification)


@router.post("/notifications/read-all", summary="Mark the whole inbox as read")
def mark_all_read(session: DbSession, user: CurrentUser) -> dict[str, int]:
    marked = notification_service.mark_all_read(session, user=user)
    session.commit()
    return {"marked": marked}


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


def _preference(row: object) -> PreferenceOut:
    out = PreferenceOut.model_validate(row)
    out.inapp_locked = out.type_key in CRITICAL_NOTIFICATION_TYPES
    return out


@router.get("/notification-preferences", summary="The full channel matrix")
def get_preferences(session: DbSession, user: CurrentUser) -> list[PreferenceOut]:
    """Every type, always — an absent row would be ambiguous."""
    rows = notification_service.preferences_for(session, user=user)
    session.commit()
    return [_preference(row) for row in rows]


@router.patch(
    "/notification-preferences/{type_key}",
    status_code=status.HTTP_200_OK,
    summary="Change one row's channels",
)
def patch_preference(
    type_key: NotificationType,
    payload: PreferencePatch,
    session: DbSession,
    user: CurrentUser,
) -> PreferenceOut:
    """Turning in-app off for a critical type is **clamped, not rejected**.

    The response shows the clamped value, so the settings screen tells the
    truth about what the system will do instead of arguing with the request.
    """
    row = notification_service.update_preference(
        session,
        user=user,
        type_key=type_key,
        channel_email=payload.channel_email,
        channel_push=payload.channel_push,
        channel_inapp=payload.channel_inapp,
        drift_sensitivity=payload.drift_sensitivity,
    )
    session.commit()
    return _preference(row)


# ---------------------------------------------------------------------------
# Push subscriptions
# ---------------------------------------------------------------------------


class PushSubscribeRequest(BaseModel):
    """The shape a browser's `PushSubscription.toJSON()` produces."""

    endpoint: str = Field(min_length=8, max_length=2000)
    p256dh_key: str = Field(min_length=8, max_length=500)
    auth_key: str = Field(min_length=4, max_length=500)


class PushSubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    #: Only the host. The full endpoint is a capability URL: anyone holding it
    #: can push to that browser, so it is never echoed back.
    endpoint_host: str = ""
    user_agent: str | None = None
    failed_count: int
    last_used_at: datetime | None = None


@router.get("/push/subscriptions", summary="This account's push endpoints")
def list_push(session: DbSession, user: CurrentUser) -> list[PushSubscriptionOut]:
    out: list[PushSubscriptionOut] = []
    for row in push_service.list_subscriptions(session, user=user):
        item = PushSubscriptionOut.model_validate(row)
        item.endpoint_host = row.endpoint.split("/")[2] if "//" in row.endpoint else ""
        out.append(item)
    return out


@router.post(
    "/push/subscriptions",
    status_code=status.HTTP_201_CREATED,
    summary="Register a browser for push",
)
def subscribe_push(
    payload: PushSubscribeRequest,
    session: DbSession,
    user: CurrentUser,
    settings: Config,
    user_agent: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    """Accepted even while push is disabled.

    A browser that has granted permission should not have to ask again when
    the flag flips, and storing the endpoint costs nothing. The response says
    plainly whether anything will actually be delivered.
    """
    subscription = push_service.subscribe(
        session,
        user=user,
        endpoint=payload.endpoint,
        p256dh_key=payload.p256dh_key,
        auth_key=payload.auth_key,
        user_agent=user_agent,
    )
    session.commit()
    return {
        "id": str(subscription.id),
        "delivery_enabled": settings.push_enabled,
        "note": (
            "Registered. Push delivery is off in this release; your "
            "notifications are in the in-app inbox."
            if not settings.push_enabled
            else "Registered."
        ),
    }


@router.delete(
    "/push/subscriptions/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Remove a browser",
)
def unsubscribe_push(subscription_id: UUID, session: DbSession, user: CurrentUser) -> None:
    push_service.unsubscribe(session, user=user, subscription_id=subscription_id)
    session.commit()
