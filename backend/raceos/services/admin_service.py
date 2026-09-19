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

from sqlalchemy import ColumnElement, Select, func, select
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
    AccountState,
    AdminRole,
    CrowdConfidence,
    CrowdStatus,
    CurationStatus,
    Currency,
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
        # A privacy notice a convenience preference can mute is not a notice.
        # This used to be DIGEST, so switching the weekly summary off also
        # switched off being told somebody asked to read your account.
        type_key=NotificationType.SUPPORT_ACCESS,
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
        # Which model is actually live. "Off" and "on, and every call failing"
        # look identical from outside — both return the deterministic text —
        # so an operator needs to be able to tell them apart.
        "phrasing_provider": _phrasing_provider(settings),
    }


def _phrasing_boundary() -> dict[str, Any]:
    from raceos.services import phrasing_service

    return phrasing_service.describe_boundary()


def _phrasing_provider(settings: Settings) -> dict[str, Any]:
    from raceos.services import phrasing_service

    return phrasing_service.describe_provider(settings)


# ---------------------------------------------------------------------------
# Account administration
# ---------------------------------------------------------------------------
#
# The line this surface must not cross
# ------------------------------------
# Everything below reports *account* facts: who holds an account, what they
# pay, what roles they hold, what state the account is in. It reports no
# athlete content — no plans, no races, no constraints, no physiology, no
# emergency contact.
#
# That is not a tidiness preference. Support access is consent-gated: an agent
# sees an athlete's data only after that athlete approves, for one hour, and
# every read is appended to a log the athlete can open. An admin list that
# quietly carried plans or measurements would be a second door into the same
# room with none of the lock on it, and the athlete would never know it had
# been used. `support_summary` stays the only way to athlete content, and it
# still demands a live grant.
#
# The one identifier here that is personal — the email address — is the thing
# an operator searches by, so a user-administration screen cannot do its job
# without it. A test asserts the rest stays out.


#: Fields an athlete owns that must never appear in an admin account view.
#: Named rather than implied so the test that enforces it reads as a list of
#: promises rather than a regex.
WITHHELD_FROM_ACCOUNT_VIEW = (
    "date_of_birth",
    "emergency_contact_name",
    "emergency_contact_phone",
    "password_hash",
    "avatar_url",
)

#: A page of accounts. Large enough to scan, small enough to stay one query.
ACCOUNT_PAGE_SIZE = 50


@dataclass(frozen=True)
class AccountRow:
    """One account as the administration screen sees it."""

    id: UUID
    email: str
    name: str | None
    tier: UserTier
    account_state: AccountState
    is_coach: bool
    email_verified: bool
    roles: tuple[AdminRole, ...]
    created_at: datetime
    subscription_status: SubscriptionStatus | None


def _roles_by_user(session: Session, user_ids: list[UUID]) -> dict[UUID, tuple[AdminRole, ...]]:
    """One query for the whole page rather than one per row."""
    from raceos.db.models import AdminRoleAssignment

    if not user_ids:
        return {}
    grouped: dict[UUID, list[AdminRole]] = {}
    rows = session.execute(
        select(AdminRoleAssignment.user_id, AdminRoleAssignment.role).where(
            AdminRoleAssignment.user_id.in_(user_ids)
        )
    )
    for user_id, role in rows:
        grouped.setdefault(user_id, []).append(role)
    return {key: tuple(sorted(value, key=lambda r: r.value)) for key, value in grouped.items()}


def _live_subscription_by_user(
    session: Session, user_ids: list[UUID]
) -> dict[UUID, SubscriptionStatus]:
    """The status that matters per account, worst-case first.

    An account can carry more than one subscription row over its life. What an
    operator needs on a list is whether money is currently moving, so an
    active row wins over a past-due one and both win over a cancelled one.
    """
    from raceos.db.models import Subscription

    if not user_ids:
        return {}
    rank = {
        SubscriptionStatus.ACTIVE: 0,
        SubscriptionStatus.PAST_DUE: 1,
        SubscriptionStatus.CANCELLED: 2,
    }
    best: dict[UUID, SubscriptionStatus] = {}
    rows = session.execute(
        select(Subscription.user_id, Subscription.status).where(Subscription.user_id.in_(user_ids))
    )
    for user_id, status in rows:
        current = best.get(user_id)
        if current is None or rank[status] < rank[current]:
            best[user_id] = status
    return best


