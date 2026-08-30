"""`bind()` — §0.5. Bindingness is a first-class output, not a post-hoc guess.

The product exposes a "Why this?" drawer on every number, so every emitted
value must be able to name the constraint that determined it. That is designed
in: **every clamped quantity is computed through this helper, never through
``min()``/``max()``.**

Ties break by **position in the candidate tuple, lowest index wins**. The tuple
order is a fixed, configured precedence list per quantity
(:mod:`raceos.solver.tables.precedence`), never the iteration order of a
runtime collection — a first-timer whose gut ceiling exactly equals the
duration-based carbohydrate target is a common case, and which key gets named
must not depend on dictionary ordering.
"""

from __future__ import annotations

from dataclasses import dataclass

from raceos.domain.enums import BindDirection


@dataclass(frozen=True)
class Candidate:
    """One limit competing to determine a value."""

    key: str
    limit: float
    direction: BindDirection


@dataclass(frozen=True)
class Bound:
    value: float
    binding_key: str


def bind(candidates: tuple[Candidate, ...]) -> Bound:
    """The most restrictive limit, and the key that produced it.

    ``UPPER`` candidates cap the value (the smallest wins); ``LOWER``
    candidates floor it (the largest wins). Mixing directions in one call is a
    programming error — a quantity is either being capped or floored — and is
    rejected rather than silently resolved.
    """
    if not candidates:
        raise ValueError("bind() needs at least one candidate")

    directions = {candidate.direction for candidate in candidates}
    if len(directions) > 1:
        raise ValueError(
            "bind() candidates must share a direction; got "
            f"{sorted(d.value for d in directions)}. A quantity is either "
            f"capped or floored, never both in one call."
        )

    direction = candidates[0].direction
    winner = candidates[0]
    for candidate in candidates[1:]:
        if direction is BindDirection.UPPER:
            more_restrictive = candidate.limit < winner.limit
        else:
            more_restrictive = candidate.limit > winner.limit
        # Strictly more restrictive only: on an exact tie the earlier
        # candidate keeps the binding key, which is the documented rule.
        if more_restrictive:
            winner = candidate

    return Bound(value=winner.limit, binding_key=winner.key)


def clamp(value: float, low: float, high: float) -> float:
    """A plain numeric clamp, for quantities that emit no binding key.

    Used where the document specifies a range guard rather than a competing
    limit — input sanity clamps, for instance. Anything a user could ask "why?"
    about goes through :func:`bind` instead.
    """
    if low > high:  # pragma: no cover - a misconfigured table
        raise ValueError(f"clamp bounds inverted: {low} > {high}")
    return max(low, min(high, value))
