"""Web push subscriptions and delivery.

**Built and disabled.** `PUSH_ENABLED` is false in V1: push needs a VAPID
keypair and an explicit browser grant, and shipping it half-wired would
promise a delivery that silently never happens. Everything except the network
call is real — subscriptions are stored, the preference matrix is honoured,
failures are counted, and dead endpoints are pruned — so enabling it is a
configuration change and a keypair, not a code change.

The in-app inbox is the live channel in V1, and critical notifications land
there regardless of what push is doing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.errors import InvalidInput, NotFound
from raceos.config import Settings
from raceos.db.models import Notification, PushSubscription, User
from raceos.logging import get_logger

logger = get_logger(__name__)

#: After this many consecutive failures an endpoint is treated as dead and
#: removed. Browsers revoke subscriptions silently, and retrying a gone
#: endpoint forever is how a push queue turns into a backlog.
MAX_CONSECUTIVE_FAILURES = 5


@dataclass(frozen=True)
class PushResult:
    attempted: int
    delivered: int
    pruned: int
    reason: str = ""


def subscribe(
    session: Session,
    *,
    user: User,
    endpoint: str,
    p256dh_key: str,
    auth_key: str,
    user_agent: str | None = None,
) -> PushSubscription:
    """Store a browser's push endpoint. Idempotent on the endpoint.

    Re-subscribing from the same browser must not create a second row, or the
    athlete gets every notification twice.
    """
    if not endpoint.startswith("https://"):
        raise InvalidInput("A push endpoint must be an HTTPS URL.", field="endpoint")
    if not p256dh_key or not auth_key:
        raise InvalidInput("A push subscription needs both encryption keys.", field="p256dh_key")

    existing = session.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    if existing is not None:
        # Re-point it at whoever is signed in now: a shared browser is a real
        # situation, and leaving it attached to the previous account would
        # send them somebody else's race.
        existing.user_id = user.id
        existing.p256dh_key = p256dh_key
        existing.auth_key = auth_key
        existing.user_agent = user_agent
        existing.failed_count = 0
        session.flush()
        return existing

    subscription = PushSubscription(
        user_id=user.id,
        endpoint=endpoint,
        p256dh_key=p256dh_key,
        auth_key=auth_key,
        user_agent=user_agent,
        failed_count=0,
    )
    session.add(subscription)
    session.flush()
    return subscription


def unsubscribe(session: Session, *, user: User, subscription_id: UUID) -> None:
    subscription = session.get(PushSubscription, subscription_id)
    if subscription is None or subscription.user_id != user.id:
        raise NotFound("Push subscription not found.")
    session.delete(subscription)
    session.flush()


def list_subscriptions(session: Session, *, user: User) -> list[PushSubscription]:
    return list(
        session.scalars(select(PushSubscription).where(PushSubscription.user_id == user.id))
    )


def deliver(
    session: Session,
    *,
    user: User,
    notification: Notification,
    settings: Settings,
) -> PushResult:
    """Send one notification to every live endpoint for *user*.

    With push disabled this reports honestly rather than pretending: the
    caller sees ``delivered=0`` and a reason, and the in-app inbox has the
    notification either way.
    """
    subscriptions = list_subscriptions(session, user=user)
    if not settings.push_enabled:
        return PushResult(
            attempted=0,
            delivered=0,
            pruned=0,
            reason="push is disabled; the notification is in the in-app inbox",
        )
    if not settings.vapid_private_key.get_secret_value():
        # Config validation already refuses this combination at boot, so
        # reaching here means someone changed it at runtime.
        return PushResult(attempted=0, delivered=0, pruned=0, reason="no VAPID key configured")

    delivered = 0
    pruned = 0
    for subscription in subscriptions:
        if _send(subscription, notification, settings):
            subscription.failed_count = 0
            subscription.last_used_at = datetime.now(UTC)
            delivered += 1
            continue

        subscription.failed_count += 1
        if subscription.failed_count >= MAX_CONSECUTIVE_FAILURES:
            # Browsers revoke silently. Retrying a gone endpoint forever turns
            # the push path into a backlog.
            session.delete(subscription)
            pruned += 1
    session.flush()

    logger.info(
        "push.delivered",
        extra={
            "notification_id": str(notification.id),
            "attempted": len(subscriptions),
            "delivered": delivered,
            "pruned": pruned,
        },
    )
    return PushResult(attempted=len(subscriptions), delivered=delivered, pruned=pruned)


def _send(subscription: PushSubscription, notification: Notification, settings: Settings) -> bool:
    """The one network call, isolated so everything around it is testable.

    V1 never reaches this: :func:`deliver` returns before calling it while
    ``PUSH_ENABLED`` is false. It is written out rather than left as a stub so
    that turning push on is a keypair and a flag, and the payload shape is
    already settled and reviewed.
    """
    import json

    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {
                    "p256dh": subscription.p256dh_key,
                    "auth": subscription.auth_key,
                },
            },
            data=json.dumps(
                {
                    "title": notification.title,
                    "body": notification.body,
                    "tag": notification.tag,
                    "url": notification.cta_href,
                    # The severity travels so the service worker can pick an
                    # icon without a second request.
                    "severity": notification.severity.value,
                }
            ),
            vapid_private_key=settings.vapid_private_key.get_secret_value(),
            vapid_claims={"sub": settings.vapid_subject},
            timeout=10,
        )
    except WebPushException as error:
        logger.warning(
            "push.failed",
            extra={
                "subscription_id": str(subscription.id),
                # The endpoint is a capability URL, so only its host is logged.
                "endpoint_host": subscription.endpoint.split("/")[2],
                "status": getattr(error.response, "status_code", None),
            },
        )
        return False
    except Exception as error:  # transport, DNS, timeout
        logger.warning("push.unavailable", extra={"error_type": type(error).__name__})
        return False
    return True