def search_accounts(
    session: Session,
    *,
    query: str | None = None,
    tier: UserTier | None = None,
    role: AdminRole | None = None,
    state: AccountState | None = None,
    limit: int = ACCOUNT_PAGE_SIZE,
    offset: int = 0,
) -> tuple[list[AccountRow], int]:
    """Find an account. Returns the page and the true total behind it.

    The total is the count of everything matching, not the length of the page,
    so an operator can tell "three results" from "the first three of nine
    hundred" — the distinction that decides whether searching harder is worth
    it.
    """
    from raceos.db.models import AdminRoleAssignment

    clauses: list[ColumnElement[bool]] = []
    if query:
        pattern = f"%{query.strip()}%"
        clauses.append(User.email.ilike(pattern) | User.name.ilike(pattern))
    if tier is not None:
        clauses.append(User.tier == tier)
    if state is not None:
        clauses.append(User.account_state == state)
    if role is not None:
        clauses.append(
            User.id.in_(select(AdminRoleAssignment.user_id).where(AdminRoleAssignment.role == role))
        )

    total = int(session.scalar(select(func.count()).select_from(User).where(*clauses)) or 0)
    users = list(
        session.scalars(
            select(User)
            .where(*clauses)
            .order_by(User.created_at.desc(), User.id)
            .limit(limit)
            .offset(offset)
        )
    )

    ids = [user.id for user in users]
    roles = _roles_by_user(session, ids)
    subscriptions = _live_subscription_by_user(session, ids)

    rows = [
        AccountRow(
            id=user.id,
            email=user.email,
            name=user.name,
            tier=user.tier,
            account_state=user.account_state,
            is_coach=user.is_coach,
            email_verified=user.email_verified_at is not None,
            roles=roles.get(user.id, ()),
            created_at=user.created_at,
            subscription_status=subscriptions.get(user.id),
        )
        for user in users
    ]
    return rows, total


def account_detail(session: Session, *, user_id: UUID) -> dict[str, Any]:
    """One account, in administration terms.

    The counts are counts. A plan total tells an operator whether an account
    is in use without telling them what any plan says, which is the whole
    distinction this surface is built on.
    """
    from raceos.db.models import Invoice, Subscription

    user = session.get(User, user_id)
    if user is None:
        raise NotFound("User not found.")

    subscriptions = list(
        session.scalars(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
        )
    )
    invoiced = session.execute(
        select(Invoice.currency, func.sum(Invoice.amount_cents), func.count())
        .where(Invoice.user_id == user_id)
        .group_by(Invoice.currency)
    ).all()
    plan_count = int(
        session.scalar(select(func.count()).select_from(Plan).where(Plan.user_id == user_id)) or 0
    )
    grants = int(
        session.scalar(
            select(func.count())
            .select_from(SupportAccessGrant)
            .where(SupportAccessGrant.athlete_id == user_id)
        )
        or 0
    )

    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "tier": user.tier.value,
        "account_state": user.account_state.value,
        "is_coach": user.is_coach,
        "email_verified": user.email_verified_at is not None,
        "created_at": user.created_at.isoformat(),
        "roles": [role.value for role in _roles_by_user(session, [user_id]).get(user_id, ())],
        "subscriptions": [
            {
                "id": str(row.id),
                "tier": row.tier.value,
                "status": row.status.value,
                "renews_at": row.renews_at.isoformat() if row.renews_at else None,
                "cancel_at": row.cancel_at.isoformat() if row.cancel_at else None,
            }
            for row in subscriptions
        ],
        # Per currency, never summed. See `revenue` for why.
        "invoiced": [
            {"currency": currency.value, "amount_cents": int(total or 0), "count": int(count)}
            for currency, total, count in invoiced
        ],
        "plan_count": plan_count,
        "support_grant_count": grants,
        # Said out loud so an operator does not read the absence of plans as
        # an empty account and go looking for a screen that shows them.
        "athlete_data": (
            "Not shown here. Athlete content requires a support-access grant "
            "the athlete approves, and every read of it is logged to them."
        ),
    }


