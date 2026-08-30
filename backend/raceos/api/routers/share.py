"""Share links: create, list, revoke, resolve.

The resolve endpoint is public — a share link is meant to be opened by
someone without an account. Everything else is owner-only.

**No scope returns a constraint value or account data.** The response is
assembled from an allow-list, so a field added to the plan serialiser later is
absent by default rather than present until someone remembers to remove it.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from sqlalchemy.orm import Session

from raceos.api.deps import Config, CurrentUser, DbSession, get_db
from raceos.api.errors import NotFound
from raceos.api.schemas.coach import (
    ShareCreateRequest,
    ShareCreateResponse,
    SharedPlanOut,
    ShareLinkOut,
)
from raceos.api.serialise import plan_detail
from raceos.db.models import Plan, ShareLink
from raceos.services import plan_service, rate_limit, share_service

router = APIRouter(prefix="/api/v1", tags=["share"])

PublicSession = Annotated[Session, Depends(get_db)]


def _out(link: ShareLink) -> ShareLinkOut:
    out = ShareLinkOut.model_validate(link)
    out.has_access_code = link.access_code_hash is not None
    return out


@router.post(
    "/plans/{plan_id}/share",
    status_code=status.HTTP_201_CREATED,
    summary="Mint a share link",
)
def create_share(
    plan_id: UUID,
    payload: ShareCreateRequest,
    session: DbSession,
    user: CurrentUser,
    settings: Config,
) -> ShareCreateResponse:
    """Expiry is mandatory: there is no "never" option to send."""
    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)
    issued = share_service.create(
        session,
        plan=plan,
        user=user,
        settings=settings,
        scope=payload.scope,
        expires_in_days=payload.expires_in_days,
        recipient_label=payload.recipient_label,
        access_code=payload.access_code,
    )
    session.commit()
    return ShareCreateResponse(link=_out(issued.link), token=issued.token, url=issued.url)


@router.get("/plans/{plan_id}/share", summary="Links minted for this plan")
def list_shares(plan_id: UUID, session: DbSession, user: CurrentUser) -> list[ShareLinkOut]:
    plan = plan_service.get_plan(session, plan_id=plan_id, user=user)
    return [_out(link) for link in share_service.list_links(session, plan=plan, user=user)]


@router.post("/share-links/{link_id}/revoke", summary="Revoke a link now")
def revoke_share(link_id: UUID, session: DbSession, user: CurrentUser) -> ShareLinkOut:
    """Takes effect immediately, including on a page already open."""
    link = share_service.revoke(session, link_id=link_id, user=user)
    session.commit()
    return _out(link)


@router.get("/shared/{token}", summary="Open a shared plan")
def resolve_share(
    token: str,
    request: Request,
    session: PublicSession,
    settings: Config,
    access_code: Annotated[str | None, Query(description="Second factor, if set")] = None,
    user_agent: Annotated[str | None, Header()] = None,
) -> SharedPlanOut:
    """Public by design — a share link is opened by someone with no account.

    Rate-limited on the token prefix so the optional access code cannot be
    brute-forced, and every open is recorded with a hashed IP.
    """
    rate_limit.enforce_rate_limit(
        session,
        subject=token[:12],
        bucket="share_resolve",
        limit=settings.rate_limit_share_code_per_minute,
        settings=settings,
    )
    link, plan = share_service.resolve(
        session,
        token=token,
        settings=settings,
        access_code=access_code,
        ip=request.client.host if request.client else None,
        user_agent=user_agent,
    )
    detail = plan_detail(session, plan).model_dump(mode="json")
    payload = share_service.render(session, link=link, plan=plan, detail=detail)
    session.commit()
    return SharedPlanOut.model_validate(payload)


@router.get("/share-links/{link_id}", summary="One link's status and opens")
def get_share(link_id: UUID, session: DbSession, user: CurrentUser) -> ShareLinkOut:
    link = session.get(ShareLink, link_id)
    if link is None:
        raise NotFound("Share link not found.")
    plan = session.get(Plan, link.plan_id)
    if plan is None or plan.user_id != user.id:
        raise NotFound("Share link not found.")
    return _out(link)
