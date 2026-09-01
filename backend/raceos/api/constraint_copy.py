"""Copy for the "Why this?" drawer, one entry per canonical constraint.

The solver produces the *numbers*; this module supplies the sentences around
them. It is deliberately separate from the solver for two reasons:

1.  **The golden suite must not move.** ``tests/golden/expected/*.json`` records
    the solver's output byte for byte and a diff blocks deploy. Prose is not a
    solver decision, so putting it here leaves that contract untouched.
2.  **It is copy.** Keeping it in one flat table means changing a sentence is a
    one-line edit by whoever owns the words, not a change to the model.

Nothing here introduces a number. Each drawer already carries the athlete's own
value, unit and source alongside this text, and the first of the three laws in
the README — the solver decides, the model phrases — means a sentence that
invented one would be wrong by construction.

``raceos.api.serialise`` fills these in only where the stored row has no text of
its own, so a future solver that emits richer copy wins over this table without
anything here having to be removed first.
"""

from __future__ import annotations

from typing import NamedTuple


class ConstraintCopy(NamedTuple):
    """What a value is, what it moves, and what overriding it means."""

    description: str
    affects_text: str
    override_text: str


#: Keyed by :data:`raceos.domain.enums.CONSTRAINT_KEYS`. All eight are present;
#: a key missing here renders an empty drawer rather than a wrong one.
CONSTRAINT_COPY: dict[str, ConstraintCopy] = {
    "swim_threshold_pace": ConstraintCopy(
        description=(
            "The pace you can hold in open water for about an hour before "
            "fatigue forces you slower."
        ),
        affects_text=(
            "Sets your swim split, and with it how much of the day is left for "
            "the bike and run to reach every cut-off in time."
        ),
        override_text=(
            "A tested time trial replaces an estimate here, and the swim " "re-solves against it."
        ),
    ),
    "bike_threshold_power": ConstraintCopy(
        description=(
            "The power you can hold on the bike for about an hour. Every bike "
            "target in this plan is solved as a fraction of it."
        ),
        affects_text=(
            "Sets the power target on each segment, the bike split, and the "
            "margin at every barrier on the bike."
        ),
        override_text=(
            "This is usually the value worth testing first: on a long course "
            "it is the one that most often binds."
        ),
    ),
    "run_threshold_pace": ConstraintCopy(
        description=(
            "The pace you can hold on foot for about an hour — off the bike, " "not fresh."
        ),
        affects_text=(
            "Sets run pace targets after heat is applied, and therefore your " "projected finish."
        ),
        override_text=(
            "A recent race gives the estimator enough to place this well; a "
            "tested value replaces it outright."
        ),
    ),
    "weight": ConstraintCopy(
        description=(
            "Your racing weight. It decides how much of your power goes into "
            "lifting you up a climb rather than moving you along it."
        ),
        affects_text=(
            "Changes your time on every climb, and the fluid and carbohydrate "
            "the plan expects you to absorb per hour."
        ),
        override_text=(
            "Worth keeping current: a stale weight quietly shifts every climb " "on the course."
        ),
    ),
    "sweat_rate": ConstraintCopy(
        description="How much fluid you lose per hour at race effort.",
        affects_text=(
            "Sets fluid per hour, and how much you have to carry across the "
            "widest gap between aid stations rather than collect at one."
        ),
        override_text=(
            "Weighing yourself before and after one session is enough for the "
            "estimator to place this properly."
        ),
    ),
    "sodium_loss": ConstraintCopy(
        description="How much sodium your sweat carries out with that fluid.",
        affects_text=(
            "Sets sodium per hour, and how much salt goes in a bag rather than "
            "being assumed available on course."
        ),
        override_text=(
            "Only a sweat test measures this directly. Until then the estimate "
            "carries full weight in the plan."
        ),
    ),
    "gut_carb_ceiling": ConstraintCopy(
        description=(
            "The most carbohydrate your gut has actually absorbed per hour " "without distress."
        ),
        affects_text=(
            "Caps carbohydrate per hour, and with it the total the plan can "
            "put into you across the whole day."
        ),
        override_text=(
            "You can fuel above it, but not silently: the override is recorded "
            "against the plan and shown wherever the number appears."
        ),
    ),
    "caffeine_tolerance": ConstraintCopy(
        description="How much caffeine you tolerate across a long day.",
        affects_text=(
            "Sets the total for the race and where in the run it is placed, so "
            "it lands when it is worth having."
        ),
        override_text=(
            "Practise the race-day amount in training first. This is not a "
            "number to discover on the day."
        ),
    ),
}
