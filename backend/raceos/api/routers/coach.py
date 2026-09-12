"""The coach domain: invites, permissions, notes, board, compare.

**No endpoint here can read or write an athlete's constraints.** There is no
route that does it, no permission that would allow it, and no field in any
request schema that could ask for it. That is structural guarantee 1 seen
from the API surface.

Permissions are checked live on every request. A revocation takes effect on
the coach's next action, including on a page they already have open.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Response, UploadFile, status

from raceos.api.deps import Config, CurrentUser, DbSession
from raceos.api.errors import NotFound
from raceos.api.schemas.coach import (
    AcceptRequest,
    BoardRowOut,
    BrandingOut,
    BrandingUpdate,
    CoachLinkOut,
    CompareRequest,
    InviteRequest,
    InviteResponse,
    NoteOut,
    NoteRequest,
    PermissionPatch,
)
from raceos.api.schemas.plan import PlanDetail, format_hm
from raceos.api.serialise import plan_detail
from raceos.config import Settings
from raceos.db.models import CoachAthleteLink, CoachBranding, Plan, User
from raceos.domain.entitlements import EntitlementAction
from raceos.exports import tokens
from raceos.services import billing_service, branding_service, coach_service, plan_service
from raceos.storage.base import ObjectNotFoundError, get_storage_backend

router = APIRouter(prefix="/api/v1/coach", tags=["coach"])


def _link_out(session: DbSession, link: CoachAthleteLink) -> CoachLinkOut:
    out = CoachLinkOut.model_validate(link)
    coach = session.get(User, link.coach_id)
    athlete = session.get(User, link.athlete_id)
    out.coach_name = coach.name if coach else None
    out.athlete_name = athlete.name if athlete else None
    return out


def _row(row: coach_service.BoardRow) -> BoardRowOut:
    out = BoardRowOut.model_validate(row.__dict__)
    out.projected_label = format_hm(row.projected_minutes)
    if row.worst_margin_minutes is not None:
        sign = "+" if row.worst_margin_minutes >= 0 else "-"
        out.margin_label = f"{sign}{format_hm(abs(row.worst_margin_minutes))}"
    return out


# ---------------------------------------------------------------------------
# Branding — a logo and an accent, and nothing more
# ---------------------------------------------------------------------------


def _branding_out(row: CoachBranding | None) -> BrandingOut:
    if row is None:
        return BrandingOut(effective_accent_hex=tokens.ACCENT)
    return BrandingOut(
        display_name=row.display_name,
        accent_hex=row.accent_hex,
        footer_note=row.footer_note,
        has_logo=bool(row.logo_storage_key),
        # The house accent where none was chosen, so a preview cannot disagree
        # with the document a coach is about to hand over.
        effective_accent_hex=row.accent_hex or tokens.ACCENT,
    )


def _require_white_label(session: DbSession, user: User, settings: Settings) -> None:
    billing_service.require(
        session,
        user=user,
        action=EntitlementAction.WHITE_LABEL_EXPORT,
        settings=settings,
    )


@router.get("/branding", summary="My branding")
def get_branding(session: DbSession, user: CurrentUser, settings: Config) -> BrandingOut:
    _require_white_label(session, user, settings)
    return _branding_out(branding_service.for_coach(session, coach=user))


@router.patch("/branding", summary="Set a display name, accent or footer line")
def update_branding(
    payload: BrandingUpdate, session: DbSession, user: CurrentUser, settings: Config
) -> BrandingOut:
    """**One colour, not a theme.**

    The accent replaces a single value and is refused if it would not survive
    printing: the race card is read in a transition tent, often through a wet
    sleeve and often after somebody photocopied it, and the person holding an
    unreadable one cannot fix it. The layout, the typography and every
    safeguard in the artefact are fixed.
    """
    _require_white_label(session, user, settings)
    row = branding_service.update(
        session,
        coach=user,
        display_name=payload.display_name,
        accent_hex=payload.accent_hex,
        footer_note=payload.footer_note,
        clear_accent=payload.clear_accent,
    )
    session.commit()
    return _branding_out(row)


@router.put("/branding/logo", summary="Upload a logo")
def upload_branding_logo(
    session: DbSession,
    user: CurrentUser,
    settings: Config,
    file: Annotated[UploadFile, File(description="A PNG, JPEG or WebP")],
) -> BrandingOut:
    """PNG, JPEG or WebP, and the bytes are checked against what they claim.

    No SVG: it is a document format carrying script and external references,
    and this file is rendered into a PDF on our own server.
    """
    _require_white_label(session, user, settings)
    row = branding_service.set_logo(
        session,
        coach=user,
        data=file.file.read(),
        content_type=file.content_type or "",
        settings=settings,
    )
    session.commit()
    return _branding_out(row)


@router.get(
    "/branding/logo",
    response_class=Response,
    summary="The stored logo",
)
def get_branding_logo(session: DbSession, user: CurrentUser, settings: Config) -> Response:
    """Served from its own endpoint rather than inlined on the settings read.

    A base64 blob on every read would be most of the payload, for an image the
    screen shows once.
    """
    _require_white_label(session, user, settings)
    row = branding_service.for_coach(session, coach=user)
    if row is None or not row.logo_storage_key:
        raise NotFound("No logo has been uploaded.")
    try:
        data = get_storage_backend(settings).get(row.logo_storage_key)
    except ObjectNotFoundError as error:
        raise NotFound("That logo is no longer stored. Upload it again.") from error
    return Response(
        content=data,
        media_type=row.logo_content_type or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.delete(
    "/branding/logo",
    status_code=status.HTTP_200_OK,
    summary="Remove the logo",
)
def delete_branding_logo(session: DbSession, user: CurrentUser, settings: Config) -> BrandingOut:
    _require_white_label(session, user, settings)
    row = branding_service.clear_logo(session, coach=user, settings=settings)
    session.commit()
    return _branding_out(row)


# ---------------------------------------------------------------------------
# Invites — the coach asks, the athlete decides
# ---------------------------------------------------------------------------


@router.post("/invites", status_code=status.HTTP_201_CREATED, summary="Invite an athlete")
def create_invite(
    payload: InviteRequest, session: DbSession, user: CurrentUser, settings: Config
) -> InviteResponse:
    """Creates a *pending* link. It grants nothing until the athlete accepts,
    and still grants nothing until they choose what to share."""
    invite = coach_service.invite(
        session, coach=user, athlete_email=payload.athlete_email, settings=settings
    )
    session.commit()
    return InviteResponse(
        link=_link_out(session, invite.link),
        invite_token=invite.token,
        invite_url=f"{settings.app_base_url}/coach/accept?token={invite.token}",
    )


@router.post("/invites/accept", summary="Accept an invite (athlete only)")
def accept_invite(
    payload: AcceptRequest, session: DbSession, user: CurrentUser, settings: Config
) -> CoachLinkOut:
    link = coach_service.accept_invite(
        session, athlete=user, token=payload.token, settings=settings
    )
    session.commit()
    return _link_out(session, link)


@router.post("/links/{link_id}/decline", summary="Decline an invite")
def decline_invite(link_id: UUID, session: DbSession, user: CurrentUser) -> CoachLinkOut:
    link = coach_service.decline_invite(session, athlete=user, link_id=link_id)
    session.commit()
    return _link_out(session, link)


# ---------------------------------------------------------------------------
# Links and permissions
# ---------------------------------------------------------------------------


@router.get("/athletes", summary="Athletes linked to me as a coach")
def list_athletes(session: DbSession, user: CurrentUser) -> list[CoachLinkOut]:
    return [_link_out(session, link) for link in coach_service.list_athletes(session, coach=user)]


@router.get("/coaches", summary="Coaches linked to me as an athlete")
def list_coaches(session: DbSession, user: CurrentUser) -> list[CoachLinkOut]:
    return [_link_out(session, link) for link in coach_service.list_coaches(session, athlete=user)]


@router.patch("/links/{link_id}/permissions", summary="Set what a coach may see")
def set_permissions(
    link_id: UUID, payload: PermissionPatch, session: DbSession, user: CurrentUser
) -> CoachLinkOut:
    """**Athlete only.** A coach cannot grant themselves access.

    The body has three fields. There is no constraints field to send, because
    constraints are not a permission that happens to be off.
    """
    link = coach_service.set_permissions(
        session,
        athlete=user,
        link_id=link_id,
        plans=payload.plans,
        build=payload.build,
        analysis=payload.analysis,
    )
    session.commit()
    return _link_out(session, link)


@router.post("/links/{link_id}/revoke", summary="End the relationship")
def revoke_link(link_id: UUID, session: DbSession, user: CurrentUser) -> CoachLinkOut:
    """Either side, immediately."""
    link = coach_service.revoke(session, actor=user, link_id=link_id)
    session.commit()
    return _link_out(session, link)


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------


@router.get("/board", summary="Race-week board across linked athletes")
def get_board(session: DbSession, user: CurrentUser, settings: Config) -> list[BoardRowOut]:
    """Coach tier only.

    An athlete who has not granted plan access still appears, with their
    numbers withheld and a reason — hiding them would make a coach think the
    invite failed.
    """
    billing_service.require(
        session, user=user, action=EntitlementAction.COACH_BOARD, settings=settings
    )
    return [_row(row) for row in coach_service.board(session, coach=user)]


@router.post("/compare", summary="Compare chosen athletes side by side")
def compare(
    payload: CompareRequest, session: DbSession, user: CurrentUser, settings: Config
) -> list[BoardRowOut]:
    billing_service.require(
        session, user=user, action=EntitlementAction.COACH_BOARD, settings=settings
    )
    rows = coach_service.compare(session, coach=user, athlete_ids=payload.athlete_ids)
    return [_row(row) for row in rows]


@router.get("/athletes/{athlete_id}/plans/{plan_id}", summary="Read a shared plan")
def get_athlete_plan(
    athlete_id: UUID, plan_id: UUID, session: DbSession, user: CurrentUser
) -> PlanDetail:
    """Requires live `plans` permission from *this* athlete.

    The permission is read on this request, not cached from when the link was
    made, so a revocation lands immediately.
    """
    coach_service.require_permission(session, coach=user, athlete_id=athlete_id, permission="plans")
    plan = session.get(Plan, plan_id)
    if plan is None or plan.user_id != athlete_id:
        from raceos.api.errors import NotFound

        raise NotFound("Plan not found.")
    return plan_detail(session, plan)


@router.post(
    "/athletes/{athlete_id}/plans/{plan_id}/build",
    summary="Re-solve a plan on the athlete's behalf",
)
def build_for_athlete(
    athlete_id: UUID,
    plan_id: UUID,
    session: DbSession,
    user: CurrentUser,
    settings: Config,
) -> PlanDetail:
    """Requires live `build` permission. The result is **not yet theirs**.

    A coach-built plan lands as `pending_athlete_approval`; the athlete
    approves it before it becomes their active plan. The solve reads the
    athlete's constraints as inputs — it never writes one.
    """
    coach_service.require_permission(session, coach=user, athlete_id=athlete_id, permission="build")
    from raceos.api.errors import NotFound

    plan = session.get(Plan, plan_id)
    if plan is None or plan.user_id != athlete_id:
        raise NotFound("Plan not found.")
    athlete = session.get(User, athlete_id)
    if athlete is None:  # pragma: no cover - FK CASCADE
        raise NotFound("Athlete not found.")

    # Stamped *before* the solve: the persist step reads it to decide whether
    # the new version lands pending-approval or active, and a plan that went
    # live in the athlete's account without them seeing it would be the wrong
    # outcome entirely.
    plan_service.mark_built_by_coach(session, plan=plan, coach=user)
    result = plan_service.solve_plan(
        session, plan=plan, user=athlete, settings=settings, force=True
    )
    session.commit()
    return plan_detail(session, result.plan)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


@router.post(
    "/athletes/{athlete_id}/notes",
    status_code=status.HTTP_201_CREATED,
    summary="Leave a note the athlete can read",
)
def write_note(
    athlete_id: UUID, payload: NoteRequest, session: DbSession, user: CurrentUser
) -> NoteOut:
    note = coach_service.write_note(session, coach=user, athlete_id=athlete_id, body=payload.body)
    session.commit()
    return NoteOut.model_validate(note)


@router.get("/notes", summary="Notes written about me")
def list_my_notes(session: DbSession, user: CurrentUser) -> list[NoteOut]:
    return [
        NoteOut.model_validate(note)
        for note in coach_service.list_notes(session, athlete_id=user.id)
    ]


@router.get("/athletes/{athlete_id}/notes", summary="Notes I wrote about an athlete")
def list_athlete_notes(athlete_id: UUID, session: DbSession, user: CurrentUser) -> list[NoteOut]:
    coach_service.require_permission(session, coach=user, athlete_id=athlete_id, permission="plans")
    return [
        NoteOut.model_validate(note)
        for note in coach_service.list_notes(session, athlete_id=athlete_id)
    ]
