"""The documentation, checked against the code it describes.

A README that claims a route count, a job table, or a decision range is making
a factual assertion. Left unchecked those drift within a release and become
the thing a new contributor trusts and is wrong about.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from raceos.api.main import create_app
from raceos.services import job_service
from tests.routes import iter_routes

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def readme() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def route_count() -> int:
    app = create_app()
    return sum(
        1
        for route in iter_routes(app)
        for _ in (getattr(route, "methods", set()) or set()) - {"HEAD", "OPTIONS"}
    )


def test_the_readme_route_count_is_true(readme: str, route_count: int) -> None:
    match = re.search(r"(\d+) routes\.", readme)
    assert match, "the README does not state a route count"
    assert int(match.group(1)) == route_count


def test_the_readme_route_table_sums_to_the_stated_total(readme: str, route_count: int) -> None:
    """The per-area table drifted to five short of the total while the total
    itself stayed true, because only the total was ever checked. A reader uses
    the table to find where a route lives, so a row that undercounts is the
    same class of untruth as a wrong total."""
    table = re.search(r"^\| Area \| Routes \|$.*?(?=\n\n)", readme, re.MULTILINE | re.DOTALL)
    assert table, "the README does not contain a route-area table"

    rows = re.findall(r"^\| (?!Area\b)(.+?) \| (\d+) \|$", table.group(0), re.MULTILINE)
    assert rows, "the route-area table has no rows"

    total = sum(int(n) for _, n in rows)
    assert (
        total == route_count
    ), f"the route-area table sums to {total} but the app has {route_count} routes"


def test_every_job_in_the_readme_table_exists(readme: str) -> None:
    """A cron configured from a stale table calls a 404 forever."""
    documented = set(re.findall(r"^\| `([a-z-]+)` \| `([^`]+)` \|", readme, re.MULTILINE))
    registry = job_service.registry()

    names = {name for name, _ in documented}
    assert names == set(registry), (
        f"README and registry disagree: only in README {sorted(names - set(registry))}, "
        f"only in code {sorted(set(registry) - names)}"
    )
    for name, cadence in documented:
        assert (
            registry[name].suggested_cron == cadence
        ), f"{name}: README says {cadence}, code says {registry[name].suggested_cron}"


def test_the_decision_log_is_contiguous() -> None:
    """A gap means a decision was recorded and then deleted, which is exactly
    the history this file exists to keep."""
    text = (REPO_ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    numbers = sorted({int(n) for n in re.findall(r"\bD-(\d{3})\b", text)})

    assert numbers, "no decisions recorded"
    assert numbers[0] == 1
    assert numbers == list(range(1, numbers[-1] + 1)), (
        f"gaps in the decision log: " f"{sorted(set(range(1, numbers[-1] + 1)) - set(numbers))}"
    )


def test_the_definition_of_done_matches_the_decision_log() -> None:
    text = (REPO_ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    dod = (REPO_ROOT / "docs" / "DEFINITION_OF_DONE.md").read_text(encoding="utf-8")

    highest = max(int(n) for n in re.findall(r"\bD-(\d{3})\b", text))
    claimed = re.search(r"D-001 to D-(\d{3})", dod)
    assert claimed, "the DoD does not state a decision range"
    assert int(claimed.group(1)) == highest


def test_every_document_the_readme_promises_exists(readme: str) -> None:
    for path in re.findall(r"`(docs/[A-Z_]+\.md|SOLVER_MODEL\.md)`", readme):
        assert (REPO_ROOT / path).is_file(), f"the README links {path}, which is absent"


def test_the_readme_makes_no_claim_about_an_absent_make_target(readme: str) -> None:
    """Every `make x` in the README has to be a target somebody can run."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-z][a-z-]*):", makefile, re.MULTILINE))
    for target in set(re.findall(r"`make ([a-z-]+)", readme)):
        assert target in targets, f"the README says `make {target}`, which does not exist"
