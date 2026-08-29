"""Routing graph built from real road ways, and the loop router over it.

Design notes
------------
*Topology comes from the source, not from geometry.* Overture ways carry
`connectors`; two ways sharing a connector id meet there. Splitting each way at
its connectors gives a graph whose nodes are real junctions, with no
coordinate-rounding heuristics deciding what touches what.

*Roads are treated as bidirectional.* Triathlon bike and run legs are raced on
closed roads, so one-way restrictions that apply to ordinary traffic do not
apply to the race. Modelling them would produce detours a race would never take.

*Nothing here is random.* Candidate sets are sorted, ties break on explicit
total-order keys, and the distance search is a fixed-length scan followed by a
fixed number of bisection steps -- never a tolerance-terminated loop.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from .geo import (
    Point,
    cumulative_m,
    haversine_m,
    path_length_m,
    slice_fraction,
)
from .sources.base import RoadWay


class RoutingError(RuntimeError):
    """No plausible route of the required distance and character exists."""


@dataclass(frozen=True)
class Edge:
    __slots__ = (
        "u", "v", "geometry", "length_m", "road_class", "name", "surface",
        "way_id", "key", "structure_cost",
    )
    u: int
    v: int
    geometry: tuple[Point, ...]
    length_m: float
    road_class: str
    name: str | None
    surface: str | None
    way_id: str
    key: str
    structure_cost: float


class RoadGraph:
    """An undirected graph of road spans, with per-node terrain elevation."""

    def __init__(self) -> None:
        self.node_id: dict[str, int] = {}
        self.node_key: list[str] = []
        self.node_xy: list[Point] = []
        self.node_h: list[float] = []
        self.edges: list[Edge] = []
        self.adj: list[list[tuple[int, int]]] = []  # node -> [(edge_index, other_node)]
        #: way_id -> ((dataset, license), ...). Read back for the bundle's
        #: attribution string, which is assembled from the ways actually used
        #: rather than hardcoded.
        self.way_sources: dict[str, tuple[tuple[str, str], ...]] = {}
        self.excluded_structure_ways = 0
        self.excluded_bridge_edges = 0

    # ------------------------------------------------------------------ build

    def _node(self, key: str, xy: Point) -> int:
        idx = self.node_id.get(key)
        if idx is None:
            idx = len(self.node_key)
            self.node_id[key] = idx
            self.node_key.append(key)
            self.node_xy.append(xy)
            self.adj.append([])
        return idx

    @classmethod
    def from_ways(
        cls,
        ways: Iterable[RoadWay],
        class_cost: dict[str, float | None],
        structures: dict | None = None,
        min_edge_m: float = 1.0,
    ) -> "RoadGraph":
        structures = structures or {}
        excluded = set(structures.get("excluded_flags", ()))
        bridge_multiplier = float(structures.get("bridge_cost_multiplier", 1.0))
        max_bridge = float(structures.get("max_bridge_length_m", 1e12))

        g = cls()
        g.excluded_structure_ways = 0
        g.excluded_bridge_edges = 0
        for way in sorted(ways, key=lambda w: w.way_id):
            if way.access_denied:
                continue
            if class_cost.get(way.road_class) is None:
                continue
            flags = set(way.flags)
            if flags & excluded:
                # The DEM cannot see under a tunnel; any elevation here would
                # be the hillside above it.
                g.excluded_structure_ways += 1
                continue
            is_bridge = "is_bridge" in flags
            geometry = way.geometry
            if len(geometry) < 2:
                continue

            # Breakpoints: every connector, plus both ends.
            breaks: list[tuple[float, str]] = [(0.0, f"{way.way_id}@0"), (1.0, f"{way.way_id}@1")]
            for cid, at in way.connectors:
                at = min(1.0, max(0.0, at))
                breaks.append((at, cid))
            # Dedupe on position; a real connector beats a synthetic end.
            merged: dict[int, tuple[float, str]] = {}
            for at, key in sorted(breaks, key=lambda b: (b[0], b[1].startswith(f"{way.way_id}@"))):
                bucket = int(round(at * 1_000_000))
                if bucket not in merged or merged[bucket][1].startswith(f"{way.way_id}@"):
                    merged[bucket] = (at, key)
            ordered = [merged[b] for b in sorted(merged)]
            if len(ordered) < 2:
                continue
            g.way_sources[way.way_id] = way.sources

            for i in range(len(ordered) - 1):
                a_at, a_key = ordered[i]
                b_at, b_key = ordered[i + 1]
                span = slice_fraction(geometry, a_at, b_at)
                length = path_length_m(span)
                if length < min_edge_m:
                    continue
                if is_bridge and length > max_bridge:
                    # A long viaduct reads as a gorge in the terrain profile.
                    g.excluded_bridge_edges += 1
                    continue
                u = g._node(a_key, span[0])
                v = g._node(b_key, span[-1])
                if u == v:
                    continue
                ei = len(g.edges)
                g.edges.append(
                    Edge(
                        u=u,
                        v=v,
                        geometry=tuple(span),
                        length_m=length,
                        road_class=way.road_class,
                        name=way.name,
                        surface=way.surface,
                        way_id=way.way_id,
                        key=f"{way.way_id}:{i}",
                        structure_cost=bridge_multiplier if is_bridge else 1.0,
                    )
                )
                g.adj[u].append((ei, v))
                g.adj[v].append((ei, u))
        for lst in g.adj:
            lst.sort()
        g.node_h = [0.0] * len(g.node_key)
        return g

    def attach_elevation(self, heights: Sequence[float]) -> None:
        if len(heights) != len(self.node_key):
            raise RoutingError("elevation length does not match node count")
        self.node_h = list(heights)

    # ----------------------------------------------------------- connectivity

    def component_of(self, start: int, banned_ways: frozenset[str] = frozenset()) -> set[int]:
        """Nodes reachable from `start`, honouring banned ways.

        Banning a way can cut the network in two on a sparse rural graph. If the
        component is computed without the ban, a waypoint can be chosen on the
        far side of the cut and routing to it then fails outright.
        """
        seen = {start}
        stack = [start]
        while stack:
            n = stack.pop()
            for ei, m in self.adj[n]:
                if m in seen:
                    continue
                if banned_ways and self.edges[ei].way_id in banned_ways:
                    continue
                seen.add(m)
                stack.append(m)
        return seen

    def nearest_node(self, xy: Point, allowed: set[int] | None = None) -> int:
        best, best_d = -1, float("inf")
        for i, p in enumerate(self.node_xy):
            if allowed is not None and i not in allowed:
                continue
            d = (p[0] - xy[0]) ** 2 + (p[1] - xy[1]) ** 2
            if d < best_d or (d == best_d and i < best):
                best, best_d = i, d
        if best < 0:
            raise RoutingError("graph has no nodes")
        return best

    # --------------------------------------------------------------- costing

    def make_cost(
        self,
        class_cost: dict[str, float | None],
        climb_bias: float,
        gradient_scale: float,
        factor_min: float,
        factor_max: float,
        used_edges: frozenset[int] = frozenset(),
        repeat_penalty: float = 1.0,
        banned_ways: frozenset[str] = frozenset(),
    ):
        """Return `cost(edge_index, from_node, to_node) -> float`.

        The character factor is `1 - climb_bias * tanh(g / gradient_scale)`,
        clamped strictly positive so Dijkstra's optimality assumption holds. A
        positive `climb_bias` makes uphill cheap, which is how a mountainous
        course is made to seek real climbs rather than merely being allowed to.
        """
        edges = self.edges
        node_h = self.node_h
        banned = banned_ways

        def cost(ei: int, frm: int, to: int) -> float:
            e = edges[ei]
            base = class_cost.get(e.road_class)
            if base is None:
                return math.inf
            if e.way_id in banned:
                return math.inf
            grad = (node_h[to] - node_h[frm]) / e.length_m if e.length_m > 0 else 0.0
            factor = 1.0 - climb_bias * math.tanh(grad / gradient_scale)
            factor = max(factor_min, min(factor_max, factor))
            c = e.length_m * float(base) * factor * e.structure_cost
            if ei in used_edges:
                c *= repeat_penalty
            return c

        return cost

    # --------------------------------------------------------------- routing

    def dijkstra(self, src: int, cost, targets: set[int] | None = None):
        """Single-source shortest paths. Returns (dist, parent_edge, parent_node)."""
        n = len(self.node_key)
        dist = [math.inf] * n
        pe = [-1] * n
        pn = [-1] * n
        dist[src] = 0.0
        heap = [(0.0, src)]
        remaining = set(targets) if targets else None
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist[u]:
                continue
            if remaining is not None:
                remaining.discard(u)
                if not remaining:
                    break
            for ei, v in self.adj[u]:
                c = cost(ei, u, v)
                if c == math.inf:
                    continue
                nd = d + c
                if nd < dist[v]:
                    dist[v] = nd
                    pe[v] = ei
                    pn[v] = u
                    heapq.heappush(heap, (nd, v))
        return dist, pe, pn

    def path_edges(self, pe: Sequence[int], pn: Sequence[int], src: int, dst: int) -> list[tuple[int, int, int]]:
        """Walk the parent arrays back from `dst`, returning (edge, from, to)."""
        out: list[tuple[int, int, int]] = []
        cur = dst
        while cur != src:
            ei = pe[cur]
            if ei < 0:
                raise RoutingError(f"no path from node {src} to node {dst}")
            out.append((ei, pn[cur], cur))
            cur = pn[cur]
        out.reverse()
        return out

    def geometry_of(self, steps: Sequence[tuple[int, int, int]]) -> list[Point]:
        """Concatenate edge geometries, oriented along travel, without repeating
        the shared vertex at each junction."""
        return self.geometry_with_edges(steps)[0]

    def geometry_with_edges(
        self, steps: Sequence[tuple[int, int, int]]
    ) -> tuple[list[Point], list[int]]:
        """As `geometry_of`, plus the edge index that produced each point.

        Carrying provenance alongside the geometry is what lets Stage 8 name a
        segment after the OpenStreetMap way that actually carries most of it,
        and read that way's surface for `surface_quality`.
        """
        pts: list[Point] = []
        owners: list[int] = []
        for ei, frm, _to in steps:
            e = self.edges[ei]
            span = list(e.geometry) if e.u == frm else list(reversed(e.geometry))
            if pts and haversine_m(pts[-1], span[0]) < 0.5:
                span = span[1:]
            pts.extend(span)
            owners.extend([ei] * len(span))
        return pts, owners


# ---------------------------------------------------------------- loop router


@dataclass(frozen=True)
class RoutedLeg:
    points: tuple[Point, ...]
    steps: tuple[tuple[int, int, int], ...]
    length_m: float
    ring_radius_m: float
    waypoint_nodes: tuple[int, ...]
    edge_owners: tuple[int, ...]


class LoopRouter:
    """Builds a closed loop of a target length with a required terrain character.

    Shape: a ring of waypoints is chosen around the start, one per bearing
    sector, each selected for the character objective (high ground for a
    mountainous course, low for a flat one). The loop is the concatenation of
    shortest paths through them and back. Ring radius is searched over a fixed
    scan then bisected a fixed number of times; whatever shortfall remains is
    made up by an out-and-back spur from the start, which is both how real
    courses make up distance and the only way to land a length exactly.
    """

    def __init__(self, graph: RoadGraph, cfg, leg: str, character: str) -> None:
        self.g = graph
        self.cfg = cfg
        self.leg = leg
        rcfg = cfg["routing"]
        self.class_cost: dict[str, float | None] = rcfg["class_cost"][leg.lower()]
        char = rcfg["character"][character]
        self.climb_bias = float(char["climb_bias"])
        self.relief_fraction = float(char["waypoint_relief_fraction"])
        self.gradient_scale = float(rcfg["gradient_scale"])
        self.factor_min = float(rcfg["character_factor_min"])
        self.factor_max = float(rcfg["character_factor_max"])
        loop = rcfg["loop"]
        self.arc = math.radians(float(loop["waypoint_arc_deg"][character]))
        self.banned_ways: frozenset[str] = frozenset()
        self.bisections = int(loop["bisection_iterations"])
        self.bracket = tuple(float(x) for x in loop["radius_bracket_fraction"])
        self.annulus = float(loop["candidate_annulus_fraction"])
        self.max_candidates = int(loop["max_candidates_per_waypoint"])
        self.repeat_penalty = float(loop["repeat_edge_penalty"])
        self.cul_de_sac_penalty_m = float(loop["cul_de_sac_penalty_m"])

    # ------------------------------------------------------------- waypoints

    def _elevation_target(self, nodes: Sequence[int]) -> float:
        """The elevation waypoints aim for, as a fraction of the local relief.

        Deliberately not a percentile of the node population. A road network is
        dominated by dense town streets, so on Mallorca the 97th-percentile node
        sits at 212 m while the network reaches 834 m -- aiming at percentiles
        put the flagship mountain course in the foothills. Relief fraction asks
        the question that was actually meant: how far up the range available
        here should this course go?

        The extremes are taken at the 1st and 99.9th percentiles rather than the
        raw min and max, so one bad node cannot move the target.
        """
        heights = sorted(self.g.node_h[n] for n in nodes)
        if not heights:
            raise RoutingError("no nodes available for waypoint selection")
        lo = heights[max(0, int(0.01 * (len(heights) - 1)))]
        hi = heights[min(len(heights) - 1, int(0.999 * (len(heights) - 1)))]
        return lo + self.relief_fraction * (hi - lo)

    def _pick_waypoints(
        self, start: int, radius_m: float, count: int, bearing_offset: float, component: set[int]
    ) -> list[int]:
        from .geo import destination

        origin = self.g.node_xy[start]
        target_h = self._elevation_target(sorted(component))
        annulus_m = radius_m * self.annulus
        picks: list[int] = []
        for k in range(count):
            if self.arc >= 2.0 * math.pi - 1e-9:
                bearing = bearing_offset + 2.0 * math.pi * k / count
            else:
                # Spread across an arc centred on the offset, so a coastal start
                # can run out and back into a range rather than being forced to
                # find high ground in every direction.
                span = self.arc
                t = 0.5 if count == 1 else k / (count - 1)
                bearing = bearing_offset - span / 2.0 + span * t
            ring_pt = destination(origin, bearing, radius_m)
            candidates: list[tuple[float, float, int]] = []
            for n in sorted(component):
                d = haversine_m(self.g.node_xy[n], ring_pt)
                if d > annulus_m:
                    continue
                # Prefer a junction: a waypoint on a cul-de-sac forces the route
                # to reverse out the way it came, which is what puts implausible
                # stubs on the map even when the elevation profile is right.
                #
                # A soft penalty rather than a hard sort key. Making it decisive
                # cost the flagship course 700 m of climbing, because the best
                # high ground in a sector is often reached by a spur road. The
                # penalty is denominated in metres of elevation error, so a
                # dead-end worth the detour still wins.
                score = abs(self.g.node_h[n] - target_h)
                if len(self.g.adj[n]) < 3:
                    score += self.cul_de_sac_penalty_m
                candidates.append((score, d, n))
            if not candidates:
                # Widen once to the nearest node in the component, so a sparse
                # sector degrades rather than failing the whole build.
                nearest = min(
                    (haversine_m(self.g.node_xy[n], ring_pt), n) for n in sorted(component)
                )
                picks.append(nearest[1])
                continue
            candidates.sort()
            picks.append(candidates[: self.max_candidates][0][2])
        # Collapse consecutive duplicates; a loop through the same node twice in
        # a row is a degenerate sector, not a route.
        deduped: list[int] = []
        for n in picks:
            if not deduped or deduped[-1] != n:
                deduped.append(n)
        return deduped

    # ------------------------------------------------------------------ loop

    def _build_loop(self, start: int, radius_m: float, count: int, bearing_offset: float, component: set[int]):
        waypoints = self._pick_waypoints(start, radius_m, count, bearing_offset, component)
        order = [start, *waypoints, start]
        steps: list[tuple[int, int, int]] = []
        used: set[int] = set()
        for i in range(len(order) - 1):
            cost = self.g.make_cost(
                self.class_cost,
                self.climb_bias,
                self.gradient_scale,
                self.factor_min,
                self.factor_max,
                used_edges=frozenset(used),
                repeat_penalty=self.repeat_penalty,
                banned_ways=self.banned_ways,
            )
            _dist, pe, pn = self.g.dijkstra(order[i], cost, targets={order[i + 1]})
            leg_steps = self.g.path_edges(pe, pn, order[i], order[i + 1])
            steps.extend(leg_steps)
            used.update(s[0] for s in leg_steps)
        pts, owners = self.g.geometry_with_edges(steps)
        return steps, pts, path_length_m(pts), tuple(waypoints), owners

    def route(self, start: int, target_m: float, count: int, bearing_offset: float) -> RoutedLeg:
        component = self.g.component_of(start, self.banned_ways)
        if len(component) < 32:
            raise RoutingError(
                f"start node sits in a component of {len(component)} nodes; "
                "the start coordinate is not on the routable network"
            )
        lo = target_m * self.bracket[0]
        hi = target_m * self.bracket[1]

        # Fixed coarse scan, then fixed bisection. Deterministic by construction.
        scan = 7
        samples: list[tuple[float, float]] = []
        for i in range(scan):
            r = lo + (hi - lo) * i / (scan - 1)
            _s, _p, length, _w, _o = self._build_loop(start, r, count, bearing_offset, component)
            samples.append((r, length))

        under = [s for s in samples if s[1] <= target_m]
        if not under:
            raise RoutingError(
                f"even the tightest ring produces {samples[0][1]/1000:.1f} km, longer than the "
                f"{target_m/1000:.1f} km target: the road network around this start is too sparse"
            )
        lo_r = max(under, key=lambda s: s[1])[0]
        over = [s for s in samples if s[1] > target_m]
        hi_r = min(over, key=lambda s: s[1])[0] if over else hi

        best = None
        for _ in range(self.bisections):
            mid = (lo_r + hi_r) / 2.0
            steps, pts, length, wps, owners = self._build_loop(start, mid, count, bearing_offset, component)
            if length <= target_m:
                lo_r = mid
                if best is None or length > best[2]:
                    best = (steps, pts, length, wps, mid, owners)
            else:
                hi_r = mid
        if best is None:
            steps, pts, length, wps, owners = self._build_loop(start, lo_r, count, bearing_offset, component)
            best = (steps, pts, length, wps, lo_r, owners)

        steps, pts, length, wps, radius, owners = best
        return RoutedLeg(tuple(pts), tuple(steps), length, radius, wps, tuple(owners))

    # ------------------------------------------------------------------ spur

    def out_and_back(self, start: int, half_m: float) -> tuple[list[Point], list[int]]:
        """An out-and-back stub of exactly `2 * half_m`, on real road.

        Used to land a leg on its nominal distance. Real courses do exactly this
        for the same reason.
        """
        if half_m <= 0.5:
            return [], []
        cost = self.g.make_cost(
            self.class_cost, 0.0, self.gradient_scale, self.factor_min, self.factor_max,
            banned_ways=self.banned_ways,
        )
        _dist, pe, pn = self.g.dijkstra(start, cost)

        # Walk out along the cheapest tree, choosing at each node the successor
        # that keeps the stub on the best-classed road, until length is reached.
        best_node, best_gap = -1, math.inf
        metric: dict[int, float] = {}
        for n in range(len(self.g.node_key)):
            if pe[n] < 0 or n == start:
                continue
            length = self._tree_length(pe, pn, start, n)
            if length is None:
                continue
            metric[n] = length
            gap = abs(length - half_m)
            if gap < best_gap or (gap == best_gap and n < best_node):
                best_node, best_gap = n, gap
        if best_node < 0:
            raise RoutingError("no out-and-back spur available from the transition")

        steps = self.g.path_edges(pe, pn, start, best_node)
        pts, owners = self.g.geometry_with_edges(steps)
        have = path_length_m(pts)
        if have > half_m:
            pts, owners = _trim_to(pts, half_m, owners)
        elif have < half_m:
            pts, owners = _extend_last(self.g, steps, pts, owners, half_m)
        out_pts = list(pts) + list(reversed(pts))[1:]
        out_owners = list(owners) + list(reversed(owners))[1:]
        return out_pts, out_owners

    def _tree_length(self, pe: Sequence[int], pn: Sequence[int], src: int, dst: int) -> float | None:
        total = 0.0
        cur = dst
        guard = 0
        while cur != src:
            ei = pe[cur]
            if ei < 0:
                return None
            total += self.g.edges[ei].length_m
            cur = pn[cur]
            guard += 1
            if guard > 4096:
                return None
        return total


def _trim_to(points: Sequence[Point], length_m: float, owners: Sequence[int]):
    cum = cumulative_m(points)
    if cum[-1] <= length_m:
        return list(points), list(owners)
    for i in range(1, len(cum)):
        if cum[i] >= length_m:
            seg = cum[i] - cum[i - 1]
            t = 0.0 if seg <= 0 else (length_m - cum[i - 1]) / seg
            a, b = points[i - 1], points[i]
            cut = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            return list(points[:i]) + [cut], list(owners[:i]) + [owners[i]]
    return list(points), list(owners)


def _extend_last(graph: RoadGraph, steps, points: Sequence[Point], owners: Sequence[int], length_m: float):
    """Push past the tree's end along the best continuing edge to reach length."""
    pts = list(points)
    own = list(owners)
    visited = {s[0] for s in steps}
    tail = steps[-1][2] if steps else None
    guard = 0
    while tail is not None and path_length_m(pts) < length_m and guard < 64:
        options = sorted(
            (graph.edges[ei].length_m, ei, v)
            for ei, v in graph.adj[tail]
            if ei not in visited
        )
        if not options:
            break
        _l, ei, v = options[-1]
        e = graph.edges[ei]
        span = list(e.geometry) if e.u == tail else list(reversed(e.geometry))
        if pts and haversine_m(pts[-1], span[0]) < 0.5:
            span = span[1:]
        pts.extend(span)
        own.extend([ei] * len(span))
        visited.add(ei)
        tail = v
        guard += 1
    return _trim_to(pts, length_m, own)
