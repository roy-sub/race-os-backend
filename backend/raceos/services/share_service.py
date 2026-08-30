"""Share links: a real token scheme, mandatory expiry, live revocation.

Three properties, each of which is a test:

1. **No scope exposes a constraint value or account data — including
   ``full_plan``.** This is the second structural guarantee. The scope shapes
   which *plan content* comes back; it never decides whether the athlete's
   body is included, because it never is. The response is built by an
   allow-list of fields, so a field added to the plan serialiser later cannot
   leak by default.
2. **Expiry is mandatory.** ``expires_at`` is NOT NULL in the schema and there
   is no code path that computes a null or a distant sentinel. A link that
   never expires is a permanent credential someone forgot about.
3. **Revocation is immediate.** Every resolve re-reads the row and re-checks
   revocation and expiry, so a revoked link stops working on a page that is
   already open.

The access code is an optional *second* factor, rate-limited, never the sole
gate. The frontend mock's six-character box is cosmetic; the token is the
security boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.errors import Conflict, InvalidInput, NotFound
from raceos.config import Settings
from raceos.db.models import Course, Plan, Race, ShareLink, ShareLinkOpen, User
from raceos.domain.enums import ShareScope
from raceos.logging import get_logger
from raceos.services import security

logger = get_logger(__name__)

#: The longest a share link may live. A coach reviewing race week does not
#: need a year, and every extra day is exposure with no owner watching it.
MAX_TTL_DAYS = 180
DEFAULT_TTL_DAYS = 30

#: **The complete set of fields any share response may contain.**
#:
#: An allow-list rather than a deny-list: a field added to the plan serialiser
#: later must be *chosen* into a share response, and cannot leak by having
#: been forgotten. Note what is absent — every constraint value, the athlete's
#: weight, their email, their tier, their other races.
SHAREABLE_PLAN_FIELDS: frozenset[str] = frozenset(
    {
        "plan_version",
        "course_name",
        "course_place",
        "event_date",
        "start_time_local",
        "projected_label",
        "feasibility",
        "bundle_version",
        "attribution",
        "splits",
        "segments",
        "gates",
        "fuelling",
        "aid_actions",
        "bags",
        "shared_by",
        "scope",
        "expires_at",
    }
)

#: Which content blocks each scope includes. Every scope draws only from
#: :data:`SHAREABLE_PLAN_FIELDS`.
SCOPE_BLOCKS: dict[ShareScope, frozenset[str]] = {
    ShareScope.FULL_PLAN: frozenset(
        {"splits", "segments", "gates", "fuelling", "aid_actions", "bags"}
    ),
    ShareScope.PACING_ONLY: frozenset({"splits", "segments"}),
    ShareScope.BAGS_ONLY: frozenset({"bags"}),
    ShareScope.RACE_CARD: frozenset({"splits", "gates", "fuelling", "aid_actions"}),
}

#: **The allow-list runs field by field inside each block too.**
#:
#: A top-level allow-list is not enough, and a test proved it: bag items carry
#: ``reason_constraint_key`` and ``reason_text`` — the "Why this?" drawer —
#: and a reason like "Swim leg planned at 1:56/100m" *is* a constraint value
#: written out in prose. Fuelling carries ``binding_*_key``, which names the
#: constraint that bound each number. Both are the athlete's private
#: reasoning, and neither survives into a share response at any scope.
#:
#: What a recipient packing a bag needs is the item and the quantity. What
#: they do not need is which of the athlete's measured numbers put it there.
BLOCK_FIELDS: dict[str, frozenset[str]] = {
    "splits": frozenset(
        {
            "leg",
            "distance",
            "target_pace_or_power",
            "unit",
            "split_minutes",
            "split_label",
            "note",
        }
    ),
    "segments": frozenset(
        {
            "ordinal",
            "leg",
            "name",
            "from_km",
            "to_km",
            "terrain_desc",
            "target_watts",
            "target_pace_sec_per_km",
            "target_minutes",
            "note",
        }
    ),
    "gates": frozenset(
        {
            "name",
            "leg",
            "limit_minutes",
            "eta_minutes",
            "margin_minutes",
            "margin_label",
            "load_pct",
            "state",
        }
    ),
    "fuelling": frozenset(
        {
            "carb_g_per_hr",
            "fluid_ml_per_hr",
            "sodium_mg_per_hr",
            "caffeine_mg_total",
            "total_carb_g",
            "requires_multiple_transportable",
            "overridden",
        }
    ),
    "aid_actions": frozenset(
        {
            "ordinal",
            "at_clock_minutes",
            "at_km",
            "leg",
            "station_name",
            "action_text",
            "cumulative_carb_g",
        }
    ),
    "bags": frozenset({"key", "name", "when_label", "item_count", "items"}),
    "bag_items": frozenset({"ordinal", "name", "qty", "note", "is_user_added"}),
}


def _project(item: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key in allowed}


def _project_block(name: str, value: Any) -> Any:
    """Select from a block, never redact it.

    Selection means a field added to the plan serialiser later is absent by
    default; redaction would mean it is present until somebody remembers.
    """
    allowed = BLOCK_FIELDS[name]
    if name == "fuelling":
        return _project(value, allowed) if isinstance(value, dict) else None
    if name == "bags":
        bags = []
        for bag in value or []:
            if not isinstance(bag, dict):
                continue
            projected = _project(bag, allowed)
            projected["items"] = [
                _project(item, BLOCK_FIELDS["bag_items"])
                for item in bag.get("items", [])
                if isinstance(item, dict)
            ]
            bags.append(projected)
        return bags
    return [_project(item, allowed) for item in value or [] if isinstance(item, dict)]


@dataclass(frozen=True)
class IssuedShare:
    link: ShareLink
    #: Returned once, at creation. Only the hash is stored.
    token: str
    url: str


def create(
    session: Session,
    *,
    plan: Plan,
    user: User,
    settings: Settings,
    scope: ShareScope = ShareScope.FULL_PLAN,
    expires_in_days: int = DEFAULT_TTL_DAYS,
    recipient_label: str | None = None,
    access_code: str | None = None,
) -> IssuedShare:
    """Mint a link. **Expiry is not optional.**"""
    if plan.user_id != user.id:
        raise NotFound("Plan not found.")
    if plan.solved_at is None:
        raise Conflict("There is nothing to share until this plan is solved.")
    if not 1 <= expires_in_days <= MAX_TTL_DAYS:
        raise InvalidInput(
            f"A share link must expire between 1 and {MAX_TTL_DAYS} days from "
            f"now. A link that never expires is a permanent credential.",
            field="expires_in_days",
        )

    issued = security.issue_token(settings.share_token_bytes)
    link = ShareLink(
        plan_id=plan.id,
        token_hash=issued.hashed,
        token_prefix=issued.prefix,
        scope=scope,
        created_by=user.id,
        recipient_label=recipient_label,
        expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
        access_code_hash=(
            security.hash_token(access_code.strip())
            if access_code and access_code.strip()
            else None
        ),
    )
    session.add(link)
    plan.shared = True
    session.flush()

    logger.info(
        "share.created",
        extra={
            "share_link_id": str(link.id),
            "plan_id": str(plan.id),
            "scope": scope.value,
            "expires_at": link.expires_at.isoformat(),
        },
    )
    return IssuedShare(
        link=link,
        token=issued.raw,
        url=f"{settings.app_base_url}/shared/{issued.raw}",
    )


def revoke(session: Session, *, link_id: UUID, user: User) -> ShareLink:
    """Immediate. The next resolve re-reads the row and refuses."""
    link = session.get(ShareLink, link_id)
    if link is None:
        raise NotFound("Share link not found.")
    plan = session.get(Plan, link.plan_id)
    if plan is None or plan.user_id != user.id:
        raise NotFound("Share link not found.")
    if link.revoked_at is None:
        link.revoked_at = datetime.now(UTC)
        session.flush()
    _refresh_shared_flag(session, plan)
    logger.info("share.revoked", extra={"share_link_id": str(link.id)})
    return link


def _refresh_shared_flag(session: Session, plan: Plan) -> None:
    live = session.scalar(
        select(ShareLink).where(
            ShareLink.plan_id == plan.id,
            ShareLink.revoked_at.is_(None),
            ShareLink.expires_at > datetime.now(UTC),
        )
    )
    plan.shared = live is not None
    session.flush()


def list_links(session: Session, *, plan: Plan, user: User) -> list[ShareLink]:
    if plan.user_id != user.id:
        raise NotFound("Plan not found.")
    return list(
        session.scalars(
            select(ShareLink)
            .where(ShareLink.plan_id == plan.id)
            .order_by(ShareLink.created_at.desc())
        )
    )


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


def resolve(
    session: Session,
    *,
    token: str,
    settings: Settings,
    access_code: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[ShareLink, Plan]:
    """Re-check everything, every time.

    Revocation and expiry are read from the row on each resolve rather than
    trusted from the token, which is what makes revoking work on a page that
    is already open.
    """
    link = session.scalar(
        select(ShareLink).where(ShareLink.token_hash == security.hash_token(token))
    )
    now = datetime.now(UTC)
    # One message for every failure mode: an unknown token, a revoked link and
    # an expired one are indistinguishable to the holder, so a probe learns
    # nothing about which links exist.
    if link is None or link.revoked_at is not None or link.expires_at <= now:
        raise NotFound("This link is no longer available.")

    code_required = link.access_code_hash is not None
    code_matches = bool(
        access_code
        and link.access_code_hash
        and security.tokens_match(access_code.strip(), link.access_code_hash)
    )
    if code_required and not code_matches:
        raise InvalidInput("That access code is not right.", field="access_code")

    plan = session.get(Plan, link.plan_id)
    if plan is None:  # pragma: no cover - CASCADE
        raise NotFound("This link is no longer available.")

    link.opens_count += 1
    link.last_opened_at = now
    session.add(
        ShareLinkOpen(
            share_link_id=link.id,
            opened_at=now,
            # An IP address is PII, so only a keyed hash is kept.
            ip_hash=security.hash_ip(ip, settings),
            user_agent=(user_agent or "")[:500] or None,
        )
    )
    session.flush()
    return link, plan


def render(
    session: Session, *, link: ShareLink, plan: Plan, detail: dict[str, Any]
) -> dict[str, Any]:
    """Build the response from the allow-list. **Nothing else gets through.**

    ``detail`` is the full serialised plan; this function *selects from* it
    rather than redacting it, so a new field is absent by default instead of
    present until someone remembers to remove it.
    """
    race = session.get(Race, plan.race_id)
    course = session.get(Course, race.course_id) if race else None
    owner = session.get(User, plan.user_id)
    from raceos.db.models import CourseBundle

    bundle = session.get(CourseBundle, race.course_bundle_id) if race else None

    payload: dict[str, Any] = {
        "plan_version": plan.version,
        "course_name": course.name if course else None,
        "course_place": course.place if course else None,
        "event_date": race.event_date.isoformat() if race else None,
        "start_time_local": race.start_time_local.strftime("%H:%M") if race else None,
        "projected_label": detail.get("projected_label"),
        "feasibility": plan.feasibility.value,
        "bundle_version": bundle.version if bundle else None,
        # ODbL travels with the derived data, into every surface that shows it.
        "attribution": bundle.attribution if bundle else None,
        # A first name only. A share link is often forwarded, and the full
        # account identity is not part of what was shared.
        "shared_by": (owner.name or "").split(" ")[0] if owner else None,
        "scope": link.scope.value,
        "expires_at": link.expires_at.isoformat(),
    }

    for block in SCOPE_BLOCKS[link.scope]:
        payload[block] = _project_block(block, detail.get(block))

    leaked = set(payload) - SHAREABLE_PLAN_FIELDS
    if leaked:  # pragma: no cover - the allow-list test proves this is empty
        raise RuntimeError(
            f"share response carried fields outside the allow-list: {sorted(leaked)}"
        )
    return payload


def purge_expired(session: Session) -> dict[str, int]:
    """Retire links that have lapsed, and correct the plans' shared flags.

    Rows are kept — the open history is a record of who saw what — but the
    plan stops advertising itself as shared once nothing live points at it.
    """
    now = datetime.now(UTC)
    lapsed = list(
        session.scalars(
            select(ShareLink).where(ShareLink.expires_at <= now, ShareLink.revoked_at.is_(None))
        )
    )
    touched: set[UUID] = set()
    for link in lapsed:
        plan = session.get(Plan, link.plan_id)
        if plan is not None and plan.id not in touched:
            _refresh_shared_flag(session, plan)
            touched.add(plan.id)
    return {"expired_links": len(lapsed), "plans_updated": len(touched)}