# ---------------------------------------------------------------------------
# Revenue and churn
# ---------------------------------------------------------------------------
#
# Why revenue is reported per currency and never summed
# ----------------------------------------------------
# Invoices are issued in GBP, USD or EUR, and this system stores no exchange
# rate — not a live one, not a rate at the time of the invoice. Adding the
# cents together would produce a number with no unit: "£1 + $1 = 2" is not a
# fact about anything. Picking a rate to convert with would be worse, because
# the result would look like a real total, would move when nobody changed
# anything, and would be the figure someone quotes in a board meeting.
#
# So the answer is three answers, one per currency, and a caller who wants one
# number has to supply the rate that makes it true.


#: Refunds reduce the period they were *issued in*, not the period of the
#: invoice they reverse. An operator reading a month wants the money that
#: actually moved that month.
@dataclass(frozen=True)
class CurrencyRevenue:
    currency: Currency
    invoiced_cents: int
    refunded_cents: int
    invoice_count: int
    refund_count: int

    @property
    def net_cents(self) -> int:
        return self.invoiced_cents - self.refunded_cents


def revenue(session: Session, *, days: int = 30) -> list[CurrencyRevenue]:
    """Money invoiced and money given back, over a window, per currency.

    A currency with no activity in the window is absent rather than reported
    as zero, for the same reason a KPI with no measurement reports null: an
    operator must be able to tell "nobody paid in euros" from "we do not sell
    in euros", and a zero row says neither.
    """
    from raceos.db.models import Invoice, Refund

    since = datetime.now(UTC) - timedelta(days=days)

    invoiced = {
        currency: (int(total or 0), int(count))
        for currency, total, count in session.execute(
            select(Invoice.currency, func.sum(Invoice.amount_cents), func.count())
            .where(Invoice.issued_at >= since)
            .group_by(Invoice.currency)
        )
    }
    # A refund carries no currency of its own; it inherits the invoice's,
    # which is the only currency it could possibly be in.
    refunded = {
        currency: (int(total or 0), int(count))
        for currency, total, count in session.execute(
            select(Invoice.currency, func.sum(Refund.amount_cents), func.count())
            .join(Invoice, Invoice.id == Refund.invoice_id)
            .where(Refund.created_at >= since)
            .group_by(Invoice.currency)
        )
    }

    out = []
    for currency in sorted(set(invoiced) | set(refunded), key=lambda c: c.value):
        gross, invoice_count = invoiced.get(currency, (0, 0))
        back, refund_count = refunded.get(currency, (0, 0))
        out.append(
            CurrencyRevenue(
                currency=currency,
                invoiced_cents=gross,
                refunded_cents=back,
                invoice_count=invoice_count,
                refund_count=refund_count,
            )
        )
    return out


