"""Stage 2 -- route the bike and run legs along real ways.

The route is built on a graph of actual OpenStreetMap-derived road spans, so
every metre of the bike and run legs lies on a way that exists. The only drawn
geometry in the whole bundle is the swim (Stage 3), because there are no roads
in water.

Terrain character is not decoration. A course declared mountainous is routed
with a cost function that makes climbing cheap and with waypoints drawn from the
top of the local elevation distribution; a flat course inverts both. Stage 10
then checks the delivered elevation gain against the declared character and
rejects the bundle if they disagree, so the character claim is enforced rather
than asserted.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ..config import Config
from ..geo import Point, path_length_m
from ..graph import LoopRouter, RoadGraph, RoutingError
from ..sources.base import DemSource, RoadSource
from .s01_ingest import BuildPlan


@dataclass(frozen=True)
class WaySpan:
    """The stretch of a leg carried by one OpenStreetMap way, in fractions of
    the leg's length. Stage 8 reads these to name segments and to resolve
    `surface_quality`."""

    from_fraction: float
    to_fraction: float
    edge_index: int
    way_id: str
    name: str | None
    surface: str | None
    road_class: str


@dataclass(frozen=True)
class RoutedLegResult:
    leg: str
    banned_ways: frozenset[str]
    points: tuple[Point, ...]
    lap_points: tuple[Point, ...]
    laps: int
    graph: RoadGraph
    steps: tuple[tuple[int, int, int], ...]
    transition_node: int
    length_m: float
    spur_m: float
    ring_radius_m: float
    spans: tuple[WaySpan, ...]


def _spans_from_owners(graph: RoadGraph, points, owners) -> tuple[WaySpan, ...]:
    """Collapse a per-point edge index into contiguous fractional spans."""
    from ..geo import cumulative_m

    if not points or len(points) != len(owners):
        return ()
    cum = cumulative_m(points)
    total = cum[-1]
    if total <= 0.0:
        return ()
    spans: list[WaySpan] = []
    start_i = 0
    for i in range(1, len(owners) + 1):
        if i == len(owners) or owners[i] != owners[start_i]:
            edge = graph.edges[owners[start_i]]
            spans.append(
                WaySpan(
                    from_fraction=cum[start_i] / total,
                    to_fraction=cum[min(i, len(cum) - 1)] / total,
                    edge_index=owners[start_i],
                    way_id=edge.way_id,
                    name=edge.name,
                    surface=edge.surface,
                    road_class=edge.road_class,
                )
            )
            start_i = i
    return tuple(spans)


def build_graph(
    roads: RoadSource,
    dem: DemSource,
    bbox: tuple[float, float, float, float],
    cfg: Config,
    leg: str,
) -> RoadGraph:
    ways = roads.ways_in_bbox(bbox)
    if not ways:
        raise RoutingError(f"no ways returned for bbox {bbox}")
    graph = RoadGraph.from_ways(
        ways,
        cfg["routing"]["class_cost"][leg.lower()],
        structures=cfg["routing"]["structures"],
    )
    if not graph.node_key:
        raise RoutingError(
            f"bbox {bbox} contains ways but none routable for the {leg} leg; "
            "check routing.class_cost"
        )
    graph.attach_elevation(dem.sample(graph.node_xy))
    return graph


def route_leg(
    plan: BuildPlan,
    cfg: Config,
    graph: RoadGraph,
    leg: str,
    target_m: float,
    laps: int,
    character: str,
    bearing_offset_deg: float,
    banned_ways: frozenset[str] = frozenset(),
) -> RoutedLegResult:
    """Route one leg: pick the loop, then assemble it with a distance-making spur."""
    router, transition, routed = build_loop(
        plan, cfg, graph, leg, target_m, laps, character, bearing_offset_deg, banned_ways
    )
    return assemble_leg(router, graph, leg, routed, transition, target_m, laps, banned_ways)


def build_loop(
    plan: BuildPlan,
    cfg: Config,
    graph: RoadGraph,
    leg: str,
    target_m: float,
    laps: int,
    character: str,
    bearing_offset_deg: float,
    banned_ways: frozenset[str] = frozenset(),
):
    """The expensive half: the radius scan and bisection that choose the loop.

    Separated from `assemble_leg` so the length-correction passes can re-cut the
    spur -- one Dijkstra -- without repeating the search.
    """
    lap_target = target_m / laps
    router = LoopRouter(graph, cfg, leg, character)
    router.banned_ways = banned_ways

    component_seed = graph.nearest_node(plan.start)
    component = graph.component_of(component_seed, banned_ways)
    if len(component) < 32:
        # The nearest node is on an island of the network; take the nearest node
        # of the largest component instead.
        best: set[int] = set()
        seen: set[int] = set()
        for n in range(len(graph.node_key)):
            if n in seen:
                continue
            comp = graph.component_of(n, banned_ways)
            seen |= comp
            if len(comp) > len(best):
                best = comp
        component = best
    transition = graph.nearest_node(plan.start, allowed=component)

    routed = router.route(
        transition, lap_target, plan.waypoint_count, math.radians(bearing_offset_deg)
    )
    return router, transition, routed


def assemble_leg(
    router: LoopRouter,
    graph: RoadGraph,
    leg: str,
    routed,
    transition: int,
    target_m: float,
    laps: int,
    banned_ways: frozenset[str] = frozenset(),
    extra_spur_m: float = 0.0,
) -> RoutedLegResult:
    """Attach the out-and-back spur and repeat the lap.

    `extra_spur_m` is the correction the caller applies once it knows how much
    length the downstream cleaning and map-matching stages take off.
    """
    lap_target = target_m / laps
    shortfall = lap_target - routed.length_m + extra_spur_m
    spur: list[Point] = []
    spur_owners: list[int] = []
    if shortfall > 1.0:
        spur, spur_owners = router.out_and_back(transition, shortfall / 2.0)

    if spur:
        # The spur runs out from the transition and back to it, then the loop
        # departs. Drop the duplicated junction point where they meet.
        lap_points = list(spur[:-1]) + list(routed.points)
        lap_owners = list(spur_owners[:-1]) + list(routed.edge_owners)
    else:
        lap_points = list(routed.points)
        lap_owners = list(routed.edge_owners)

    points: list[Point] = []
    owners: list[int] = []
    for lap in range(laps):
        if lap == 0:
            points.extend(lap_points)
            owners.extend(lap_owners)
        else:
            points.extend(lap_points[1:])
            owners.extend(lap_owners[1:])

    return RoutedLegResult(
        leg=leg,
        banned_ways=banned_ways,
        points=tuple(points),
        lap_points=tuple(lap_points),
        laps=laps,
        graph=graph,
        steps=routed.steps,
        transition_node=transition,
        length_m=path_length_m(points),
        spur_m=path_length_m(spur) if spur else 0.0,
        ring_radius_m=routed.ring_radius_m,
        spans=_spans_from_owners(graph, points, owners),
    )


def ways_carrying_bad_gradients(
    spans: Sequence[WaySpan],
    node_distances: Sequence[float],
    gradients: Sequence[float],
    hard_max: float,
) -> frozenset[str]:
    """Ways whose nodes exceed the hard gradient bound.

    A road does not go up at 200%. Where the terrain series says it does, the
    route has crossed something the DEM cannot see -- most often an unflagged
    viaduct or a cutting -- and the honest response is to route elsewhere rather
    than to invent a plausible height. The caller re-routes with these banned.
    """
    if not spans or not node_distances:
        return frozenset()
    total = node_distances[-1]
    if total <= 0.0:
        return frozenset()
    bad: set[str] = set()
    for i, g in enumerate(gradients):
        if abs(g) <= hard_max:
            continue
        frac = node_distances[i] / total
        for span in spans:
            if span.from_fraction <= frac <= span.to_fraction:
                bad.add(span.way_id)
                break
    return frozenset(bad)
