"""Seed people, plans and history. Idempotent and re-runnable.

**Every plan here is produced by the real solver.** Nothing is hand-written
into `plans` or its children — a fixture with fabricated splits would let a
solver regression pass a demo, which is exactly backwards.

**No secret is seeded.** Development passwords are generated at random per run
and printed once to the operator's terminal; nothing is written to a file, and
re-running produces new ones. There is no default password in this repository.

The named athletes come from the product's own copy: Elena Marsh is the
worked example throughout `SOLVER_MODEL.md`, and Jonas Feldt is the coach in
the notifications mock.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raceos.config import Settings, get_settings
from raceos.db.models import (
    AdminRoleAssignment,
    CoachAthleteLink,
    Course,
    CourseBundle,
    CrowdReport,
    CrowdReportUpload,
    Incident,
    Invoice,
    Plan,
    Purchase,
    Race,
    Subscription,
    User,
)
from raceos.domain.enums import (
    AdminRole,
    AthleteLevel,
    CoachLinkStatus,
    ConstraintSource,
    CrowdCategory,
    Currency,
    DriftCause,
    IncidentSeverity,
    PlanStatus,
    RaceStatus,
    SubscriptionStatus,
    UserTier,
)
from raceos.logging import get_logger
from raceos.services import (
    admin_service,
    auth_service,
    billing_service,
    constraint_service,
    drift_service,
    plan_service,
    share_service,
)

logger = get_logger(__name__)

#: Athlete M from ``SOLVER_MODEL.md`` §B.2 — the worked example, so seeded
#: numbers are recognisable against the document.
ATHLETE_M: dict[str, float] = {
    "swim_threshold_pace": 105,
    "bike_threshold_power": 224,
    "run_threshold_pace": 282,
    "weight": 75,
    "sweat_rate": 1.1,
    "sodium_loss": 900,
    "gut_carb_ceiling": 75,
    "caffeine_tolerance": 300,
}


@dataclass(frozen=True)
class SeedPerson:
    email: str
    name: str
    level: AthleteLevel
    tier: UserTier
    currency: Currency
    #: Scale applied to the reference constraints, so the eleven athletes are
    #: genuinely different rather than eleven copies with different names.
    ability: float = 1.0
    admin_role: AdminRole | None = None


#: Two named people from the product copy, plus eleven athletes across every
#: tier, level and currency — enough that a screen built against this seed
#: cannot accidentally assume one shape.
PEOPLE: tuple[SeedPerson, ...] = (
    SeedPerson(
        "elena.marsh@example.com",
        "Elena Marsh",
        AthleteLevel.EXPERIENCED,
        UserTier.SEASON,
        Currency.GBP,
    ),
    SeedPerson(
        "jonas.feldt@example.com",
        "Jonas Feldt",
        AthleteLevel.EXPERIENCED,
        UserTier.COACH,
        Currency.EUR,
    ),
    SeedPerson(
        "aina.roig@example.com",
        "Aina Roig",
        AthleteLevel.FIRST,
        UserTier.FREE,
        Currency.EUR,
        ability=0.82,
    ),
    SeedPerson(
        "tom.brennan@example.com",
        "Tom Brennan",
        AthleteLevel.IMPROVER,
        UserTier.PER_RACE,
        Currency.GBP,
        ability=0.91,
    ),
    SeedPerson(
        "sara.lindqvist@example.com",
        "Sara Lindqvist",
        AthleteLevel.EXPERIENCED,
        UserTier.SEASON,
        Currency.EUR,
        ability=1.06,
    ),
    SeedPerson(
        "marcus.oyelaran@example.com",
        "Marcus Oyelaran",
        AthleteLevel.IMPROVER,
        UserTier.PER_RACE,
        Currency.USD,
        ability=0.95,
    ),
    SeedPerson(
        "priya.nair@example.com",
        "Priya Nair",
        AthleteLevel.EXPERIENCED,
        UserTier.SEASON,
        Currency.USD,
        ability=1.03,
    ),
    SeedPerson(
        "kenji.watanabe@example.com",
        "Kenji Watanabe",
        AthleteLevel.FIRST,
        UserTier.FREE,
        Currency.USD,
        ability=0.86,
    ),
    SeedPerson(
        "lucia.ferrari@example.com",
        "Lucia Ferrari",
        AthleteLevel.IMPROVER,
        UserTier.PER_RACE,
        Currency.EUR,
        ability=0.98,
    ),
    SeedPerson(
        "noah.dubois@example.com",
        "Noah Dubois",
        AthleteLevel.EXPERIENCED,
        UserTier.SEASON,
        Currency.GBP,
        ability=1.09,
    ),
    SeedPerson(
        "hannah.kowalski@example.com",
        "Hannah Kowalski",
        AthleteLevel.IMPROVER,
        UserTier.PER_RACE,
        Currency.GBP,
        ability=0.93,
    ),
    SeedPerson(
        "ops@example.com",
        "Ops Operator",
        AthleteLevel.EXPERIENCED,
        UserTier.FREE,
        Currency.GBP,
        admin_role=AdminRole.OPS,
    ),
    SeedPerson(
        "support@example.com",
        "Support Agent",
        AthleteLevel.EXPERIENCED,
        UserTier.FREE,
        Currency.GBP,
        admin_role=AdminRole.SUPPORT,
    ),
)


def _scaled_constraints(ability: float) -> dict[str, float]:
    """Reference constraints scaled by ability.

    Pace-like keys are *inverted* — a stronger athlete runs a lower number of
    seconds per kilometre — which is the sort of detail a seed that just
    multiplied everything would get backwards and nobody would notice until a
    screenshot showed a beginner outrunning an elite.
    """
    return {
        "swim_threshold_pace": round(ATHLETE_M["swim_threshold_pace"] / ability, 1),
        "run_threshold_pace": round(ATHLETE_M["run_threshold_pace"] / ability, 1),
        "bike_threshold_power": round(ATHLETE_M["bike_threshold_power"] * ability, 1),
        "weight": round(ATHLETE_M["weight"] * (0.94 + 0.12 * ability), 1),
        "sweat_rate": ATHLETE_M["sweat_rate"],
        "sodium_loss": ATHLETE_M["sodium_loss"],
        "gut_carb_ceiling": ATHLETE_M["gut_carb_ceiling"],
        "caffeine_tolerance": ATHLETE_M["caffeine_tolerance"],
    }


def _ensure_person(
    session: Session, person: SeedPerson, settings: Settings
) -> tuple[User, str | None]:
    """Create the account if it is absent. Returns the user and any new password."""
    existing = session.scalar(select(User).where(User.email == person.email))
    if existing is not None:
        return existing, None

    # Generated per run and never stored anywhere but the operator's terminal.
    password = f"dev-{secrets.token_urlsafe(12)}"
    result = auth_service.signup(
        session,
        email=person.email,
        password=password,
        settings=settings,
        name=person.name,
    )
    user = result.user
    user.level = person.level
    user.tier = person.tier
    user.currency = person.currency
    user.is_coach = person.tier is UserTier.COACH
    session.flush()

    if person.admin_role is not None:
        session.add(AdminRoleAssignment(user_id=user.id, role=person.admin_role))

    if person.tier in (UserTier.SEASON, UserTier.COACH):
        session.add(
            Subscription(
                user_id=user.id,
                tier=person.tier,
                status=SubscriptionStatus.ACTIVE,
                renews_at=datetime.now(UTC) + timedelta(days=280),
            )
        )

    if person.admin_role is None:
        for key, value in _scaled_constraints(person.ability).items():
            constraint_service.write_constraint(
                session,
                athlete_id=user.id,
                actor=user,
                key=key,
                value=value,
                source=(
                    # A first-timer's numbers are estimated, and the seed says
                    # so: provenance is the product's spine, and a seed that
                    # stamped everything `tested` would hide it.
                    ConstraintSource.ESTIMATED
                    if person.level is AthleteLevel.FIRST
                    else ConstraintSource.TESTED
                ),
                source_detail="seed",
            )
    session.flush()
    return user, password


# ---------------------------------------------------------------------------
# Races and plans
# ---------------------------------------------------------------------------


def _courses(session: Session) -> list[tuple[Course, CourseBundle]]:
    pairs: list[tuple[Course, CourseBundle]] = []
    for course in session.scalars(select(Course).order_by(Course.slug)):
        bundle = session.scalar(
            select(CourseBundle)
            .where(CourseBundle.course_id == course.id)
            .order_by(CourseBundle.version.desc())
            .limit(1)
        )
        if bundle is not None:
            pairs.append((course, bundle))
    return pairs


def _ensure_race(
    session: Session, *, user: User, course: Course, bundle: CourseBundle, days_out: int
) -> Race:
    event_date = datetime.now(UTC).date() + timedelta(days=days_out)
    existing = session.scalar(
        select(Race).where(
            Race.user_id == user.id,
            Race.course_id == course.id,
            Race.event_date == event_date,
        )
    )
    if existing is not None:
        return existing
    race = Race(
        user_id=user.id,
        course_id=course.id,
        course_bundle_id=bundle.id,
        event_date=event_date,
        start_time_local=time(7, 0),
        status=RaceStatus.COMPLETED if days_out < 0 else RaceStatus.UPCOMING,
        bib=f"{1000 + (hash(str(user.id)) % 900):d}",
    )
    session.add(race)
    session.flush()
    return race


def _solve(session: Session, *, race: Race, user: User, settings: Settings) -> Plan | None:
    """Run the real solver, once. Returns ``None`` where no plan is possible.

    Idempotent: a race that already carries a solved plan is left alone. Every
    solve inserts a new version, so re-running without this check would walk a
    seeded plan to v2, v3, v4 and quietly make "solved 18 Jun, v1" untrue.

    A seed that swallowed a solver failure would hide a regression, so the
    reason is logged and the caller decides.
    """
    existing = session.scalar(
        select(Plan)
        .where(
            Plan.race_id == race.id,
            Plan.status.in_((PlanStatus.ACTIVE, PlanStatus.PAST)),
            Plan.solved_at.is_not(None),
        )
        .order_by(Plan.version.desc())
        .limit(1)
    )
    if existing is not None:
        return existing

    plan = plan_service.create_draft(session, user=user, race_id=race.id)
    try:
        result = plan_service.solve_plan(
            session, plan=plan, user=user, settings=settings, force=True
        )
    except Exception as error:
        logger.warning(
            "seed plan did not solve",
            extra={
                "race_id": str(race.id),
                "user": user.email,
                "error_type": type(error).__name__,
            },
        )
        return None
    return result.plan


def _pay_for(session: Session, *, user: User, plan: Plan, settings: Settings) -> Purchase | None:
    """A captured purchase and its invoice, through the real billing path."""
    if billing_service.has_captured_purchase_for_race(
        session, user_id=user.id, race_id=plan.race_id
    ):
        return None
    authorization = billing_service.authorize(
        session,
        user=user,
        plan=plan,
        currency=user.currency,
        idempotency_key=f"seed-{plan.id}",
        settings=settings,
    )
    return billing_service.capture_for_plan(session, plan=plan, settings=settings) or (
        authorization.purchase
    )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def _seed_kpis(session: Session, *, days: int = 30) -> int:
    """Thirty days of snapshots, aggregated from whatever really exists.

    Back-dated rows are computed from the same function the nightly job uses,
    so a chart built on this data has the same shape as one built on
    production — including the days where nothing happened.
    """
    today = datetime.now(UTC).date()
    for offset in range(days, 0, -1):
        admin_service.snapshot_kpis(session, on_date=today - timedelta(days=offset))
    admin_service.snapshot_kpis(session, on_date=today)
    return days + 1


def _seed_crowd(session: Session, course: Course) -> CrowdReport | None:
    existing = session.scalar(select(CrowdReport).where(CrowdReport.course_id == course.id))
    if existing is not None:
        return existing
    report = CrowdReport(
        course_id=course.id,
        category=CrowdCategory.AID_STATION,
        title="Unlisted aid station around km 92",
        body=(
            "Water and cola at the top of the second climb, consistently "
            "reported and absent from the athlete guide."
        ),
        upload_count=0,
        agreement_weight_pct=78.0,
        affected_plans_count=0,
    )
    session.add(report)
    session.flush()

    # Independent uploaders, because agreement is computed across people.
    for person in PEOPLE[:8]:
        user = session.scalar(select(User).where(User.email == person.email))
        if user is None:
            continue
        session.add(
            CrowdReportUpload(
                crowd_report_id=report.id,
                user_id=user.id,
                payload={"km": 92.0, "category": "aid_station"},
                submitted_at=datetime.now(UTC) - timedelta(days=3),
            )
        )
    report.upload_count = 8
    session.flush()
    return report


def _seed_incidents(session: Session) -> int:
    if session.scalar(select(func.count()).select_from(Incident)):
        return 0
    entries = [
        (
            IncidentSeverity.SEV3,
            "Forecast provider returned 502 for 14 minutes; plans solved on "
            "their last known forecast.",
            14,
            "open-meteo",
            6,
        ),
        (
            IncidentSeverity.SEV2,
            "A course bundle published without its diff computed; blast radius "
            "was recomputed and athletes re-notified.",
            48,
            "bundle-publish",
            19,
        ),
    ]
    for severity, what, minutes, service, days_ago in entries:
        session.add(
            Incident(
                occurred_at=datetime.now(UTC) - timedelta(days=days_ago),
                severity=severity,
                what=what,
                duration_minutes=minutes,
                service_ref=service,
            )
        )
    session.flush()
    return len(entries)


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------


def seed_people(session: Session, settings: Settings | None = None) -> dict[str, Any]:
    """Idempotent. Re-running adds nothing and changes no existing row."""
    config = settings or get_settings()
    courses = _courses(session)
    if not courses:
        logger.warning("no courses seeded; run the course seed first")
        return {"people": 0, "plans": 0}

    created_passwords: dict[str, str] = {}
    people: list[tuple[SeedPerson, User]] = []
    for person in PEOPLE:
        user, password = _ensure_person(session, person, config)
        if password is not None:
            created_passwords[person.email] = password
        people.append((person, user))
    session.flush()

    athletes = [(p, u) for p, u in people if p.admin_role is None]
    plans_solved = 0
    drift_raised = 0
    shares = 0

    for index, (person, user) in enumerate(athletes):
        course, bundle = courses[index % len(courses)]

        # An upcoming race with a solved, paid plan.
        upcoming = _ensure_race(
            session, user=user, course=course, bundle=bundle, days_out=5 + index * 11
        )
        plan = _solve(session, race=upcoming, user=user, settings=config)
        if plan is not None:
            plans_solved += 1
            if person.tier is UserTier.PER_RACE:
                _pay_for(session, user=user, plan=plan, settings=config)

            # Every third athlete has an outstanding drift to review, so the
            # dashboard's "needs review" state is populated by a real event.
            #
            # The cause is a *constraint change*, not a forecast one. A seed
            # run has no forecast provider, so a forecast recompute produces
            # an identical hash and — correctly — no drift. Rather than
            # fabricate a drift row to make the screen look right, the
            # athlete's threshold is genuinely moved and the solver genuinely
            # disagrees with the stored plan. The event is real.
            if index % 3 == 0 and not drift_service.list_pending(session, plan=plan):
                sharpened = constraint_service.get_value(
                    session, athlete_id=user.id, key="bike_threshold_power"
                )
                if sharpened is not None:
                    constraint_service.write_constraint(
                        session,
                        athlete_id=user.id,
                        actor=user,
                        key="bike_threshold_power",
                        value=round(sharpened * 0.92, 1),
                        source=ConstraintSource.TESTED,
                        source_detail="seed: retest after a heavy block",
                        change_reason="seeded constraint change",
                    )
                    assessment = drift_service.shadow_recompute(
                        session,
                        plan=plan,
                        user=user,
                        settings=config,
                        cause=DriftCause.CONSTRAINT_CHANGE,
                    )
                    if assessment.material and drift_service.record(
                        session,
                        plan=plan,
                        user=user,
                        settings=config,
                        assessment=assessment,
                    ):
                        drift_raised += 1

            # And every fourth has shared theirs. Guarded: minting a fresh
            # token on every seed run would leave a growing pile of live
            # links to the same plan.
            if index % 4 == 0 and not share_service.list_links(session, plan=plan, user=user):
                share_service.create(
                    session,
                    plan=plan,
                    user=user,
                    settings=config,
                    expires_in_days=30,
                    recipient_label="Coach",
                )
                shares += 1

        # A past race, so post-race and season history have something to show.
        if index % 2 == 0:
            past_course, past_bundle = courses[(index + 1) % len(courses)]
            past = _ensure_race(
                session,
                user=user,
                course=past_course,
                bundle=past_bundle,
                days_out=-(30 + index * 9),
            )
            past_plan = _solve(session, race=past, user=user, settings=config)
            if past_plan is not None:
                plans_solved += 1
                past_plan.status = PlanStatus.PAST

        # A draft nobody has solved, so the builder's empty state is real.
        if index % 5 == 0:
            draft_course, draft_bundle = courses[(index + 2) % len(courses)]
            draft_race = _ensure_race(
                session,
                user=user,
                course=draft_course,
                bundle=draft_bundle,
                days_out=140 + index * 7,
            )
            plan_service.create_draft(session, user=user, race_id=draft_race.id)

    # Coach links: Jonas coaches three athletes, at three permission levels.
    coach = session.scalar(select(User).where(User.email == "jonas.feldt@example.com"))
    links = 0
    if coach is not None:
        for offset, (_, athlete) in enumerate(athletes[2:5]):
            existing = session.scalar(
                select(CoachAthleteLink).where(
                    CoachAthleteLink.coach_id == coach.id,
                    CoachAthleteLink.athlete_id == athlete.id,
                )
            )
            if existing is not None:
                continue
            session.add(
                CoachAthleteLink(
                    coach_id=coach.id,
                    athlete_id=athlete.id,
                    status=CoachLinkStatus.ACTIVE,
                    perm_plans=True,
                    perm_build=offset > 0,
                    perm_analysis=offset > 1,
                    invited_at=datetime.now(UTC) - timedelta(days=40),
                    accepted_at=datetime.now(UTC) - timedelta(days=39),
                )
            )
            links += 1
    session.flush()

    invoices = int(session.scalar(select(func.count()).select_from(Invoice)) or 0)
    currencies = {
        row[0].value for row in session.execute(select(Invoice.currency).distinct()).all()
    }
    crowd = _seed_crowd(session, courses[0][0])
    incidents = _seed_incidents(session)
    kpi_days = _seed_kpis(session)

    summary = {
        "people": len(people),
        "plans_solved": plans_solved,
        "drift_events": drift_raised,
        "share_links": shares,
        "coach_links": links,
        "invoices": invoices,
        "invoice_currencies": sorted(currencies),
        "crowd_reports": 1 if crowd else 0,
        "incidents": incidents,
        "kpi_days": kpi_days,
        "new_passwords": created_passwords,
    }
    logger.info(
        "seeded people and history",
        extra={key: value for key, value in summary.items() if key != "new_passwords"},
    )
    return summary
