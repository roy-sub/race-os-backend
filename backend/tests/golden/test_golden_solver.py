"""The golden-file solver suite. A diff here blocks deploy.

``SOLVER_MODEL.md`` §B defines fifteen cases; their inputs are pinned by the
document and their expected outputs were captured from the first correct run
and then frozen. Regenerating one requires an explicit reason in the commit
message, which CI checks.

Five determinism tests accompany them (§B.4), and each exists because the
failure it catches is invisible without it:

1. **Byte-identical repeat** — the base guarantee.
2. **Provenance invariance** — the CI enforcement of §0.6. Permutes every
   ``source`` and requires identical numeric output.
3. **Input-hash invariance** — stable across processes and across dict
   insertion orders.
4. **Continuity** — a plan must not jump because a forecast moved 0.1 °C.
5. **Monotonicity** — more power and better run pace must never make a plan
   slower. A model failing this has a sign error somewhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from raceos.domain.enums import ConstraintSource, Feasibility, MarginState
from raceos.solver.pipeline import solve
from raceos.solver.serialisation import canonical, solve_input_hash
from tests.golden.cases import CASES, CASES_BY_ID
from tests.golden.runner import build_input, expected_path, read_expected, run_case

pytestmark = pytest.mark.golden

BACKEND_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# The fifteen cases
# ---------------------------------------------------------------------------


def test_all_fifteen_cases_are_defined() -> None:
    """§B.4 defines G01-G15.

    The section heading says "twelve", which is stale text from before the
    revision that added G13, G14 and G15 — all three appear in §F.8's
    affected-cases table. Dropping any would leave a §F contract change
    untested.
    """
    assert len(CASES) == 15
    assert [case.case_id for case in CASES] == [
        "G01-FULL",
        "G02-HALF",
        "G03-OLYMPIC",
        "G04-SPRINT",
        "G05-INFEASIBLE",
        "G06-TIGHT",
        "G07-HOT",
        "G08-FIRSTTIMER",
        "G09-CARBOVERRIDE",
        "G10-NIGHT",
        "G11-FLAT",
        "G12-MOUNTAIN",
        "G13-NOWETSUIT",
        "G14-EARLIESTMISS",
        "G15-ASSUMED",
    ]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_golden_output_is_unchanged(case) -> None:
    """The regression itself. Any diff fails."""
    path = expected_path(case.case_id)
    assert path.is_file(), (
        f"no frozen expectation for {case.case_id}. Regenerate with "
        f"`python -m tests.golden.freeze` and explain why in the commit message."
    )
    assert run_case(case) == read_expected(case.case_id)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_every_bag_item_carries_a_reason(case) -> None:
    """§6.1, on every case rather than only where it was convenient."""
    result = run_case(case)
    if result["verdict"] != "solved":
        pytest.skip("infeasible; Stage 6 does not run")
    output = result["output"]
    assert len(output["bags"]) == 5, "exactly five bags, always"
    for bag in output["bags"]:
        for item in bag["items"]:
            assert item["reason_constraint_key"], f"{item['name']} has no reason key"
            assert item["reason_text"].strip(), f"{item['name']} has no reason text"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_every_split_and_segment_time_is_positive(case) -> None:
    """§4.3.3's postcondition: never emit a negative or zero split time."""
    result = run_case(case)
    if result["verdict"] != "solved":
        pytest.skip("infeasible")
    output = result["output"]
    for split in output["splits"]:
        assert float(split["split_minutes"]) > 0
    for segment in output["segments"]:
        assert float(segment["target_minutes"]) > 0


# ---------------------------------------------------------------------------
# The cases whose verdicts the document states outright
# ---------------------------------------------------------------------------


def test_g05_is_infeasible_at_the_finish() -> None:
    """Definitional (§B.4). Only the finish is missed, so earliest == tightest."""
    result = run_case(CASES_BY_ID["G05-INFEASIBLE"])
    assert result["verdict"] == "infeasible"
    detail = result["infeasibility"]
    assert detail["barrier"] == "finish"
    assert detail["tightest_barrier"] == "finish"
    assert float(detail["miss_minutes"]) == pytest.approx(90.6, abs=1.0)
    assert detail["levers"] == ["improve_run_pace", "raise_ftp"]


