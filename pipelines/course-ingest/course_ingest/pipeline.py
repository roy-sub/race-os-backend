"""The ten-stage pipeline, end to end.

    generate(spec) -> BuildResult

Determinism is a property of this module as much as of any single stage: no
randomness, no clock in the numeric path, every collection iterated in an
explicit order, and every float rounded once at emit through a fixed-precision
formatter. `tests/test_determinism.py` runs the whole thing twice and diffs the
bytes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bundle import LegData, assemble, build_attribution
from .config import Config, load_config
from .geo import cumulative_m, elevation_gain, path_length_m
from .sources.base import DemSource, RoadSource
from .sources.cache import BlobCache
from .sources.overture import OvertureRoadSource
from .sources.terrarium import TerrariumDemSource
from .spec import CourseSpec
from .stages.s01_ingest import BuildPlan, ingest
from .graph import RoutingError
from .stages.s02_route import (
    assemble_leg,
    build_graph,
    build_loop,
    route_leg,
    ways_carrying_bad_gradients,
)
from .stages.s03_swim import draw_swim
from .stages.s04_clean import clean_leg
from .stages.s05_mapmatch import _EdgeIndex, map_match
from .stages.s06_resample import resample_leg
from .stages.s07_elevation import sample_leg
from .stages.s08_segments import segment_leg
from .stages.s09_furniture import build_furniture
from .stages.s10_emit import EmitResult, emit


@dataclass
class BuildResult:
    spec: CourseSpec
    plan: BuildPlan
    bundle: dict[str, Any]
    legs: dict[str, LegData]
    emit_result: EmitResult | None
    stage_seconds: dict[str, float]
    reports: dict[str, Any] = field(default_factory=dict)


def default_sources(cfg: Config, cache_dir: str | Path | None = None) -> tuple[RoadSource, DemSource]:
    root = Path(cache_dir) if cache_dir else Path(cfg["sources"]["cache"]["dir"])
    cache = BlobCache(root, enabled=bool(cfg["sources"]["cache"]["enabled"]))
    return OvertureRoadSource(cfg, cache), TerrariumDemSource(cfg, cache)


def _sampled_gradients(points, dem: DemSource, spacing: float = 10.0):
    from .geo import resample as _resample

    nodes = _resample(list(points), spacing)
    heights = dem.sample(nodes)
    cum = cumulative_m(nodes)
    grads = [
        0.0 if cum[i + 1] - cum[i] <= 0 else (heights[i + 1] - heights[i]) / (cum[i + 1] - cum[i])
        for i in range(len(heights) - 1)
    ]
    return cum, grads


def _bad_ways(routed, dem: DemSource, hard_max: float) -> frozenset[str]:
    """Ways whose terrain profile is impossible for a road.

    A road does not climb at 200%. Where the series says it does, the route has
    crossed something the DEM cannot see -- an unflagged viaduct, a cutting --
    and the honest response is to route elsewhere rather than invent a height.
    """
    cum, grads = _sampled_gradients(routed.points, dem)
    return ways_carrying_bad_gradients(routed.spans, cum, grads, hard_max)


def _outlier_ways(routed, dem: DemSource, outlier_max: float, fraction_limit: float):
    """Ways carrying steep-but-not-impossible nodes, when there are too many.

    Returns (ways, fraction). A DEM sampled every 10 m on a road cut into a
    hillside always produces a few nodes over the bound; only an unusual number
    of them is worth a second attempt.
    """
    cum, grads = _sampled_gradients(routed.points, dem)
    outliers = [i for i, g in enumerate(grads) if abs(g) > outlier_max]
    fraction = len(outliers) / max(1, len(grads))
    if fraction <= fraction_limit:
        return frozenset(), fraction
    return (
        ways_carrying_bad_gradients(routed.spans, cum, grads, outlier_max),
        fraction,
    )


def generate(
    spec: CourseSpec,
    out_dir: str | Path,
    cfg: Config | None = None,
    roads: RoadSource | None = None,
    dem: DemSource | None = None,
    dry_run: bool = False,
    log=lambda msg: None,
) -> BuildResult:
    cfg = cfg or load_config()
    if roads is None or dem is None:
        roads, dem = default_sources(cfg)

    timings: dict[str, float] = {}
    reports: dict[str, Any] = {}

    def timed(name: str, fn):
        t0 = time.perf_counter()
        value = fn()
        timings[name] = time.perf_counter() - t0
        log(f"  {name:<14} {timings[name]:6.1f}s")
        return value

    # --- 1. ingest -------------------------------------------------------
    plan = timed("01-ingest", lambda: ingest(spec, cfg))

    # --- 2. route bike and run, with stages 4-6 in the correction loop ------
    #
    # Cleaning and map-matching shorten a routed leg, so the length that
    # survives them is not the length that was routed. The delivered leg is
    # therefore measured and the spur re-cut until it lands inside tolerance --
    # a fixed number of passes, and only the spur is re-routed, never the loop.
    hard_max = float(cfg["course"]["validation"]["hard_max_node_gradient"])
    outlier_max = float(cfg["course"]["validation"]["max_abs_node_gradient"])
    outlier_fraction_limit = float(cfg["course"]["validation"]["outlier_reroute_fraction"])
    max_passes = int(cfg["routing"]["structures"]["max_reroute_passes"])
    correction_passes = int(cfg["routing"]["loop"]["length_correction_passes"])
    tolerance = cfg["course"]["distance_tolerance"]

    cleaned: dict[str, list] = {}
    nodes: dict[str, list] = {}

    def _finish_leg(leg: str, points, graph):
        """Stages 4-6 for one leg: clean, map-match, resample."""
        pts, clean_report = clean_leg(list(points), leg, cfg, close_loop=True)
        pts, match_report = map_match(pts, graph, leg, cfg, _EdgeIndex(graph))
        node_list, resample_report = resample_leg(pts, leg, cfg)
        return pts, node_list, (clean_report, match_report, resample_report)

    def _route(leg: str, bbox, target_m, laps, character, bearing):
        graph = build_graph(roads, dem, bbox, cfg, leg)
        log(
            f"     {leg.lower()} graph: {len(graph.node_key)} nodes, {len(graph.edges)} edges "
            f"(excluded {graph.excluded_structure_ways} tunnel/covered ways, "
            f"{graph.excluded_bridge_edges} long-bridge spans)"
        )

        banned: frozenset[str] = frozenset()
        outlier_pass_used = False
        router = transition = loop = None
        accepted: tuple | None = None
        for attempt in range(max_passes + 1):
            try:
                router, transition, loop = build_loop(
                    plan, cfg, graph, leg, target_m, laps, character, bearing, banned
                )
            except RoutingError as exc:
                # Banning ways can cut a sparse rural network in two. When it
                # does, keep the last route that worked and accept its outliers
                # rather than failing the course over DEM noise.
                if accepted is None:
                    raise
                log(
                    f"     {leg.lower()}: excluding {len(banned)} ways leaves no route "
                    f"({exc}); keeping the previous route and accepting its steep nodes"
                )
                router, transition, loop, banned = accepted
                break
            accepted = (router, transition, loop, banned)
            probe = assemble_leg(router, graph, leg, loop, transition, target_m, laps, banned)
            bad = _bad_ways(probe, dem, hard_max)
            extra = frozenset()
            if not bad and not outlier_pass_used:
                extra, fraction = _outlier_ways(probe, dem, outlier_max, outlier_fraction_limit)
                if extra:
                    outlier_pass_used = True
                    log(
                        f"     {leg.lower()}: {fraction:.2%} of nodes over {outlier_max:.0%} "
                        f"(trigger {outlier_fraction_limit:.1%}); one re-route without "
                        f"{len(extra)} ways, then accept"
                    )
            if not (bad or extra) or attempt == max_passes:
                if bad:
                    log(
                        f"     {leg.lower()}: {len(bad)} ways still exceed the hard gradient "
                        "bound after re-routing; the bundle will be rejected"
                    )
                break
            if bad:
                log(
                    f"     {leg.lower()}: {len(bad)} ways cross terrain the DEM cannot see; "
                    f"re-routing without them (pass {attempt + 2})"
                )
            banned = banned | bad | extra

        # Length correction: re-cut the spur until the delivered leg is in
        # tolerance. Deterministic, fixed pass count, no loop re-search.
        tol_m = target_m * float(tolerance[leg])
        extra_spur = 0.0
        best = None
        for correction in range(correction_passes):
            routed = assemble_leg(
                router, graph, leg, loop, transition, target_m, laps, banned, extra_spur
            )
            pts, node_list, reps = _finish_leg(leg, routed.points, graph)
            delivered = cumulative_m(node_list)[-1]
            error = delivered - target_m
            if best is None or abs(error) < abs(best[0]):
                best = (error, routed, pts, node_list, reps)
            if abs(error) <= tol_m:
                break
            if correction < correction_passes - 1:
                # The spur is traversed once per lap, so a per-lap correction of
                # deficit/laps closes the whole gap.
                extra_spur += -error / laps
                log(
                    f"     {leg.lower()}: delivered {delivered/1000:.3f} km vs "
                    f"{target_m/1000:.3f} km ({error:+.0f} m); re-cutting the spur"
                )
        error, routed, pts, node_list, reps = best
        cleaned[leg] = pts
        nodes[leg] = node_list
        reports.setdefault("clean", {})[leg] = reps[0]
        reports.setdefault("map_match", {})[leg] = reps[1]
        reports.setdefault("resample", {})[leg] = reps[2]
        log(f"     {leg.lower()}: delivered {cumulative_m(node_list)[-1]/1000:.3f} km ({error:+.0f} m)")
        return routed

    bike = timed(
        "02-route-bike",
        lambda: _route("BIKE", plan.bike_bbox, plan.bike_target_m, spec.bike.laps,
                       plan.bike_character, spec.bike.bearing_offset_deg),
    )
    run = timed(
        "02-route-run",
        lambda: _route("RUN", plan.run_bbox, plan.run_target_m, spec.run.laps,
                       plan.run_character, spec.run.bearing_offset_deg),
    )

    # --- 3. draw the swim ------------------------------------------------
    swim = timed("03-swim", lambda: draw_swim(plan, cfg, roads))

    # --- 4/5/6. clean and resample the swim -------------------------------
    # Bike and run went through stages 4-6 inside the correction loop above;
    # the swim has no road to match against, so it only cleans and resamples.
    def _finish_swim():
        pts, clean_report = clean_leg(list(swim.points), "SWIM", cfg, close_loop=True)
        cleaned["SWIM"] = pts
        reports.setdefault("clean", {})["SWIM"] = clean_report
        node_list, resample_report = resample_leg(pts, "SWIM", cfg)
        nodes["SWIM"] = node_list
        reports.setdefault("resample", {})["SWIM"] = resample_report
    timed("04-06-swim-clean", _finish_swim)

    # --- 7. sample the DEM ----------------------------------------------
    heights: dict[str, list] = {}

    def _elevation():
        for leg in ("SWIM", "BIKE", "RUN"):
            hs, rep = sample_leg(nodes[leg], leg, dem, cfg, water_surface=(leg == "SWIM"))
            heights[leg] = hs
            reports.setdefault("elevation", {})[leg] = rep
    timed("07-elevation", _elevation)

    # --- 8. gradients, climbs, named segments ---------------------------
    legs: dict[str, LegData] = {}

    def _segments():
        span_by_leg = {"SWIM": (), "BIKE": bike.spans, "RUN": run.spans}
        laps_by_leg = {"SWIM": swim.laps, "BIKE": bike.laps, "RUN": run.laps}
        for leg in ("SWIM", "BIKE", "RUN"):
            pts, hs = nodes[leg], heights[leg]
            cum = cumulative_m(pts)
            threshold = float(cfg["course"]["elevation"]["gain_threshold_m"])
            gain = elevation_gain(hs, threshold)
            raw_gain = elevation_gain(hs, 0.0)
            loss = sum(max(0.0, hs[i] - hs[i + 1]) for i in range(len(hs) - 1))
            if leg == "SWIM":
                from .stages.s08_segments import node_gradients

                legs[leg] = LegData(
                    leg=leg, nodes=pts, heights=hs,
                    gradients=node_gradients(cum, hs),
                    surface_quality="typical_road",
                    length_m=cum[-1], gain_m=gain, gain_m_raw_nodes=raw_gain,
                    loss_m=loss, laps=laps_by_leg[leg],
                    segments=[],
                )
                continue
            result = segment_leg(pts, hs, span_by_leg[leg], leg, cfg)
            legs[leg] = LegData(
                leg=leg, nodes=pts, heights=hs,
                gradients=list(result.node_gradients),
                surface_quality=result.leg_surface_quality,
                length_m=cum[-1], gain_m=gain, gain_m_raw_nodes=raw_gain,
                loss_m=loss, laps=laps_by_leg[leg],
                segments=list(result.segments),
            )
    timed("08-segments", _segments)

    # --- 9. aid stations, transitions, special needs, markers, cut-offs --
    leg_km = {leg: legs[leg].length_m / 1000.0 for leg in ("SWIM", "BIKE", "RUN")}
    furniture = timed("09-furniture", lambda: build_furniture(spec, cfg, leg_km))

    # --- 10. validate and emit ------------------------------------------
    datasets: list[tuple[str, str]] = []
    for routed in (bike, run):
        used = {s.way_id for s in routed.spans}
        for way_id in sorted(used):
            datasets.extend(routed.graph.way_sources.get(way_id, ()))
    attribution = build_attribution(datasets, dem.attribution(), cfg)

    char_cfg = cfg["routing"]["character"]
    provenance_detail = {
        "road_source": roads.snapshot_id,
        "dem_source": dem.snapshot_id,
        "dem_sample_zoom": getattr(dem, "sample_zoom", None),
        "road_datasets": sorted({d for d in datasets}),
        "swim": {
            "shape": swim.shape,
            "laps": swim.laps,
            "lap_perimeter_m": round(swim.lap_perimeter_m, 2),
            "water_body": swim.water_name,
            "water_subtype": swim.water_subtype,
            "bearing_deg": round(swim.bearing_deg, 2),
            "elevation_note": "water surface held level at the DEM median over the course",
        },
        "character": {
            "BIKE": {"character": plan.bike_character, **char_cfg[plan.bike_character]},
            "RUN": {"character": plan.run_character, **char_cfg[plan.run_character]},
        },
        "routing": {
            "BIKE": {
                "laps": bike.laps,
                "ring_radius_m": round(bike.ring_radius_m, 1),
                "out_and_back_spur_m": round(bike.spur_m, 1),
                "ways_used": len({s.way_id for s in bike.spans}),
            },
            "RUN": {
                "laps": run.laps,
                "ring_radius_m": round(run.ring_radius_m, 1),
                "out_and_back_spur_m": round(run.spur_m, 1),
                "ways_used": len({s.way_id for s in run.spans}),
            },
        },
    }

    bundle = assemble(
        spec, cfg, legs, furniture, attribution,
        elevation_source=dem.elevation_source,
        provenance_detail=provenance_detail,
    )
    result = timed(
        "10-emit",
        lambda: emit(bundle, legs, cfg, out_dir, spec.slug, dry_run=dry_run),
    )

    return BuildResult(
        spec=spec, plan=plan, bundle=bundle, legs=legs,
        emit_result=result, stage_seconds=timings, reports=reports,
    )
