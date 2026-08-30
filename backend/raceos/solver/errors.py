"""Errors the solver raises. No HTTP knowledge, no framework imports.

These live here rather than in ``raceos.api`` because the dependency must run
one way: the API imports the solver, never the reverse. A solver that imported
its own exception types from the API layer could not be used by a CLI, a job,
or a test harness without dragging FastAPI in behind it — and the build spec
makes the solver "callable in-process and from workers".

:mod:`raceos.api.errors` re-exports these and maps them onto the taxonomy at
the boundary.
"""

from __future__ import annotations


class SolverInputError(Exception):
    """Base for problems with what the solver was handed."""


class BundleIncomplete(SolverInputError):
    """A course bundle cannot support a solve.

    Raised for the §1.2 and §1.3 invariants: a non-terrain elevation source,
    zero barriers, barriers out of chronological order, a segment spanning no
    delivered nodes, or an elevation series with too many clamped gradients.
    """


class MissingConstraint(SolverInputError):
    """A required athlete constraint is absent.

    Names the key. **Never a silent default** — a defaulted constraint would
    produce a plan for an athlete who does not exist, and nothing downstream
    could tell that from a real one.
    """

    def __init__(self, key: str) -> None:
        super().__init__(f"required constraint {key!r} is missing")
        self.key = key


class ImplausibleConstraint(SolverInputError):
    """A constraint is outside its plausibility range (§2.2).

    The API enforces the same table and returns ``INVALID_INPUT`` before a
    solve is attempted; the solver re-asserts it as a defensive postcondition,
    because a value that reaches the numeric path out of range produces a
    plausible-looking plan rather than an error.
    """

    def __init__(self, key: str, value: float, minimum: float, maximum: float) -> None:
        super().__init__(
            f"constraint {key!r} = {value} is outside its plausible range "
            f"[{minimum}, {maximum}]"
        )
        self.key = key
        self.value = value
        self.minimum = minimum
        self.maximum = maximum