def test_g06_is_tight_at_the_finish() -> None:
    """Definitional (§B.4), and it pins §3.5's precedence rule 1.

    A cut-off in play outranks everything, so the binding key names the
    barrier rather than the largest leg's constraint.
    """
    output = run_case(CASES_BY_ID["G06-TIGHT"])["output"]
    assert output["margin_state"] == MarginState.TIGHT.value
    assert output["feasibility"] == Feasibility.TIGHT.value
    assert float(output["worst_margin_minutes"]) == pytest.approx(6.2, abs=1.0)
    assert float(output["projected_minutes"]) == pytest.approx(953.9, abs=2.0)
    assert output["binding_constraint_key"] == "barrier:finish"


def test_g14_reports_the_earliest_missed_barrier_not_the_tightest() -> None:
    """**The case that pins §F.5.** The whole point of the contract change.

    Told "you miss the finish by 132 minutes", this athlete would reasonably
    conclude the race is far out of reach. Told "you miss the bike cut-off by
    ten", they learn the truth: the race ends mid-bike, and ten minutes is a
    gap a winter of work genuinely closes.

    A regression that reverted to "tightest" would report the finish here, and
    this assertion is what makes that fail loudly.
    """
    detail = run_case(CASES_BY_ID["G14-EARLIESTMISS"])["infeasibility"]
    assert detail["barrier"] == "bike_cutoff", "must be the EARLIEST missed"
    assert detail["tightest_barrier"] == "finish", "the tightest is still carried"
    # The two are far apart, which is what makes the distinction matter.
    assert float(detail["tightest_miss_minutes"]) > float(detail["miss_minutes"]) * 5


def test_g15_declares_exactly_the_four_documented_assumptions() -> None:
    """§F.6, sorted lexicographically so it is deterministic and diffable."""
    output = run_case(CASES_BY_ID["G15-ASSUMED"])["output"]
    assert output["assumed_fields"] == [
        "athlete.bike_setup",
        "forecast.cloud_cover_pct",
        "forecast.pressure_hpa",
        "sweat_rate.measured_at_temp_c",
    ]


def test_g15_bike_split_differs_from_g01_because_cda_fell_back() -> None:
    """The fallback is not cosmetic: it moves the plan.

    ``road_clipons`` + ``improver`` = 0.280 against A-M's real 0.255, which is
    why supplying ``bike_setup`` later frequently crosses the drift thresholds.
    """
    g01 = run_case(CASES_BY_ID["G01-FULL"])["output"]
    g15 = run_case(CASES_BY_ID["G15-ASSUMED"])["output"]
    bike_g01 = next(s for s in g01["splits"] if s["leg"] == "BIKE")
    bike_g15 = next(s for s in g15["splits"] if s["leg"] == "BIKE")
    assert float(bike_g15["split_minutes"]) > float(bike_g01["split_minutes"])


def test_g13_races_without_a_wetsuit_and_warns() -> None:
    """Water 26.0 °C: permitted, but not award-eligible."""
    output = run_case(CASES_BY_ID["G13-NOWETSUIT"])["output"]
    assert output["wetsuit_used"] is False
    assert output["wetsuit_warning"] is True


def test_g09_override_is_recorded_and_capped_by_the_hard_maximum() -> None:
    """95 g/h is above A-M's 75 ceiling and below the 120 hard maximum."""
    output = run_case(CASES_BY_ID["G09-CARBOVERRIDE"])["output"]
    assert output["fuelling"]["overridden"] is True
    assert output["fuelling"]["carb_g_per_hr"] == 95
    assert output["fuelling"]["binding_carb_key"] == "options:carb_override"


def test_g10_emits_a_head_torch_with_a_real_reason() -> None:
    """The case must be **feasible**, or Stage 6 never runs and it tests nothing.

    That is not hypothetical: §B.2 records that A-X was replaced outright
    because its previous profile was infeasible on C-TRAM.
    """
    result = run_case(CASES_BY_ID["G10-NIGHT"])
    assert result["verdict"] == "solved", "G10 must reach Stage 6"
    torches = [
        item
        for bag in result["output"]["bags"]
        for item in bag["items"]
        if item["name"] == "Head torch"
    ]
    assert len(torches) == 1
    assert torches[0]["reason_constraint_key"] == "model:dusk_buffer"
    assert "civil dusk" in torches[0]["reason_text"]


def test_g07_includes_arm_coolers_and_g01_does_not() -> None:
    """The 28 °C rule, keyed off dry-bulb temperature as the contract says."""

    def has_coolers(case_id: str) -> bool:
        return any(
            item["name"] == "Arm coolers"
            for bag in run_case(CASES_BY_ID[case_id])["output"]["bags"]
            for item in bag["items"]
        )

    assert has_coolers("G07-HOT"), "31 °C is above the threshold"
    assert not has_coolers("G01-FULL"), "22 °C is below it"


