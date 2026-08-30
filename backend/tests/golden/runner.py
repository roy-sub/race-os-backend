"""Running a golden case, and freezing its output.

Kept separate from the test module so the regeneration path and the assertion
path use exactly the same code. If they diverged, a regenerated golden could
pass its own test while differing from what the solver actually produces.
"""

from __future__ import annotations

import json
from pathlib import Path

from raceos.solver.adapters import load_golden_course
from raceos.solver.models import SCHEMA_VERSION, SolveInput, SolveOutput
from raceos.solver.pipeline import SolveInfeasible, solve
from raceos.solver.serialisation import canonical, golden_json, solve_input_hash

GOLDEN_DIR = Path(__file__).resolve().parent
COURSE_DIR = GOLDEN_DIR / "courses"
EXPECTED_DIR = GOLDEN_DIR / "expected"


def build_input(case) -> SolveInput:
    """Assemble the frozen ``SolveInput`` for a case."""
    return SolveInput(
        schema_version=SCHEMA_VERSION,
        athlete=case.athlete,
        course=load_golden_course(COURSE_DIR / f"{case.course_id}.json"),
        goal=case.goal,
        forecast=case.forecast,
        event=case.event(),
        options=case.options,
    )


def run_case(case) -> dict[str, object]:
    """Solve one case and return its frozen-file representation.

    ``stage_timings_ms`` is deliberately excluded: it is a real measurement, so
    it differs on every run and on every machine. Freezing it would make the
    golden suite fail for reasons that have nothing to do with correctness —
    the exact "flaky test nobody trusts" outcome the determinism guarantee
    exists to avoid.
    """
    request = build_input(case)
    payload: dict[str, object] = {
        "case_id": case.case_id,
        "course_id": case.course_id,
        "schema_version": SCHEMA_VERSION,
        "solve_input_hash": solve_input_hash(request),
    }

    try:
        output: SolveOutput = solve(request)
    except SolveInfeasible as verdict:
        payload["verdict"] = "infeasible"
        payload["infeasibility"] = canonical(verdict.infeasibility)
        return payload

    payload["verdict"] = "solved"
    frozen = canonical(output)
    assert isinstance(frozen, dict)
    frozen.pop("stage_timings_ms", None)
    payload["output"] = frozen
    return payload


def expected_path(case_id: str) -> Path:
    return EXPECTED_DIR / f"{case_id}.json"


def write_expected(case) -> Path:
    EXPECTED_DIR.mkdir(parents=True, exist_ok=True)
    path = expected_path(case.case_id)
    path.write_text(golden_json(run_case(case)), encoding="utf-8")
    return path


def read_expected(case_id: str) -> dict[str, object]:
    return json.loads(expected_path(case_id).read_text(encoding="utf-8"))
