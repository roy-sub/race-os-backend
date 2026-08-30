"""Admin and ops, behind role-based access.

Support holds `support`; it cannot see the refunds workspace or the bundle
publish controls, and that is expressed by not holding `ops` rather than by a
hidden button.

**Support sees no athlete data without a live, athlete-approved grant.** The
grant endpoints are here; the gate is `admin_service.require_support_access`,
which also writes the athlete-visible access log.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from raceos.api.deps import Config, CurrentUser, DbSession, require_roles
from raceos.api.errors import NotFound
from raceos.api.schemas.billing import InvoiceOut, RefundOut, RefundRequest
from raceos.db.models import CrowdReport, Invoice, User
from raceos.domain.enums import (
    AdminRole,
    CrowdStatus,
    IncidentSeverity,
    RefundReason,
)
from raceos.services import admin_service, billing_service

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

SupportUser = Annotated[User, Depends(require_roles(AdminRole.SUPPORT))]
OpsUser = Annotated[User, Depends(require_roles(AdminRole.OPS))]
AdminUser = Annotated[User, Depends(require_roles(AdminRole.ADMIN))]


# ---------------------------------------------------------------------------
# KPIs and health
# ---------------------------------------------------------------------------


@router.get("/overview", summary="The ops landing page, from real series")
def overview(session: DbSession, settings: Config, actor: OpsUser) -> dict[str, object]:
    return admin_service.ops_overview(session, settings=settings)


@router.get("/kpis", summary="Daily KPI snapshots")
def kpis(
    session: DbSession,
    actor: OpsUser,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> list[dict[str, object]]:
    """Every figure aggregated from real rows. A day with no measurement
    reports null rather than zero."""
    return [
        {
            "date": row.date.isoformat(),
            "plans_solved": row.plans_solved,
            "solver_p50_ms": row.solver_p50_ms,
            "solver_p95_ms": row.solver_p95_ms,
            "solver_p99_ms": row.solver_p99_ms,
            "total_accounts": row.total_accounts,
            "paying_count": row.paying_count,
            "season_count": row.season_count,
            "coach_seat_count": row.coach_seat_count,
            "free_to_paid_pct": (
                float(row.free_to_paid_pct) if row.free_to_paid_pct is not None else None
            ),
        }
        for row in admin_service.kpi_series(session, days=days)
    ]


@router.get("/health", summary="Service health, written by probes")
def health(session: DbSession, settings: Config, actor: OpsUser) -> list[dict[str, object]]:
    rows = admin_service.refresh_service_health(session, settings=settings)
    session.commit()
    return [
        {"service": row.service_name, "status": row.status.value, "note": row.note} for row in rows
    ]


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


class IncidentRequest(BaseModel):
    severity: IncidentSeverity
    what: str = Field(min_length=1, max_length=2000)
    duration_minutes: int | None = Field(default=None, ge=0)
    service_ref: str | None = Field(default=None, max_length=120)


@router.post("/incidents", status_code=status.HTTP_201_CREATED, summary="Record an incident")
def record_incident(
    payload: IncidentRequest, session: DbSession, actor: OpsUser
) -> dict[str, object]:
    incident = admin_service.record_incident(
        session,
        actor=actor,
        severity=payload.severity,
        what=payload.what,
        duration_minutes=payload.duration_minutes,
        service_ref=payload.service_ref,
    )
    session.commit()
    return {
        "id": str(incident.id),
        "occurred_at": incident.occurred_at.isoformat(),
        "severity": incident.severity.value,
        "what": incident.what,
    }


@router.get("/incidents", summary="Recent incidents")
def list_incidents(session: DbSession, actor: OpsUser) -> list[dict[str, object]]:
    return [
        {
            "id": str(row.id),
            "occurred_at": row.occurred_at.isoformat(),
            "severity": row.severity.value,
            "what": row.what,
            "duration_minutes": row.duration_minutes,
            "service_ref": row.service_ref,
        }
        for row in admin_service.list_incidents(session)
    ]


# ---------------------------------------------------------------------------
# Crowd reports
# ---------------------------------------------------------------------------


@router.get("/crowd-reports", summary="Crowd findings awaiting a decision")
def list_crowd(
    session: DbSession,
    actor: OpsUser,
    report_status: Annotated[CrowdStatus | None, Query(alias="status")] = None,
) -> list[dict[str, object]]:
    return [
        {
            "id": str(row.id),
            "course_id": str(row.course_id),
            "category": row.category.value,
            "title": row.title,
            "body": row.body,
            "status": row.status.value,
            "confidence": row.confidence.value,
            "upload_count": row.upload_count,
            "agreement_weight_pct": float(row.agreement_weight_pct),
            "affected_plans_count": row.affected_plans_count,
        }
        for row in admin_service.list_crowd_reports(session, status=report_status)
    ]


class PromoteRequest(BaseModel):
    #: Override the evidence threshold. Audited, never a default.
    force: bool = False


@router.post("/crowd-reports/{report_id}/promote", summary="Accept a crowd finding")
def promote_crowd(
    report_id: UUID,
    payload: PromoteRequest,
    session: DbSession,
    settings: Config,
    actor: OpsUser,
) -> dict[str, object]:
    """Promotion labels the finding honestly. It never becomes `OFFICIAL`:
    agreement across forty athletes is strong evidence and still not the
    organiser's word."""
    report = session.get(CrowdReport, report_id)
    if report is None:
        raise NotFound("Crowd report not found.")
    verdict = admin_service.assess_crowd_report(session, report=report, settings=settings)
    admin_service.promote_crowd_report(
        session, report=report, actor=actor, settings=settings, force=payload.force
    )
    session.commit()
    return {
        "id": str(report.id),
        "status": report.status.value,
        "confidence": report.confidence.value,
        "independent_uploads": verdict.upload_count,
        "reason": verdict.reason,
    }


