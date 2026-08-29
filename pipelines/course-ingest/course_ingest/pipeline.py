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
from .stages.s02_route import build_graph, route_leg, ways_carrying_bad_gradients
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


def _bad_ways(routed, dem: DemSource, hard_max: float) -> frozenset[str]:
    """Sample the routed line at node resolution and find the ways whose terrain
    profile is impossible for a road."""
    from .geo import resample as _resample

    nodes = _resample(list(routed.points), 10.0)
    heights = dem.sample(nodes)
    cum = cumulative_m(nodes)
    grads = [
        0.0 if cum[i + 1] - cum[i] <= 0 else (heights[i + 1] - heights[i]) / (cum[i + 1] - cum[i])
        for i in range(len(heights) - 1)
    ]
    return ways_carrying_bad_gradients(routed.spans, cum, grads, hard_max)


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

    # --- 2. route bike and run ------------------------------------------
    hard_max = float(cfg["course"]["validation"]["hard_max_node_gradient"])
    max_passes = int(cfg["routing"]["structures"]["max_reroute_passes"])

    def _route(leg: str, bbox, target_m, laps, character, bearing):
        graph = build_graph(roads, dem, bbox, cfg, leg)
        log(
            f"     {leg.lower()} graph: {len(graph.node_key)} nodes, {len(graph.edges)} edges "
            f"(excluded {graph.excluded_structure_ways} tunnel/covered ways, "
            f"{graph.excluded_bridge_edges} long-bridge spans)"
        )
        banned: frozenset[str] = frozenset()
        routed = None
        for attempt in range(max_passes + 1):
            routed = route_leg(
                plan, cfg, graph, leg, target_m, laps, character, bearing, banned_ways=banned
            )
            bad = _bad_ways(routed, dem, hard_max)
            if not bad or attempt == max_passes:
                if bad:
                    log(
                        f"     {leg.lower()}: {len(bad)} ways still exceed the hard gradient "
                        "bound after re-routing; the bundle will be rejected"
                    )
                break
            banned = banned | bad
            log(
                f"     {leg.lower()}: {len(bad)} ways cross terrain the DEM cannot see; "
                f"re-routing without them (pass {attempt + 2})"
            )
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

    # --- 4. clean --------------------------------------------------------
    raw = {"SWIM": list(swim.points), "BIKE": list(bike.points), "RUN": list(run.points)}
    cleaned: dict[str, list] = {}

    def _clean():
        for leg, pts in raw.items():
            pts2, rep = clean_leg(pts, leg, cfg, close_loop=True)
            cleaned[leg] = pts2
            reports.setdefault("clean", {})[leg] = rep
    timed("04-clean", _clean)

    # --- 5. map-match bike and run --------------------------------------
    def _match():
        for leg, routed in (("BIKE", bike), ("RUN", run)):
            index = _EdgeIndex(routed.graph)
            pts, rep = map_match(cleaned[leg], routed.graph, leg, cfg, index)
            cleaned[leg] = pts
            reports.setdefault("map_match", {})[leg] = rep
    timed("05-mapmatch", _match)

    # --- 6. resample to ~10 m -------------------------------------------
    nodes: dict[str, list] = {}

    def _resample():
        for leg in ("SWIM", "BIKE", "RUN"):
            pts, rep = resample_leg(cleaned[leg], leg, cfg)
            nodes[leg] = pts
            reports.setdefault("resample", {})[leg] = rep
    timed("06-resample", _resample)

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
