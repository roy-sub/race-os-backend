"""The entitlement matrix, decided in isolation.

Pure decisions over a table, so every row of the published pricing matrix is
checked directly rather than inferred from an endpoint's status code.
"""

from __future__ import annotations

import pytest

from raceos.domain.entitlements import (
    RULES,
    Decision,
    EntitlementAction,
    EntitlementContext,
    Grant,
    athlete_seats,
    decide,
)
from raceos.domain.enums import UserTier

FREE = EntitlementContext(tier=UserTier.FREE, subscription_active=False)
PAID_RACE = EntitlementContext(
    tier=UserTier.PER_RACE, subscription_active=False, has_race_purchase=True
)
PAYING_NOW = EntitlementContext(
    tier=UserTier.FREE, subscription_active=False, has_open_authorization=True
)
SEASON = EntitlementContext(tier=UserTier.SEASON, subscription_active=True)
CANCELLED_SEASON = EntitlementContext(tier=UserTier.SEASON, subscription_active=False)
COACH = EntitlementContext(tier=UserTier.COACH, subscription_active=True)


def _allowed(action: EntitlementAction, context: EntitlementContext) -> bool:
    return decide(action, context).allowed


# ---------------------------------------------------------------------------
# The free tier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        EntitlementAction.COURSE_RECON,
        EntitlementAction.CUTOFF_CALCULATOR,
        EntitlementAction.CONDITIONS_HISTORY,
    ],
)
def test_recon_is_free_for_everyone(action: EntitlementAction) -> None:
    """The course library is the front door.

    A cut-off calculator behind a paywall makes the product impossible to
    evaluate, which is why the pricing page marks these free on every tier.
    """
    assert _allowed(action, FREE)


def test_a_free_user_cannot_solve() -> None:
    decision = decide(EntitlementAction.SOLVE_PLAN, FREE)
    assert not decision.allowed
    assert decision.purchasable_per_race, "the cheapest path must be offered"
    assert UserTier.SEASON in decision.required_tiers


# ---------------------------------------------------------------------------
# The promise that matters: a plan you paid for stays yours
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        EntitlementAction.SOLVE_PLAN,
        EntitlementAction.EXPORT_PLAN,
        EntitlementAction.DRIFT_RESOLVE,
        EntitlementAction.RACE_MODE,
    ],
)
def test_a_captured_purchase_is_permanent(action: EntitlementAction) -> None:
    """It survives cancellation, downgrade, and the race itself.

    This is the whole reason entitlements are scoped to actions rather than to
    subscription status: "is the subscription active?" gets this case wrong,
    and this case is the one that would make an athlete feel robbed.
    """
    assert _allowed(action, PAID_RACE)


@pytest.mark.parametrize(
    "action",
    [
        EntitlementAction.EXPORT_PLAN,
        EntitlementAction.DRIFT_RESOLVE,
        EntitlementAction.RACE_MODE,
    ],
)
def test_cancelling_a_season_does_not_revoke_a_paid_race(
    action: EntitlementAction,
) -> None:
    lapsed = EntitlementContext(
        tier=UserTier.SEASON, subscription_active=False, has_race_purchase=True
    )
    assert _allowed(action, lapsed)


def test_cancelling_a_season_does_stop_new_work() -> None:
    """What lapses is starting something new."""
    assert not _allowed(EntitlementAction.POST_RACE_ANALYSIS, CANCELLED_SEASON)
    assert not _allowed(EntitlementAction.CONSTRAINT_CALIBRATION, CANCELLED_SEASON)
    assert not _allowed(EntitlementAction.SEASON_HISTORY, CANCELLED_SEASON)


# ---------------------------------------------------------------------------
# Two-phase capture
# ---------------------------------------------------------------------------


def test_an_open_authorization_grants_the_solve_it_is_paying_for() -> None:
    """Otherwise the first solve could never happen.

    Capture is triggered by solve success, so at the moment the solve starts
    there is by definition nothing captured. Requiring one would mean charging
    before the athlete has the plan.
    """
    assert _allowed(EntitlementAction.SOLVE_PLAN, PAYING_NOW)


@pytest.mark.parametrize(
    "action",
    [
        EntitlementAction.EXPORT_PLAN,
        EntitlementAction.DRIFT_RESOLVE,
        EntitlementAction.RACE_MODE,
    ],
)
def test_an_open_authorization_alone_unlocks_nothing_else(
    action: EntitlementAction,
) -> None:
    """A hold is not a payment."""
    assert not _allowed(action, PAYING_NOW)


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------


def test_season_gets_unlimited_plans_and_the_after_race_tools() -> None:
    assert _allowed(EntitlementAction.SOLVE_PLAN, SEASON)
    assert _allowed(EntitlementAction.POST_RACE_ANALYSIS, SEASON)
    assert _allowed(EntitlementAction.CONSTRAINT_CALIBRATION, SEASON)


def test_the_coach_board_is_coach_only() -> None:
    assert _allowed(EntitlementAction.COACH_BOARD, COACH)
    assert not _allowed(EntitlementAction.COACH_BOARD, SEASON)
    assert not _allowed(EntitlementAction.WHITE_LABEL_EXPORT, SEASON)


def test_seats_match_the_published_matrix() -> None:
    """ "Athletes managed: —, —, 1, 15" from the pricing table."""
    assert athlete_seats(UserTier.FREE) == 0
    assert athlete_seats(UserTier.PER_RACE) == 0
    assert athlete_seats(UserTier.SEASON) == 1
    assert athlete_seats(UserTier.COACH) == 15


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------


def test_every_action_has_a_rule() -> None:
    """A new action without a rule would raise KeyError at request time."""
    assert set(RULES) == set(EntitlementAction)


def test_every_refusal_explains_itself_and_names_a_way_forward() -> None:
    """A disabled button with no reason is a support ticket."""
    for action in EntitlementAction:
        decision: Decision = decide(action, FREE)
        if decision.allowed:
            continue
        assert decision.reason, f"{action.value} refuses without saying why"
        assert (
            decision.required_tiers or decision.purchasable_per_race
        ), f"{action.value} refuses without naming a way forward"


def test_no_paid_action_is_granted_to_a_bare_free_account() -> None:
    for action, rule in RULES.items():
        if rule.grant is Grant.EVERYONE:
            continue
        assert not _allowed(action, FREE), f"{action.value} leaked to the free tier"