class ResolveRequest(BaseModel):
    status: CrowdStatus


@router.post("/crowd-reports/{report_id}/resolve", summary="Hold or reject a finding")
def resolve_crowd(
    report_id: UUID, payload: ResolveRequest, session: DbSession, actor: OpsUser
) -> dict[str, object]:
    report = session.get(CrowdReport, report_id)
    if report is None:
        raise NotFound("Crowd report not found.")
    admin_service.resolve_crowd_report(session, report=report, actor=actor, status=payload.status)
    session.commit()
    return {"id": str(report.id), "status": report.status.value}


# ---------------------------------------------------------------------------
# Refunds — ops, never support
# ---------------------------------------------------------------------------


@router.get("/invoices/{invoice_id}", summary="One invoice, for the refunds desk")
def get_invoice(invoice_id: UUID, session: DbSession, actor: OpsUser) -> InvoiceOut:
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise NotFound("Invoice not found.")
    return InvoiceOut.model_validate(invoice)


@router.post("/invoices/{invoice_id}/refund", summary="Refund an invoice")
def refund_invoice(
    invoice_id: UUID,
    payload: RefundRequest,
    session: DbSession,
    settings: Config,
    actor: OpsUser,
) -> RefundOut:
    """`race_cancelled` and `bundle_error` are supported operational paths,
    not narrative promises."""
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise NotFound("Invoice not found.")
    refund = billing_service.refund_invoice(
        session,
        invoice=invoice,
        actor=actor,
        reason=payload.reason or RefundReason.OTHER,
        amount_cents=payload.amount_cents,
        note=payload.note,
        settings=settings,
    )
    session.commit()
    return RefundOut.model_validate(refund)


# ---------------------------------------------------------------------------
# Support access — the athlete decides
# ---------------------------------------------------------------------------


class SupportRequest(BaseModel):
    athlete_id: UUID
    reason: str = Field(min_length=1, max_length=500)


def _grant_out(grant: object) -> dict[str, object]:
    from raceos.db.models import SupportAccessGrant

    assert isinstance(grant, SupportAccessGrant)
    return {
        "id": str(grant.id),
        "athlete_id": str(grant.athlete_id),
        "support_agent_id": str(grant.support_agent_id),
        "requested_at": grant.requested_at.isoformat(),
        "granted_at": grant.granted_at.isoformat() if grant.granted_at else None,
        "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
        "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
        "denied_at": grant.denied_at.isoformat() if grant.denied_at else None,
        "reason": (grant.scope or {}).get("reason"),
        # Athlete-visible by design: transparency, not internal audit.
        "accessed_log": grant.accessed_log,
    }


