"""Admin and ops: KPIs, support grants, crowd promotion, incidents, health.

**Every number here is aggregated from a real series.** The solver percentiles
come from ``solve_timings`` rows written by actual solves; the account counts
from actual accounts. There is no display constant anywhere in this module,
and a KPI with no data reports ``None`` rather than a plausible figure — an
operator who cannot tell a real zero from a missing measurement will
eventually act on the wrong one.

**Support access is the third guarantee's other half.** An agent sees nothing
until the athlete approves, for one hour, non-renewable without fresh
approval, and every access is appended to a log the athlete can read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from raceos.api.errors import Conflict, Forbidden, InvalidInput, NotFound
from raceos.config import Settings
from raceos.db.models import (
    AuditLog,
    CourseBundle,
    CrowdReport,
    CrowdReportUpload,
    Incident,
    KpiSnapshot,
    Plan,
    ServiceHealth,
    SolveTiming,
    SupportAccessGrant,
    User,
)
from raceos.domain.enums import (
    AdminRole,
    CrowdConfidence,
    CrowdStatus,
    IncidentSeverity,
    NotificationSeverity,
    NotificationType,
    PlanStatus,
    ServiceStatus,
    SubscriptionStatus,
    UserTier,
)
from raceos.logging import get_logger
from raceos.services import notification_service

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Percentiles from the real series
# ---------------------------------------------------------------------------


def _percentile(session: Session, query: Select[tuple[int]], fraction: float) -> int | None:
    """Postgres' own ``percentile_disc`` over the measured rows.

    Discrete rather than continuous: every value it can return is a latency
    that actually happened, which is what makes "P95 is 5.4 s" a statement
    about a real request rather than an interpolation between two.
    """
    subquery = query.subquery()
    column = next(iter(subquery.c))
    value = session.scalar(
        select(func.percentile_disc(fraction).within_group(column.asc())).select_from(subquery)
    )
    return int(value) if value is not None else None


def solver_percentiles(session: Session, *, since: datetime | None = None) -> dict[str, int | None]:
    """P50/P95/P99 of real solve latency. ``None`` when nothing was measured."""
    query = select(SolveTiming.total_ms)
    if since is not None:
        query = query.where(SolveTiming.created_at >= since)

    measured = session.scalar(select(func.count()).select_from(query.subquery()))
    if not measured:
        # Not zero. An operator must be able to tell "nobody solved anything"
        # from "every solve was instant".
        return {"p50_ms": None, "p95_ms": None, "p99_ms": None, "samples": 0}

    return {
        "p50_ms": _percentile(session, query, 0.50),
        "p95_ms": _percentile(session, query, 0.95),
        "p99_ms": _percentile(session, query, 0.99),
        "samples": int(measured),
    }


def sla_breaches(session: Session, *, since: datetime | None = None) -> int:
    query = select(func.count()).select_from(SolveTiming).where(SolveTiming.exceeded_sla.is_(True))
    if since is not None:
        query = query.where(SolveTiming.created_at >= since)
    return int(session.scalar(query) or 0)


# ---------------------------------------------------------------------------
# KPI snapshots
# ---------------------------------------------------------------------------


def _counts(session: Session) -> dict[str, int]:
    from raceos.db.models import Subscription

    total = int(session.scalar(select(func.count()).select_from(User)) or 0)
    paying = int(
        session.scalar(
            select(func.count(func.distinct(Subscription.user_id))).where(
                Subscription.status == SubscriptionStatus.ACTIVE
            )
        )
        or 0
    )
    season = int(
        session.scalar(
            select(func.count(func.distinct(Subscription.user_id))).where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.tier == UserTier.SEASON,
            )
        )
        or 0
    )
    coach = int(
        session.scalar(
            select(func.count(func.distinct(Subscription.user_id))).where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.tier == UserTier.COACH,
            )
        )
        or 0
    )
    return {
        "total_accounts": total,
        "paying_count": paying,
        "season_count": season,
        "coach_seat_count": coach,
    }


def snapshot_kpis(session: Session, *, on_date: date | None = None) -> KpiSnapshot:
    """Aggregate one day. Idempotent: re-running replaces that day's row.

    Idempotent on purpose — a cron that fires twice, or a backfill over a week
    that already has rows, must not produce two truths for one date.
    """
    day = on_date or datetime.now(UTC).date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1)

    solved = int(
        session.scalar(
            select(func.count())
            .select_from(Plan)
            .where(
                Plan.solved_at >= start,
                Plan.solved_at < end,
                Plan.status.in_((PlanStatus.ACTIVE, PlanStatus.PAST)),
            )
        )
        or 0
    )

    day_query = select(SolveTiming.total_ms).where(
        SolveTiming.created_at >= start, SolveTiming.created_at < end
    )
    measured = int(session.scalar(select(func.count()).select_from(day_query.subquery())) or 0)
    counts = _counts(session)

    row = session.scalar(select(KpiSnapshot).where(KpiSnapshot.date == day))
    if row is None:
        row = KpiSnapshot(date=day)
        session.add(row)

    row.plans_solved = solved
    row.solver_p50_ms = _percentile(session, day_query, 0.50) if measured else None
    row.solver_p95_ms = _percentile(session, day_query, 0.95) if measured else None
    row.solver_p99_ms = _percentile(session, day_query, 0.99) if measured else None
    row.total_accounts = counts["total_accounts"]
    row.paying_count = counts["paying_count"]
    row.season_count = counts["season_count"]
    row.coach_seat_count = counts["coach_seat_count"]
    row.free_to_paid_pct = (
        round(counts["paying_count"] / counts["total_accounts"] * 100.0, 2)
        if counts["total_accounts"]
        else None
    )
    session.flush()
    return row


def kpi_series(session: Session, *, days: int = 30) -> list[KpiSnapshot]:
    since = datetime.now(UTC).date() - timedelta(days=days)
    return list(
        session.scalars(
            select(KpiSnapshot).where(KpiSnapshot.date >= since).order_by(KpiSnapshot.date)
        )
    )


# ---------------------------------------------------------------------------
# Support access — structural guarantee 3's other half
# ---------------------------------------------------------------------------


def request_support_access(
    session: Session,
    *,
    agent: User,
    athlete_id: UUID,
    reason: str,
    settings: Settings,
) -> SupportAccessGrant:
    """Ask. **Grants nothing.** The athlete decides."""
    athlete = session.get(User, athlete_id)
    if athlete is None:
        raise NotFound("Athlete not found.")
    if athlete.id == agent.id:
        raise InvalidInput("You cannot request access to your own account.")
    if not reason.strip():
        raise InvalidInput(
            "A support request needs a reason: the athlete is being asked to "
            "open their account and deserves to know why.",
            field="reason",
        )

    live = _live_grant(session, athlete_id=athlete_id, agent_id=agent.id)
    if live is not None:
        raise Conflict("You already hold live access to this account.")

    grant = SupportAccessGrant(
        athlete_id=athlete_id,
        support_agent_id=agent.id,
        requested_at=datetime.now(UTC),
        scope={"reason": reason.strip()},
        accessed_log=[],
    )
    session.add(grant)
    session.flush()

    notification_service.notify(
        session,
        user=athlete,
        settings=settings,
        type_key=NotificationType.DIGEST,
        severity=NotificationSeverity.WARN,
        title="Support has asked to look at your account.",
        body=(
            f"{agent.name or 'A support agent'} asked for one hour of access. "
            f"Their reason: {reason.strip()} You can approve or refuse, and "
            f"you will see everything they open."
        ),
        tag="SUPPORT ACCESS",
        cta_label="Review the request",
        cta_href="/settings?tab=privacy",
    )
    logger.info(
        "support.access_requested",
        extra={"grant_id": str(grant.id), "agent_id": str(agent.id)},
    )
    return grant


def approve_support_access(
    session: Session, *, athlete: User, grant_id: UUID, settings: Settings
) -> SupportAccessGrant:
    """**Only the athlete.** One hour, non-renewable without fresh approval."""
    grant = session.get(SupportAccessGrant, grant_id)
    if grant is None or grant.athlete_id != athlete.id:
        raise NotFound("Access request not found.")
    if grant.granted_at is not None:
        raise Conflict("That request was already approved.")
    if grant.denied_at is not None:
        raise Conflict("That request was already refused.")

    now = datetime.now(UTC)
    grant.granted_at = now
    grant.expires_at = now + timedelta(minutes=settings.support_grant_ttl_minutes)
    session.flush()
    logger.info("support.access_granted", extra={"grant_id": str(grant.id)})
    return grant


def deny_support_access(session: Session, *, athlete: User, grant_id: UUID) -> SupportAccessGrant:
    grant = session.get(SupportAccessGrant, grant_id)
    if grant is None or grant.athlete_id != athlete.id:
        raise NotFound("Access request not found.")
    if grant.granted_at is not None:
        raise Conflict("That request was already approved. Revoke it instead.")
    grant.denied_at = datetime.now(UTC)
    session.flush()
    return grant


def revoke_support_access(session: Session, *, athlete: User, grant_id: UUID) -> SupportAccessGrant:
    """Immediate. The next access check re-reads the row."""
    grant = session.get(SupportAccessGrant, grant_id)
    if grant is None or grant.athlete_id != athlete.id:
        raise NotFound("Access request not found.")
    grant.revoked_at = datetime.now(UTC)
    session.flush()
    logger.info("support.access_revoked", extra={"grant_id": str(grant.id)})
    return grant


def _live_grant(session: Session, *, athlete_id: UUID, agent_id: UUID) -> SupportAccessGrant | None:
    now = datetime.now(UTC)
    return session.scalar(
        select(SupportAccessGrant).where(
            SupportAccessGrant.athlete_id == athlete_id,
            SupportAccessGrant.support_agent_id == agent_id,
            SupportAccessGrant.granted_at.is_not(None),
            SupportAccessGrant.revoked_at.is_(None),
            SupportAccessGrant.expires_at > now,
        )
    )


def require_support_access(
    session: Session, *, agent: User, athlete_id: UUID, what: str
) -> SupportAccessGrant:
    """The gate on every support read, **and the thing that logs it**.

    Expiry is enforced here on every call rather than trusted from a flag set
    at approval time, so a grant that lapses mid-session stops working at the
    next request rather than at the next sweep.
    """
    grant = _live_grant(session, athlete_id=athlete_id, agent_id=agent.id)
    if grant is None:
        raise Forbidden(
            "You do not have live access to this account. Ask the athlete to "
            "approve a support session.",
            details={"athlete_id": str(athlete_id)},
        )
    # Append-only, and athlete-visible: transparency, not internal audit.
    grant.accessed_log = [
        *(grant.accessed_log or []),
        {
            "at": datetime.now(UTC).isoformat(),
            "what": what,
            "by": str(agent.id),
        },
    ]
    session.flush()
    return grant


def list_grants_for_athlete(session: Session, *, athlete: User) -> list[SupportAccessGrant]:
    return list(
        session.scalars(
            select(SupportAccessGrant)
            .where(SupportAccessGrant.athlete_id == athlete.id)
            .order_by(SupportAccessGrant.requested_at.desc())
        )
    )


def expire_support_grants(session: Session) -> dict[str, int]:
    """The sweeper. Belt and braces — expiry is already enforced on read."""
    now = datetime.now(UTC)
    lapsed = list(
        session.scalars(
            select(SupportAccessGrant).where(
                SupportAccessGrant.granted_at.is_not(None),
                SupportAccessGrant.revoked_at.is_(None),
                SupportAccessGrant.expires_at <= now,
            )
        )
    )
    for grant in lapsed:
        grant.revoked_at = now
    session.flush()
    return {"items_processed": len(lapsed)}


# ---------------------------------------------------------------------------
# Crowd promotion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrowdVerdict:
    confidence: CrowdConfidence
    upload_count: int
    agreement_pct: float
    eligible_for_promotion: bool
    reason: str


def assess_crowd_report(
    session: Session, *, report: CrowdReport, settings: Settings
) -> CrowdVerdict:
    """Confidence from **independent** uploads, never a raw count.

    One athlete uploading forty times is one observation. The unique index on
    ``(crowd_report_id, user_id)`` makes that true in the data; this counts
    what the index guarantees.
    """
    uploads = int(
        session.scalar(
            select(func.count(func.distinct(CrowdReportUpload.user_id))).where(
                CrowdReportUpload.crowd_report_id == report.id
            )
        )
        or 0
    )
    agreement = float(report.agreement_weight_pct or 0.0)

    if (
        uploads >= settings.crowd_confidence_high_uploads
        and agreement >= settings.crowd_confidence_high_agreement_pct
    ):
        confidence = CrowdConfidence.HIGH
    elif uploads >= settings.crowd_confidence_low_uploads:
        confidence = CrowdConfidence.MED
    else:
        confidence = CrowdConfidence.LOW

    eligible = uploads >= settings.crowd_verified_min_uploads
    reason = (
        f"{uploads} independent upload{'s' if uploads != 1 else ''} at "
        f"{agreement:.0f}% agreement; promotion needs "
        f"{settings.crowd_verified_min_uploads}."
    )
    return CrowdVerdict(
        confidence=confidence,
        upload_count=uploads,
        agreement_pct=agreement,
        eligible_for_promotion=eligible,
        reason=reason,
    )


def promote_crowd_report(
    session: Session,
    *,
    report: CrowdReport,
    actor: User,
    settings: Settings,
    force: bool = False,
) -> CrowdReport:
    """Accept a crowd finding as real. **Labelled honestly, never as official.**

    Promotion marks the report promoted; the bundle it informs still carries
    ``CROWD`` provenance, because agreement across forty athletes is strong
    evidence and it is still not the organiser's word.
    """
    if report.status is not CrowdStatus.PENDING:
        raise Conflict(f"That report is already {report.status.value}.")

    verdict = assess_crowd_report(session, report=report, settings=settings)
    if not verdict.eligible_for_promotion and not force:
        raise Conflict(f"Not enough independent evidence to promote this. {verdict.reason}")

    report.status = CrowdStatus.PROMOTED
    report.confidence = verdict.confidence
    report.resolved_at = datetime.now(UTC)
    report.resolved_by = actor.id
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="crowd.promote",
            entity_type="crowd_report",
            entity_id=report.id,
            before={"status": CrowdStatus.PENDING.value},
            after={
                "status": CrowdStatus.PROMOTED.value,
                "confidence": verdict.confidence.value,
                "independent_uploads": verdict.upload_count,
                "forced": force,
            },
        )
    )
    session.flush()
    logger.info(
        "crowd.promoted",
        extra={"report_id": str(report.id), "uploads": verdict.upload_count},
    )
    return report


def resolve_crowd_report(
    session: Session, *, report: CrowdReport, actor: User, status: CrowdStatus
) -> CrowdReport:
    if status not in (CrowdStatus.HELD, CrowdStatus.REJECTED):
        raise InvalidInput(f"{status.value} is not a resolution.", field="status")
    report.status = status
    report.resolved_at = datetime.now(UTC)
    report.resolved_by = actor.id
    session.flush()
    return report


def list_crowd_reports(session: Session, *, status: CrowdStatus | None = None) -> list[CrowdReport]:
    query = select(CrowdReport).order_by(CrowdReport.created_at.desc())
    if status is not None:
        query = query.where(CrowdReport.status == status)
    return list(session.scalars(query))


# ---------------------------------------------------------------------------
# Incidents and service health
# ---------------------------------------------------------------------------


def record_incident(
    session: Session,
    *,
    actor: User,
    severity: IncidentSeverity,
    what: str,
    occurred_at: datetime | None = None,
    duration_minutes: int | None = None,
    service_ref: str | None = None,
) -> Incident:
    if not what.strip():
        raise InvalidInput("An incident needs a description.", field="what")
    incident = Incident(
        occurred_at=occurred_at or datetime.now(UTC),
        severity=severity,
        what=what.strip(),
        duration_minutes=duration_minutes,
        service_ref=service_ref,
    )
    session.add(incident)
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="incident.record",
            entity_type="incident",
            entity_id=None,
            after={"severity": severity.value, "what": what.strip()},
        )
    )
    session.flush()
    return incident


def list_incidents(session: Session, *, limit: int = 50) -> list[Incident]:
    return list(
        session.scalars(select(Incident).order_by(Incident.occurred_at.desc()).limit(limit))
    )


def refresh_service_health(session: Session, *, settings: Settings) -> list[ServiceHealth]:
    """Written by checks, never by hand.

    Each probe is the cheapest call that would actually fail if the dependency
    were down — a hand-set "nominal" is worth nothing.
    """
    from raceos.payments import get_payment_gateway
    from raceos.storage.base import get_storage_backend

    probes: list[tuple[str, ServiceStatus, str | None]] = []

    try:
        session.execute(select(func.now()))
        probes.append(("database", ServiceStatus.NOMINAL, None))
    except Exception as error:  # pragma: no cover - the session would be dead
        probes.append(("database", ServiceStatus.DOWN, type(error).__name__))

    try:
        detail = get_storage_backend(settings).health()
        probes.append(("storage", ServiceStatus.NOMINAL, str(detail.get("backend"))))
    except Exception as error:
        probes.append(("storage", ServiceStatus.DOWN, type(error).__name__))

    try:
        detail = get_payment_gateway(settings).health()
        probes.append(("payments", ServiceStatus.NOMINAL, str(detail.get("gateway"))))
    except Exception as error:
        probes.append(("payments", ServiceStatus.DEGRADED, type(error).__name__))

    percentiles = solver_percentiles(session, since=datetime.now(UTC) - timedelta(hours=24))
    p95 = percentiles["p95_ms"]
    if p95 is None:
        solver_status, note = ServiceStatus.NOMINAL, "no solves in the last 24 hours"
    elif p95 > settings.solver_sla_ms:
        solver_status, note = ServiceStatus.DEGRADED, f"P95 {p95} ms over SLA"
    else:
        solver_status, note = ServiceStatus.NOMINAL, f"P95 {p95} ms"
    probes.append(("solver", solver_status, note))

    rows: list[ServiceHealth] = []
    for name, status, note_text in probes:
        row = session.scalar(select(ServiceHealth).where(ServiceHealth.service_name == name))
        if row is None:
            row = ServiceHealth(service_name=name)
            session.add(row)
        row.status = status
        row.note = note_text
        rows.append(row)
    session.flush()
    return rows


def service_health(session: Session) -> list[ServiceHealth]:
    return list(session.scalars(select(ServiceHealth).order_by(ServiceHealth.service_name)))


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def set_admin_role(
    session: Session, *, actor: User, user_id: UUID, role: AdminRole, granted: bool
) -> None:
    """Granting an admin role is itself an audited act."""
    from raceos.db.models import AdminRoleAssignment, AdminRoleAudit

    target = session.get(User, user_id)
    if target is None:
        raise NotFound("User not found.")

    existing = session.scalar(
        select(AdminRoleAssignment).where(
            AdminRoleAssignment.user_id == user_id, AdminRoleAssignment.role == role
        )
    )
    if granted and existing is None:
        session.add(AdminRoleAssignment(user_id=user_id, role=role))
    elif not granted and existing is not None:
        session.delete(existing)

    session.add(AdminRoleAudit(user_id=user_id, role=role, granted=granted, actor_user_id=actor.id))
    session.flush()
    logger.info(
        "admin.role_changed",
        extra={"user_id": str(user_id), "role": role.value, "granted": granted},
    )


def ops_overview(session: Session, *, settings: Settings) -> dict[str, Any]:
    """One read for the ops landing page."""
    since = datetime.now(UTC) - timedelta(days=30)
    published = int(
        session.scalar(
            select(func.count())
            .select_from(CourseBundle)
            .where(CourseBundle.published_at.is_not(None))
        )
        or 0
    )
    return {
        "solver": solver_percentiles(session, since=since),
        "sla_breaches_30d": sla_breaches(session, since=since),
        "accounts": _counts(session),
        "published_bundles": published,
        "pending_crowd_reports": len(list_crowd_reports(session, status=CrowdStatus.PENDING)),
        "open_support_grants": int(
            session.scalar(
                select(func.count())
                .select_from(SupportAccessGrant)
                .where(
                    SupportAccessGrant.granted_at.is_not(None),
                    SupportAccessGrant.revoked_at.is_(None),
                    SupportAccessGrant.expires_at > datetime.now(UTC),
                )
            )
            or 0
        ),
        "services": [
            {"name": row.service_name, "status": row.status.value, "note": row.note}
            for row in service_health(session)
        ],
        "phrasing_boundary": _phrasing_boundary(),
    }


def _phrasing_boundary() -> dict[str, Any]:
    from raceos.services import phrasing_service

    return phrasing_service.describe_boundary()
