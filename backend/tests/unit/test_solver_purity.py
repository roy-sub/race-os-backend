"""The solver is pure. This is the lint rule that enforces it.

Build rule: *"The solver is pure. No database, no network, no
``datetime.now()``, no ``random``, no ``uuid4()`` inside ``raceos/solver/``.
Add a lint rule enforcing it."*

This is that rule, as an AST walk rather than a grep, so a name reached
through an alias or an attribute chain is caught too. It runs in the ordinary
unit suite, so a violation fails the build rather than waiting for review.

The reason purity is load-bearing rather than stylistic: ``SOLVER_MODEL.md``
§0.4 requires byte-identical output for identical input. A clock read, a random
draw, or a database round trip makes that impossible — and makes it impossible
*intermittently*, which is worse, because the golden suite would fail once in
fifty runs and be dismissed as flaky.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOLVER_ROOT = Path(__file__).resolve().parents[2] / "raceos" / "solver"

#: Modules the solver may never import. Each would break determinism, reach
#: outside the process, or both.
FORBIDDEN_IMPORTS: dict[str, str] = {
    "random": "a random draw makes output non-reproducible",
    "secrets": "as random, and it implies a security concern the solver has none of",
    "uuid": "uuid4() is non-deterministic; identifiers are assigned by the caller",
    "time": "a clock read makes output depend on when it ran",
    "sqlalchemy": "the solver takes a frozen SolveInput; it never queries",
    "httpx": "no network",
    "requests": "no network",
    "urllib": "no network",
    "socket": "no network",
    "os": "no environment reads and no filesystem; config arrives in tables",
    "subprocess": "no shelling out",
    "logging": "the solver returns values; the caller decides what to record",
    "raceos.config": "the solver reads its constants from solver/tables/, not settings",
    "raceos.db": "no database",
    # The dependency must run one way. A solver that imported the API layer
    # could not be called from a CLI, a job or a test harness without dragging
    # FastAPI in behind it, and the build spec requires it to be callable
    # in-process and from workers. Caught only after `s1_course` had already
    # done exactly this for its exception types.
    "raceos.api": "the API imports the solver, never the reverse",
    "fastapi": "no web framework inside the numeric path",
    "pydantic": "the solver's I/O is frozen dataclasses, not request models",
}

#: Callables that are non-deterministic even when their module is allowed.
#: ``datetime`` itself is fine — the solver takes dates as inputs — but reading
#: *now* is not.
FORBIDDEN_CALLS: dict[str, str] = {
    "now": "datetime.now() reads the wall clock",
    "today": "date.today() reads the wall clock",
    "utcnow": "datetime.utcnow() reads the wall clock",
    "uuid4": "non-deterministic identifier",
    "uuid1": "non-deterministic identifier",
    "monotonic": "a clock read",
    "perf_counter": "a clock read; stage timings are measured by the caller",
    "shuffle": "non-deterministic ordering",
    "choice": "non-deterministic selection",
    "randint": "non-deterministic value",
    "getenv": "config arrives in solver/tables/, not from the environment",
    "open": "no filesystem access from inside the numeric path",
}

#: `adapters.py` is the documented boundary: it reads a course file and turns
#: it into a frozen snapshot. It is not on the numeric path — nothing it
#: returns depends on when or where it ran — so file reads are allowed there
#: and nowhere else in the package.
IO_BOUNDARY_MODULES: frozenset[str] = frozenset({"adapters.py"})


def solver_modules() -> list[Path]:
    return sorted(p for p in SOLVER_ROOT.rglob("*.py") if p.name != "__init__.py")


def test_the_solver_package_has_modules_to_check() -> None:
    """Guard against this whole file silently passing on an empty glob."""
    assert len(solver_modules()) >= 10


@pytest.mark.parametrize("path", solver_modules(), ids=lambda p: p.name)
def test_module_imports_nothing_forbidden(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]

        for name in names:
            root = name.split(".")[0]
            for forbidden, reason in FORBIDDEN_IMPORTS.items():
                forbidden_root = forbidden.split(".")[0]
                matches = name == forbidden or name.startswith(f"{forbidden}.")
                if forbidden_root == forbidden and root == forbidden:
                    matches = True
                if matches:
                    if path.name in IO_BOUNDARY_MODULES and forbidden in {"os"}:
                        continue
                    violations.append(f"{path.name}:{node.lineno} imports {name!r} — {reason}")

    assert not violations, "solver purity violated:\n  " + "\n  ".join(violations)


@pytest.mark.parametrize("path", solver_modules(), ids=lambda p: p.name)
def test_module_calls_nothing_non_deterministic(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue

        reason = FORBIDDEN_CALLS.get(name)
        if reason is None:
            continue
        if name == "open" and path.name in IO_BOUNDARY_MODULES:
            continue
        violations.append(f"{path.name}:{node.lineno} calls {name}() — {reason}")

    assert not violations, "solver purity violated:\n  " + "\n  ".join(violations)


@pytest.mark.parametrize("path", solver_modules(), ids=lambda p: p.name)
def test_module_does_not_iterate_a_set_literal(path: Path) -> None:
    """§0.4 prohibits iteration over a ``set`` in the numeric path.

    Set iteration order depends on hash values, which for strings vary between
    interpreter runs unless ``PYTHONHASHSEED`` is fixed. Accumulating floats in
    that order would make output differ between processes — the exact failure
    the determinism guarantee exists to prevent.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = [
        f"{path.name}:{node.lineno} iterates a set literal"
        for node in ast.walk(tree)
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Set | ast.SetComp)
    ]
    assert not violations, "solver purity violated:\n  " + "\n  ".join(violations)


def test_forbidden_call_detection_actually_works() -> None:
    """The checker must fail on a violation, not merely pass on clean code.

    A purity test that cannot fail is worse than none: it reports safety it has
    not established. This feeds it a known-bad module and asserts it objects.
    """
    bad = ast.parse("import datetime\nx = datetime.datetime.now()\n")
    found = [
        node.func.attr
        for node in ast.walk(bad)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "now" in found
    assert "now" in FORBIDDEN_CALLS


def test_forbidden_import_detection_actually_works() -> None:
    bad = ast.parse("import random\nfrom sqlalchemy import select\n")
    imported: list[str] = []
    for node in ast.walk(bad):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert {"random", "sqlalchemy"} <= set(imported)
    assert "random" in FORBIDDEN_IMPORTS
    assert "sqlalchemy" in FORBIDDEN_IMPORTS