def churn(session: Session, *, days: int = 30) -> dict[str, Any]:
    """How many subscriptions ended in the window, against how many could.

    The denominator is subscriptions that existed *at the start* of the
    window, not at the end. A month in which a hundred people signed up and
    five of last month's fifty left is a ten per cent churn month; dividing by
    the end-of-month hundred and fifty would report three per cent and hide
    it. Growth must not be able to flatter retention.

    Reports ``None`` rather than ``0.0`` when there was nothing to churn. A
    rate computed from an empty denominator is not zero, it is undefined, and
    a zero on that chart reads as a perfect month.

    **The one approximation, stated rather than hidden.** No column records
    when a subscription was cancelled, so ``updated_at`` stands in for it. Any
    other edit to an already-cancelled row inside the window would make it
    look like it was cancelled there. That is rare, it only ever inflates the
    figure, and the honest fix is a ``cancelled_at`` column rather than a
    cleverer query — see `docs/LAUNCH_BLOCKERS.md`.
    """
    from raceos.db.models import Subscription

    now = datetime.now(UTC)
    since = now - timedelta(days=days)

    # Live when the window opened: anything not cancelled, plus the ones that
    # were cancelled inside the window. A past-due subscription counts — it was
    # still a subscription somebody could lose.
    was_live_at_open = (Subscription.status != SubscriptionStatus.CANCELLED) | (
        Subscription.updated_at >= since
    )
    at_risk = int(
        session.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.created_at < since, was_live_at_open)
        )
        or 0
    )
    lost = int(
        session.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.created_at < since,
                Subscription.status == SubscriptionStatus.CANCELLED,
                Subscription.updated_at >= since,
            )
        )
        or 0
    )

    by_tier = {
        tier.value: int(count)
        for tier, count in session.execute(
            select(Subscription.tier, func.count())
            .where(
                Subscription.status == SubscriptionStatus.CANCELLED,
                Subscription.updated_at >= since,
                Subscription.created_at < since,
            )
            .group_by(Subscription.tier)
        )
    }

    return {
        "window_days": days,
        "subscriptions_at_risk": at_risk,
        "subscriptions_lost": lost,
        "churn_pct": round(lost / at_risk * 100, 2) if at_risk else None,
        "lost_by_tier": by_tier,
        "active_now": int(
            session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.status == SubscriptionStatus.ACTIVE)
            )
            or 0
        ),
    }


# ---------------------------------------------------------------------------
# Course curation
# ---------------------------------------------------------------------------
#
# An athlete-submitted course is private to its submitter from the moment it
# builds, and stays that way unless a reviewer says otherwise. Publishing is
# the only act here that changes what anyone else can see, which is why it is
# the one with a person's name attached to it.


@dataclass(frozen=True)
class CurationRow:
    """One submitted course as the review queue shows it."""

    course_id: UUID
    slug: str
    name: str
    place: str | None
    distance_type: str
    event_date: date | None
    submitted_by: UUID
    submitted_by_email: str
    curation_status: CurationStatus
    curation_note: str | None
    curated_at: datetime | None
    created_at: datetime
    leg_count: int


def curation_queue(
    session: Session,
    *,
    status: CurationStatus | None = CurationStatus.UNREVIEWED,
    limit: int = ACCOUNT_PAGE_SIZE,
    offset: int = 0,
) -> tuple[list[CurationRow], int]:
    """Submitted courses awaiting, or having had, a decision.

    Defaults to the unreviewed ones, because that is the queue. Pass an
    explicit status to audit what was published or declined; pass ``None`` for
    everything an athlete has ever submitted.
    """
    from raceos.db.models import Course, CourseBundleLeg

    clauses: list[ColumnElement[bool]] = [Course.submitted_by_user_id.is_not(None)]
    if status is not None:
        clauses.append(Course.curation_status == status)

    total = int(session.scalar(select(func.count()).select_from(Course).where(*clauses)) or 0)
    courses = list(
        session.scalars(
            select(Course)
            .where(*clauses)
            .order_by(Course.created_at.asc(), Course.id)
            .limit(limit)
            .offset(offset)
        )
    )
    if not courses:
        return [], total

    emails: dict[UUID, str] = {}
    for user_id, email in session.execute(
        select(User.id, User.email).where(User.id.in_([c.submitted_by_user_id for c in courses]))
    ):
        emails[user_id] = email

    # How many of the three legs actually built. A reviewer's first question
    # about a submitted course is whether it is whole; `count(distinct leg)`
    # answers it without assuming a course has exactly one bundle.
    legs: dict[UUID, int] = {
        course_id: int(count)
        for course_id, count in session.execute(
            select(CourseBundle.course_id, func.count(func.distinct(CourseBundleLeg.leg)))
            .join(CourseBundleLeg, CourseBundleLeg.bundle_id == CourseBundle.id)
            .where(CourseBundle.course_id.in_([c.id for c in courses]))
            .group_by(CourseBundle.course_id)
        )
    }

    return [
        CurationRow(
            course_id=course.id,
            slug=course.slug,
            name=course.name,
            place=course.place,
            distance_type=course.distance_type.value,
            event_date=course.next_edition_date,
            submitted_by=course.submitted_by_user_id,
            submitted_by_email=emails.get(course.submitted_by_user_id, "(unknown)"),
            curation_status=course.curation_status,
            curation_note=course.curation_note,
            curated_at=course.curated_at,
            created_at=course.created_at,
            leg_count=legs.get(course.id, 0),
        )
        for course in courses
        if course.submitted_by_user_id is not None
    ], total


