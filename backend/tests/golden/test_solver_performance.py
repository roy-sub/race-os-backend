"""Performance, measured against the **real** Tramuntana bundle.

This is deliberately not a synthetic course. ``SOLVER_MODEL.md`` §0.8 measured
its budgets against an ~1,800-node bike leg; the actual generated bundle has
**18,001 nodes**, ten times that. The golden fixtures cannot expose a
regression here at all, because they are built with a constant gradient inside
each segment — their histograms collapse to a single bin, so a revert to
per-node solving would cost them nothing and pass unnoticed.

The real bundle is what makes the gradient histogram non-optional rather than
an optimisation:

* 18,000 node pairs reduce to ~1,800 histogram bins, a 10x reduction
* §0.8 measured per-node solving at 2,360 ms against a 1,100 ms Stage 3
  budget — **on a leg a tenth this size**

So this test asserts both the per-stage budgets and the hard 6 s SLA, and it
asserts the reduction directly, because a change that quietly stopped binning
would still produce correct numbers — just slowly enough to breach the SLA in
production and nowhere else.
"""

from __future__ import annotations

import time
from datetime import date
from datetime import time as clock_time
from pathlib import Path

import pytest

from raceos.domain.enums import Leg
from raceos.solver.adapters import load_pipeline_bundle
from raceos.solver.environment import wbgt
from raceos.solver.models import (
    SCHEMA_VERSION,
    EventSpec,
    GoalSpec,
    SolveInput,
    SolveOptions,
)
from raceos.solver.pipeline import solve
from raceos.solver.stages.s1_course import load_course
from raceos.solver.stages.s2_athlete import read_athlete
from raceos.solver.stages.s3_barriers import evaluate_barriers
from raceos.solver.tables import intensity as intensity_tbl
from tests.golden.cases import A_M, F_MILD

pytestmark = [pytest.mark.perf, pytest.mark.golden]

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"
TRAMUNTANA = BUNDLE_DIR / "tramuntana-full.bundle.json"

#: The contract's hard SLA and per-stage targets (Build Spec Part 5.4, §0.8).
#: Generous multiples of the measured figures: this test exists to catch an
#: order-of-magnitude regression such as a revert to per-node solving, not to
#: police a 20% drift on a shared CI runner.
SLA_MS = 6_000
STAGE3_BUDGET_MS = 1_100 * 4
WHOLE_SOLVE_BUDGET_MS = 3_100

needs_bundle = pytest.mark.skipif(
    not TRAMUNTANA.is_file(),
    reason="generated bundles are git-ignored build artefacts; none in this checkout",
)


def _request() -> SolveInput:
    """Athlete M on the real Tramuntana bundle, at its own coordinates."""
    return SolveInput(
        schema_version=SCHEMA_VERSION,
        athlete=A_M,
        course=load_pipeline_bundle(TRAMUNTANA),
        goal=GoalSpec(),
        forecast=F_MILD,
        event=EventSpec(
            event_date=date(2026, 9, 19),
            start_time_local=clock_time(7, 0),
            timezone="Europe/Madrid",
            lat=39.8402,
            lng=3.121,
            utc_offset_hours=2.0,
        ),
        options=SolveOptions(),
    )


@needs_bundle
def test_the_real_bundle_is_an_order_of_magnitude_larger_than_the_fixtures() -> None:
    """The premise of this whole file, asserted rather than assumed."""
    bundle = load_pipeline_bundle(TRAMUNTANA)
    bike_nodes = len(bundle.leg(Leg.BIKE).nodes)
    assert bike_nodes > 15_000, (
        f"the real bike leg has {bike_nodes} nodes; §0.8's measurements assumed "
        f"~1,800, so the budgets below are being checked against the harder case"
    )


@needs_bundle
def test_the_gradient_histogram_reduces_the_work_by_an_order_of_magnitude() -> None:
    """§1.1's quadrature, measured on real terrain.

    A change that stopped binning would still produce correct numbers — it
    would just do ten times the work, and breach the SLA in production and
    nowhere else. This is what catches it.
    """
    geometry = load_course(load_pipeline_bundle(TRAMUNTANA))
    bike = geometry.leg(Leg.BIKE)

    node_pairs = sum(segment.counted_nodes for segment in bike.segments)
    bins = sum(len(segment.histogram) for segment in bike.segments)

    assert node_pairs > 15_000
    assert bins * 8 < node_pairs, (
        f"{node_pairs} node pairs reduced to only {bins} bins; the histogram is "
        f"meant to cut the speed solves by roughly an order of magnitude"
    )

    # And no distance is lost: binning is exact in distance, only gradient
    # resolution is quantised.
    for segment in bike.segments:
        binned = sum(distance for _, distance in segment.histogram)
        assert binned == pytest.approx(segment.distance_m, rel=1e-9)


@needs_bundle
def test_stage_3_grid_is_inside_its_budget_on_the_real_bundle() -> None:
    """The stage §0.8 identifies as the one that does not fit if done naively."""
    bundle = load_pipeline_bundle(TRAMUNTANA)
    geometry = load_course(bundle)
    athlete, _ = read_athlete(A_M)
    heat = wbgt(F_MILD.temp_c, F_MILD.humidity, F_MILD.conditions, F_MILD.cloud_cover_pct)
    reference = intensity_tbl.IF_REF[bundle.distance][athlete.level]

    started = time.perf_counter()
    evaluate_barriers(
        geometry,
        athlete,
        F_MILD,
        reference_if=reference,
        wbgt_c=heat,
        density_pressure_hpa=F_MILD.pressure_hpa,
        distance=bundle.distance,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert elapsed_ms < STAGE3_BUDGET_MS, (
        f"Stage 3 took {elapsed_ms:.0f} ms against a {STAGE3_BUDGET_MS} ms budget. "
        f"§0.8 measured per-node solving at 2,360 ms on a leg a tenth this size, "
        f"so this is the shape a revert to per-node solving would take."
    )


@needs_bundle
def test_a_whole_solve_is_well_inside_the_hard_sla() -> None:
    """The 6 s SLA, on the largest course this system actually carries."""
    request = _request()
    # One warm call so the measurement is of the solve, not of imports.
    solve(request)

    started = time.perf_counter()
    output = solve(request)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert output.projected_minutes > 0
    assert elapsed_ms < WHOLE_SOLVE_BUDGET_MS, (
        f"a full solve took {elapsed_ms:.0f} ms against the {WHOLE_SOLVE_BUDGET_MS} ms "
        f"P50 target and a {SLA_MS} ms hard SLA"
    )


@needs_bundle
def test_the_real_bundle_solves_and_stays_deterministic() -> None:
    """Determinism is a property of the solver, not of the fixture shape.

    The golden cases prove it on synthetic courses; this proves the same on
    18,000 nodes of real routed terrain, where float accumulation order has far
    more opportunity to matter.
    """
    request = _request()
    first = solve(request)
    for _ in range(3):
        assert solve(request) == first
