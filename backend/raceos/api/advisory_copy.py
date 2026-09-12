"""What each model advisory means, in a sentence an athlete can act on.

The solver emits ``model:`` keys; this turns one into prose. Kept beside
:mod:`raceos.api.constraint_copy` for the same two reasons: it is words rather
than model behaviour, and the golden suite records the solver's output byte for
byte, so prose living in the solver would make every copy edit a golden diff.

**These sentences say a number is soft. They never say by how much.** There is
no duration term in the bike heat curve and no basis for inventing one — the
back-testing that would produce it needs hot full-distance races nobody has
published a dose-response over. A sentence here that quoted a correction would
be putting a figure on the page that nothing supports, which is the one thing
this model does not do.
"""

from __future__ import annotations

from typing import NamedTuple


class AdvisoryCopy(NamedTuple):
    """A short label, and what it means for this plan."""

    #: All-caps, for the tag beside the affected numbers.
    tag: str
    #: One sentence. States which numbers are soft and in which direction.
    text: str


#: Keyed by :data:`raceos.solver.tables.precedence.MODEL_LIMIT_KEYS`. A key with
#: no entry is omitted rather than rendered as a bare key — see
#: :func:`advisory_copy`.
ADVISORY_COPY: dict[str, AdvisoryCopy] = {
    "model:bike_heat_duration": AdvisoryCopy(
        tag="BIKE HEAT",
        text=(
            "Your bike power is reduced for the heat, using measurements taken "
            "over about an hour of riding. This leg is much longer than that, "
            "and heat strain builds with time on course — so the real cost is "
            "more likely to be larger than this than smaller. Plan the back "
            "half conservatively."
        ),
    ),
    "model:bike_heat_clamp": AdvisoryCopy(
        tag="BIKE HEAT",
        text=(
            "It is hotter than the measurements behind this curve go. Rather "
            "than guess past them, the reduction is held at the hottest value "
            "that was actually measured — so treat the bike target as a "
            "ceiling rather than a plan."
        ),
    ),
    "model:run_heat_clamp": AdvisoryCopy(
        tag="RUN HEAT",
        text=(
            "The heat adjustment on your run pace hit its limit. Past that "
            "point the model is well outside any data it was built from, so "
            "the run split is advisory: race to how you feel, and treat the "
            "cut-off margins on the run as optimistic."
        ),
    ),
}


def advisory_copy(key: str) -> AdvisoryCopy | None:
    """The copy for *key*, or ``None`` when there is none.

    ``None`` rather than a placeholder: a bare ``model:`` key rendered on a
    race card is worse than the advisory being absent, because it looks like a
    bug to the athlete and tells them nothing either way.
    """
    return ADVISORY_COPY.get(key)
