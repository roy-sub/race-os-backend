"""What a given user may do, and why.

**Entitlements are scoped to actions, not to subscription status.** That
distinction is the whole design. A cancelled subscription must not take away
a plan the athlete already paid for and already raced on — their race card,
their exports and their offline Race Mode keep working permanently. What
lapses is the ability to start something *new*: another solve, another
analysis, another calibration.

Expressing that as "is the subscription active?" would get it wrong in the
one case that matters most, so the question every caller asks is "may this
user perform this action, on this thing?" and the answer here is pure: no
database, no clock, no network. The service layer gathers the facts; this
module decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from raceos.domain.enums import UserTier


class EntitlementAction(str, Enum):
    """Every gated action, from the pricing matrix the site publishes."""

    # Recon — free for everyone. The course library is the front door, and
    # putting a cut-off calculator behind a paywall would make the product
    # impossible to evaluate.
    COURSE_RECON = "course_recon"
    CUTOFF_CALCULATOR = "cutoff_calculator"
    CONDITIONS_HISTORY = "conditions_history"

    # The plan — bought per race, or unlimited on a season or coach tier.
    SOLVE_PLAN = "solve_plan"
    EXPORT_PLAN = "export_plan"
    DRIFT_RESOLVE = "drift_resolve"
    RACE_MODE = "race_mode"

    # After the race — season and coach only.
    POST_RACE_ANALYSIS = "post_race_analysis"
    CONSTRAINT_CALIBRATION = "constraint_calibration"
    SEASON_HISTORY = "season_history"

    # Coaching.
    COACH_BOARD = "coach_board"
    WHITE_LABEL_EXPORT = "white_label_export"


class Grant(str, Enum):
    """How an action can become permitted."""

    #: Available on every tier, including free.
    EVERYONE = "everyone"
    #: A captured purchase for *this race*, or an unlimited tier. Once
    #: captured it is permanent: it survives cancellation, downgrade and
    #: the race itself.
    RACE_PURCHASE_OR_TIER = "race_purchase_or_tier"
    #: As above, but an **open authorization on this plan also counts**. Only
    #: the solve uses it, and it is what makes two-phase capture possible: the
    #: capture happens on solve success, so at the moment the solve is
    #: authorised there is by definition nothing captured yet. Requiring a
    #: capture here would mean charging before the athlete had the plan, which
    #: is precisely the sequence the design exists to avoid.
    RACE_PAID_OR_PAYING_OR_TIER = "race_paid_or_paying_or_tier"
    #: An active subscription on a listed tier, checked at the moment of use.
    ACTIVE_TIER = "active_tier"


@dataclass(frozen=True)
class Rule:
    grant: Grant
    #: Tiers that satisfy the rule. Empty for :attr:`Grant.EVERYONE`.
    tiers: frozenset[UserTier] = frozenset()


UNLIMITED_PLAN_TIERS = frozenset({UserTier.SEASON, UserTier.COACH})
AFTER_RACE_TIERS = frozenset({UserTier.SEASON, UserTier.COACH})

#: The published pricing matrix, transcribed. Every row of the site's
#: comparison table has an entry here, so a claim on the pricing page and the
#: server's answer cannot disagree.
RULES: dict[EntitlementAction, Rule] = {
    EntitlementAction.COURSE_RECON: Rule(Grant.EVERYONE),
    EntitlementAction.CUTOFF_CALCULATOR: Rule(Grant.EVERYONE),
    EntitlementAction.CONDITIONS_HISTORY: Rule(Grant.EVERYONE),
    EntitlementAction.SOLVE_PLAN: Rule(Grant.RACE_PAID_OR_PAYING_OR_TIER, UNLIMITED_PLAN_TIERS),
    EntitlementAction.EXPORT_PLAN: Rule(Grant.RACE_PURCHASE_OR_TIER, UNLIMITED_PLAN_TIERS),
    EntitlementAction.DRIFT_RESOLVE: Rule(Grant.RACE_PURCHASE_OR_TIER, UNLIMITED_PLAN_TIERS),
    EntitlementAction.RACE_MODE: Rule(Grant.RACE_PURCHASE_OR_TIER, UNLIMITED_PLAN_TIERS),
    EntitlementAction.POST_RACE_ANALYSIS: Rule(Grant.ACTIVE_TIER, AFTER_RACE_TIERS),
    EntitlementAction.CONSTRAINT_CALIBRATION: Rule(Grant.ACTIVE_TIER, AFTER_RACE_TIERS),
    EntitlementAction.SEASON_HISTORY: Rule(Grant.ACTIVE_TIER, AFTER_RACE_TIERS),
    EntitlementAction.COACH_BOARD: Rule(Grant.ACTIVE_TIER, frozenset({UserTier.COACH})),
    EntitlementAction.WHITE_LABEL_EXPORT: Rule(Grant.ACTIVE_TIER, frozenset({UserTier.COACH})),
}

#: How many athletes each tier may manage. Season is the coach's own single
#: athlete; coach is the 15-seat tier from the matrix.
ATHLETE_SEATS: dict[UserTier, int] = {
    UserTier.FREE: 0,
    UserTier.PER_RACE: 0,
    UserTier.SEASON: 1,
    UserTier.COACH: 15,
}


@dataclass(frozen=True)
class EntitlementContext:
    """The facts a decision needs, gathered by the service layer."""

    tier: UserTier
    #: Whether the subscription backing :attr:`tier` is currently active. A
    #: cancelled season subscription still has ``tier == SEASON`` until the
    #: period ends, and that is exactly the case this flag disambiguates.
    subscription_active: bool
    #: Whether a **captured** purchase exists covering the race in question.
    #: Absent for actions that are not about one specific race.
    has_race_purchase: bool = False
    #: Whether a hold is currently placed on the plan being solved. Grants the
    #: solve and nothing else — an authorization is not a payment, so it must
    #: not unlock exports or Race Mode on its own.
    has_open_authorization: bool = False


@dataclass(frozen=True)
class Decision:
    allowed: bool
    action: EntitlementAction
    #: Empty when allowed. Written for the athlete, not for a log.
    reason: str = ""
    #: What would grant it, so the UI can offer the right upgrade rather than
    #: a generic paywall.
    required_tiers: tuple[UserTier, ...] = ()
    #: True when buying this one race would grant it — the cheapest path.
    purchasable_per_race: bool = False


_TIER_LABEL: dict[UserTier, str] = {
    UserTier.FREE: "Free",
    UserTier.PER_RACE: "Race plan",
    UserTier.SEASON: "Season",
    UserTier.COACH: "Coach",
}


def decide(action: EntitlementAction, context: EntitlementContext) -> Decision:
    """Whether *context* may perform *action*."""
    rule = RULES[action]

    if rule.grant is Grant.EVERYONE:
        return Decision(allowed=True, action=action)

    if rule.grant in (Grant.RACE_PURCHASE_OR_TIER, Grant.RACE_PAID_OR_PAYING_OR_TIER):
        if context.has_race_purchase:
            # Permanent, deliberately: this is the promise that a plan you
            # paid for stays yours after you cancel.
            return Decision(allowed=True, action=action)
        if rule.grant is Grant.RACE_PAID_OR_PAYING_OR_TIER and context.has_open_authorization:
            return Decision(allowed=True, action=action)
        if context.tier in rule.tiers and context.subscription_active:
            return Decision(allowed=True, action=action)
        return Decision(
            allowed=False,
            action=action,
            reason=(
                "This race has not been paid for. Buy this race plan, or "
                "subscribe for unlimited plans."
            ),
            required_tiers=_sorted(rule.tiers),
            purchasable_per_race=True,
        )

    # Grant.ACTIVE_TIER
    if context.tier in rule.tiers and context.subscription_active:
        return Decision(allowed=True, action=action)
    names = " or ".join(_TIER_LABEL[tier] for tier in _sorted(rule.tiers))
    return Decision(
        allowed=False,
        action=action,
        reason=f"This needs an active {names} subscription.",
        required_tiers=_sorted(rule.tiers),
    )


def _sorted(tiers: frozenset[UserTier]) -> tuple[UserTier, ...]:
    return tuple(sorted(tiers, key=lambda tier: tier.value))


def athlete_seats(tier: UserTier) -> int:
    return ATHLETE_SEATS[tier]