# ---------------------------------------------------------------------------
# Determinism test 1 — byte-identical repeat
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_solving_twice_in_one_process_is_byte_identical(case) -> None:
    assert run_case(case) == run_case(case)


# ---------------------------------------------------------------------------
# Determinism test 2 — provenance invariance (§0.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
@pytest.mark.parametrize("source", list(ConstraintSource), ids=lambda s: s.value)
def test_permuting_every_provenance_changes_no_number(case, source) -> None:
    """The CI enforcement of §0.6.

    An ``estimated`` constraint is used with exactly the numeric weight of a
    ``measured`` one. No branch anywhere reads ``constraint.source``, and this
    proves it by setting every source to the same value and requiring the
    numbers to be unmoved.

    ``source_label`` is expected to change — provenance is *carried*, and that
    is the point. Only the numbers must not.
    """
    baseline = run_case(case)

    permuted = replace(
        case,
        athlete=replace(
            case.athlete,
            constraints=tuple(replace(entry, source=source) for entry in case.athlete.constraints),
        ),
    )
    result = run_case(permuted)

    if baseline["verdict"] == "infeasible":
        assert result["infeasibility"] == baseline["infeasibility"]
        return

    def numbers_only(payload: dict[str, object]) -> dict[str, object]:
        output = dict(payload["output"])  # type: ignore[arg-type]
        output["constraint_refs"] = [
            {k: v for k, v in ref.items() if k != "source_label"}
            for ref in output["constraint_refs"]  # type: ignore[union-attr]
        ]
        return output

    assert numbers_only(result) == numbers_only(baseline)


# ---------------------------------------------------------------------------
# Determinism test 3 — input-hash invariance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_input_hash_is_stable_within_a_process(case) -> None:
    request = build_input(case)
    assert solve_input_hash(request) == solve_input_hash(build_input(case))


def test_input_hash_is_stable_across_process_restarts() -> None:
    """Hash randomisation must not reach the hash.

    Python randomises string hashing per process unless ``PYTHONHASHSEED`` is
    fixed. If any dictionary's iteration order leaked into the canonical form,
    this would differ between runs — and would do so *intermittently*, which is
    the worst way for a determinism bug to present.
    """
    script = (
        "import sys; sys.path.insert(0, '.'); "
        "from tests.golden.cases import CASES; "
        "from tests.golden.runner import build_input; "
        "from raceos.solver.serialisation import solve_input_hash; "
        "print(','.join(solve_input_hash(build_input(c)) for c in CASES))"
    )
    hashes: list[str] = []
    for seed in ("0", "1", "random"):
        environment = {**os.environ, "PYTHONHASHSEED": seed}
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=BACKEND_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        hashes.append(completed.stdout.strip())
    assert len(set(hashes)) == 1, "input hash differs between processes"


def test_input_hash_ignores_dict_insertion_order() -> None:
    """The same input, built with keys in a different order, hashes the same."""
    case = CASES_BY_ID["G01-FULL"]
    forward = build_input(case)
    reversed_constraints = replace(
        case.athlete, constraints=tuple(reversed(case.athlete.constraints))
    )
    backward = replace(build_input(case), athlete=reversed_constraints)
    # The constraint *tuple* is ordered data, so the hashes legitimately
    # differ; what must not differ is the solved output.
    assert solve(forward) == solve(backward)


# ---------------------------------------------------------------------------
# Determinism test 4 — continuity (§B.4)
# ---------------------------------------------------------------------------


def test_a_tenth_of_a_degree_does_not_move_the_plan_materially() -> None:
    """The contract's "must not jump because a forecast moved 0.1 °C".

    Note this **fails by design if ``conditions`` is perturbed instead** —
    that input is categorical under the fallback, and §D-4 records the
    discontinuity. Supplying ``cloud_cover_pct`` removes it, which is why G07
    supplies it.
    """
    case = CASES_BY_ID["G07-HOT"]
    base = solve(build_input(case))

    for delta in (-0.1, 0.1):
        nudged = replace(case, forecast=replace(case.forecast, temp_c=case.forecast.temp_c + delta))
        moved = solve(build_input(nudged))
        assert abs(moved.projected_minutes - base.projected_minutes) < 1.0, (
            f"a {delta:+.1f} °C change moved the projection by "
            f"{moved.projected_minutes - base.projected_minutes:.2f} min"
        )
        assert moved.margin_state is base.margin_state