@router.post(
    "/support/access-requests",
    status_code=status.HTTP_201_CREATED,
    summary="Ask an athlete for access",
)
def request_access(
    payload: SupportRequest, session: DbSession, settings: Config, actor: SupportUser
) -> dict[str, object]:
    """Asking grants nothing. The athlete decides, and sees everything opened."""
    grant = admin_service.request_support_access(
        session,
        agent=actor,
        athlete_id=payload.athlete_id,
        reason=payload.reason,
        settings=settings,
    )
    session.commit()
    return _grant_out(grant)


@router.get(
    "/support/athletes/{athlete_id}/summary",
    summary="What support may see, under a live grant",
)
def support_summary(athlete_id: UUID, session: DbSession, actor: SupportUser) -> dict[str, object]:
    """Refused without a live grant, and logged when allowed.

    Deliberately narrow: enough to answer "is my plan there and did my payment
    land", and nothing about the athlete's body.
    """
    from sqlalchemy import func, select

    from raceos.db.models import Plan, Purchase, Race

    admin_service.require_support_access(
        session, agent=actor, athlete_id=athlete_id, what="account summary"
    )
    athlete = session.get(User, athlete_id)
    if athlete is None:  # pragma: no cover - checked in require
        raise NotFound("Athlete not found.")

    session.commit()
    return {
        "athlete_id": str(athlete.id),
        "email": athlete.email,
        "tier": athlete.tier.value,
        "races": int(
            session.scalar(select(func.count()).select_from(Race).where(Race.user_id == athlete_id))
            or 0
        ),
        "plans": int(
            session.scalar(select(func.count()).select_from(Plan).where(Plan.user_id == athlete_id))
            or 0
        ),
        "purchases": int(
            session.scalar(
                select(func.count()).select_from(Purchase).where(Purchase.user_id == athlete_id)
            )
            or 0
        ),
        # Not present, deliberately: no constraint value, and no plan content.
        "constraints": "not visible to support at any permission level",
    }


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


class RoleRequest(BaseModel):
    user_id: UUID
    role: AdminRole
    granted: bool


@router.post("/roles", summary="Grant or remove an admin role")
def set_role(payload: RoleRequest, session: DbSession, actor: AdminUser) -> dict[str, object]:
    """Admin only, and itself audited."""
    admin_service.set_admin_role(
        session,
        actor=actor,
        user_id=payload.user_id,
        role=payload.role,
        granted=payload.granted,
    )
    session.commit()
    return {
        "user_id": str(payload.user_id),
        "role": payload.role.value,
        "granted": payload.granted,
    }


# ---------------------------------------------------------------------------
# The athlete's side of support access
# ---------------------------------------------------------------------------

athlete_router = APIRouter(prefix="/api/v1/support-access", tags=["support"])


@athlete_router.get("", summary="Support requests on my account")
def my_grants(session: DbSession, user: CurrentUser) -> list[dict[str, object]]:
    """Including the log of everything an agent opened."""
    return [
        _grant_out(grant) for grant in admin_service.list_grants_for_athlete(session, athlete=user)
    ]


@athlete_router.post("/{grant_id}/approve", summary="Allow one hour of access")
def approve(
    grant_id: UUID, session: DbSession, user: CurrentUser, settings: Config
) -> dict[str, object]:
    grant = admin_service.approve_support_access(
        session, athlete=user, grant_id=grant_id, settings=settings
    )
    session.commit()
    return _grant_out(grant)


@athlete_router.post("/{grant_id}/deny", summary="Refuse the request")
def deny(grant_id: UUID, session: DbSession, user: CurrentUser) -> dict[str, object]:
    grant = admin_service.deny_support_access(session, athlete=user, grant_id=grant_id)
    session.commit()
    return _grant_out(grant)


@athlete_router.post("/{grant_id}/revoke", summary="End access now")
def revoke(grant_id: UUID, session: DbSession, user: CurrentUser) -> dict[str, object]:
    grant = admin_service.revoke_support_access(session, athlete=user, grant_id=grant_id)
    session.commit()
    return _grant_out(grant)
