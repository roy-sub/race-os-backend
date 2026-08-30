"""Bundle publishing, behind ops RBAC.

Support cannot reach these endpoints — not because the UI hides the button,
but because they do not hold the role. Publishing a bundle changes the ground
under every plan pinned to the course, and the blast-radius preview exists so
that is never a surprise.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from raceos.api.deps import Config, DbSession, require_roles
from raceos.api.errors import InvalidInput
from raceos.api.schemas.drift import BlastRadiusOut, PublishRequest, PublishResultOut
from raceos.db.models import AuditLog, User
from raceos.domain.enums import AdminRole
from raceos.services import bundle_service

router = APIRouter(prefix="/api/v1/admin/bundles", tags=["admin"])

OpsUser = Annotated[User, Depends(require_roles(AdminRole.OPS))]


@router.get("/{bundle_id}/blast-radius", summary="What publishing would touch")
def get_blast_radius(
    bundle_id: UUID, session: DbSession, settings: Config, actor: OpsUser
) -> BlastRadiusOut:
    """Read-only, and it answers even inside the freeze window.

    An operator asking "how bad is this?" during a freeze should get the
    number, not an error — the refusal belongs on the publish, not the
    question.
    """
    bundle = bundle_service.bundle_or_404(session, bundle_id)
    radius = bundle_service.blast_radius(session, bundle=bundle, settings=settings)
    return BlastRadiusOut.model_validate(
        {
            **radius.__dict__,
            "affected": [item.__dict__ for item in radius.affected],
        }
    )


@router.post("/{bundle_id}/publish", summary="Publish, supersede and cascade")
def publish_bundle(
    bundle_id: UUID,
    payload: PublishRequest,
    session: DbSession,
    settings: Config,
    actor: OpsUser,
) -> PublishResultOut:
    """Raises a pending drift event per affected plan. **Rewrites no plan.**"""
    if payload.override_freeze and not (payload.override_reason or "").strip():
        raise InvalidInput(
            "Overriding the freeze window requires a reason: the audit entry "
            "has to say why athletes were interrupted in race week.",
            field="override_reason",
        )

    bundle = bundle_service.bundle_or_404(session, bundle_id)
    result = bundle_service.publish(
        session,
        bundle=bundle,
        actor=actor,
        settings=settings,
        override_freeze=payload.override_freeze,
    )

    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="bundle.publish",
            entity_type="course_bundle",
            entity_id=bundle.id,
            before={"version": result.superseded.version if result.superseded else None},
            after={
                "version": bundle.version,
                "plans_affected": result.plans_affected,
                "drift_events_raised": result.drift_events_raised,
                "override_freeze": payload.override_freeze,
                "override_reason": payload.override_reason,
            },
        )
    )
    session.commit()

    return PublishResultOut(
        bundle_id=result.bundle.id,
        version=result.bundle.version,
        published_at=result.bundle.published_at,
        superseded_version=result.superseded.version if result.superseded else None,
        plans_affected=result.plans_affected,
        drift_events_raised=result.drift_events_raised,
        field_deltas=result.field_deltas,
    )