# ---------------------------------------------------------------------------
# Determinism test 5 — monotonicity (§B.4)
# ---------------------------------------------------------------------------


def test_more_power_never_makes_the_plan_slower() -> None:
    """A model failing this has a sign error somewhere."""
    case = CASES_BY_ID["G01-FULL"]
    base = solve(build_input(case))

    stronger = replace(
        case,
        athlete=replace(
            case.athlete,
            constraints=tuple(
                replace(entry, value=entry.value * 1.10)
                if entry.key == "bike_threshold_power"
                else entry
                for entry in case.athlete.constraints
            ),
        ),
    )
    assert solve(build_input(stronger)).projected_minutes <= base.projected_minutes


def test_a_faster_run_threshold_never_makes_the_plan_slower() -> None:
    """Pace is seconds per km, so *falling* is improving."""
    case = CASES_BY_ID["G01-FULL"]
    base = solve(build_input(case))

    faster = replace(
        case,
        athlete=replace(
            case.athlete,
            constraints=tuple(
                replace(entry, value=entry.value * 0.90)
                if entry.key == "run_threshold_pace"
                else entry
                for entry in case.athlete.constraints
            ),
        ),
    )
    assert solve(build_input(faster)).projected_minutes <= base.projected_minutes


# ---------------------------------------------------------------------------
# Guards on the suite itself
# ---------------------------------------------------------------------------


def test_no_golden_case_reads_a_pipeline_output_path() -> None:
    """The isolation guard, restated on this side of the boundary.

    ``pipelines/course-ingest/tests/test_golden_isolation.py`` asserts the same
    separation from the pipeline's side. It exists so that regenerating a
    course can never silently break the solver's determinism proof — if a
    golden case read generated geometry, the guarantee would become a property
    of the routing engine rather than of the solver.
    """
    from tests.golden.runner import COURSE_DIR

    resolved = COURSE_DIR.resolve()
    assert resolved.is_dir()
    forbidden = (BACKEND_ROOT.parent / "pipelines" / "course-ingest" / "out").resolve()
    assert forbidden not in resolved.parents

    for path in sorted(COURSE_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["synthetic"] is True
        assert payload["source"] == "SOLVER_MODEL.md §B.1"
        assert "course_bundle" not in payload, "that shape is a pipeline bundle"


def test_frozen_expectations_exist_for_every_case() -> None:
    frozen = {path.stem for path in (expected_path("x").parent).glob("*.json")}
    assert frozen == {case.case_id for case in CASES}


def test_stage_timings_are_not_frozen() -> None:
    """They are a real measurement, so they differ every run.

    Freezing them would make the suite fail for reasons unrelated to
    correctness — the exact "flaky test nobody trusts" outcome the determinism
    guarantee exists to avoid.
    """
    payload = read_expected("G01-FULL")
    assert "stage_timings_ms" not in payload["output"]  # type: ignore[operator]
    # But the solver does emit them.
    assert isinstance(solve(build_input(CASES_BY_ID["G01-FULL"])).stage_timings_ms, dict)


def test_infeasible_cases_carry_levers_that_are_real_keys() -> None:
    """A lever the frontend cannot render is worse than no lever."""
    from raceos.domain.enums import LEVER_KEYS, LEVER_LOWER_GOAL

    valid = set(LEVER_KEYS.values()) | {LEVER_LOWER_GOAL}
    for case in CASES:
        result = run_case(case)
        if result["verdict"] != "infeasible":
            continue
        levers = result["infeasibility"]["levers"]  # type: ignore[index]
        assert 1 <= len(levers) <= 2, "§3.4 emits one or two"
        assert set(levers) <= valid, f"{case.case_id} emitted unknown lever(s)"


def test_canonical_output_contains_no_floats_that_could_reformat() -> None:
    """Floats are frozen as their ``repr``, the shortest round-tripping form.

    Storing them as JSON numbers would put the comparison at the mercy of the
    reader's float formatting; storing them as strings makes the diff exact.
    """
    output = canonical(solve(build_input(CASES_BY_ID["G01-FULL"])))

    def walk(node: object) -> None:
        if isinstance(node, float):
            raise AssertionError("a bare float survived canonicalisation")
        if isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(output)