def _curate(
    session: Session,
    *,
    actor: User,
    settings: Settings,
    course_id: UUID,
    decision: CurationStatus,
    note: str | None,
) -> Any:
    from raceos.db.models import Course

    course = session.get(Course, course_id)
    if course is None:
        raise NotFound("Course not found.")
    if course.submitted_by_user_id is None:
        # A house course is in the catalogue by construction. Letting this
        # endpoint "publish" one would imply it had been out, and letting it
        # reject one would be a deletion wearing a review's clothes.
        raise InvalidInput(
            "That course was not submitted by an athlete, so there is nothing to review.",
            details={"course_id": str(course_id)},
        )

    before = {
        "curation_status": course.curation_status.value,
        "curation_note": course.curation_note,
    }
    course.curation_status = decision
    course.curation_note = note
    course.curated_at = datetime.now(UTC)
    course.curated_by_user_id = actor.id
    session.flush()

    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action=f"course.{decision.value}",
            entity_type="course",
            entity_id=course.id,
            before=before,
            after={
                "curation_status": decision.value,
                "curation_note": note,
                "slug": course.slug,
            },
        )
    )

    submitter = session.get(User, course.submitted_by_user_id)
    if submitter is not None:
        published = decision is CurationStatus.PUBLISHED
        notification_service.notify(
            session,
            user=submitter,
            settings=settings,
            type_key=NotificationType.COURSE_REVIEWED,
            severity=NotificationSeverity.INFO,
            title=(
                f"{course.name} is now in the course directory"
                if published
                else f"{course.name} was not added to the directory"
            ),
            body=(
                note
                or (
                    "Everyone can now plan a race on the course you added."
                    if published
                    else "It is still yours to plan on. Nobody else can see it."
                )
            ),
            cta_label="Open the course",
            cta_href=f"/courses/{course.slug}",
        )

    logger.info(
        "course.curated",
        extra={
            "course_id": str(course.id),
            "slug": course.slug,
            "decision": decision.value,
            "actor_user_id": str(actor.id),
        },
    )
    return course


def publish_course(
    session: Session,
    *,
    actor: User,
    settings: Settings,
    course_id: UUID,
    note: str | None = None,
) -> Any:
    """List a submitted course to everyone.

    The submitter stays recorded on the row. Publishing does not launder a
    course into looking like a surveyed one — `is_user_submitted` is still
    true, and the directory still says so.
    """
    return _curate(
        session,
        actor=actor,
        settings=settings,
        course_id=course_id,
        decision=CurationStatus.PUBLISHED,
        note=note,
    )


def reject_course(
    session: Session, *, actor: User, settings: Settings, course_id: UUID, note: str
) -> Any:
    """Decline a submitted course, with a reason.

    Takes nothing away: the course stays usable by the athlete who added it,
    and their plans against it are untouched. A reason is required because a
    rejection an athlete cannot act on is just a wall.
    """
    if not note.strip():
        raise InvalidInput("Say why it was not published. The submitter will read this.")
    return _curate(
        session,
        actor=actor,
        settings=settings,
        course_id=course_id,
        decision=CurationStatus.REJECTED,
        note=note,
    )
