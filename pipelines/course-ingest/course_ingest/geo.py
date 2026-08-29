"""Geodesy and polyline helpers.

Everything here is pure and deterministic: no randomness, no clock, no I/O.
Distances use the WGS84 mean earth radius with the haversine formula, which is
accurate to better than 0.5% anywhere and is stable under float64 summation --
the pipeline sums tens of thousands of 10 m steps, so stability matters more
than the last decimal of accuracy on any single step.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6371008.8

Point = tuple[float, float]  # (lon, lat)


def haversine_m(a: Point, b: Point) -> float:
    lon1, lat1 = a
    lon2, lat2 = b
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def path_length_m(points: Sequence[Point]) -> float:
    return sum(haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1))


def cumulative_m(points: Sequence[Point]) -> list[float]:
    out = [0.0]
    for i in range(len(points) - 1):
        out.append(out[-1] + haversine_m(points[i], points[i + 1]))
    return out


def bearing_rad(a: Point, b: Point) -> float:
    """Initial great-circle bearing from `a` to `b`, radians clockwise from north."""
    lon1, lat1 = a
    lon2, lat2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.atan2(y, x)


def offset_m(origin: Point, east_m: float, north_m: float) -> Point:
    """Local ENU offset in metres, applied on the sphere. Exact enough for the
    few-kilometre offsets used to draw a swim course."""
    lon, lat = origin
    dlat = math.degrees(north_m / EARTH_RADIUS_M)
    dlon = math.degrees(east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return (lon + dlon, lat + dlat)


def destination(origin: Point, bearing: float, distance_m: float) -> Point:
    """Point `distance_m` from `origin` along `bearing` (radians from north)."""
    lon, lat = origin
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    dr = distance_m / EARTH_RADIUS_M
    p2 = math.asin(math.sin(p1) * math.cos(dr) + math.cos(p1) * math.sin(dr) * math.cos(bearing))
    l2 = l1 + math.atan2(
        math.sin(bearing) * math.sin(dr) * math.cos(p1),
        math.cos(dr) - math.sin(p1) * math.sin(p2),
    )
    return (math.degrees(l2), math.degrees(p2))


def local_scale(lat: float) -> tuple[float, float]:
    """Metres per degree of (longitude, latitude) at this latitude."""
    m_per_deg_lat = math.pi * EARTH_RADIUS_M / 180.0
    return (m_per_deg_lat * math.cos(math.radians(lat)), m_per_deg_lat)


def densify(points: Sequence[Point], max_step_m: float) -> list[Point]:
    """Insert intermediate vertices so no leg exceeds `max_step_m`."""
    if len(points) < 2:
        return list(points)
    out: list[Point] = [points[0]]
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        d = haversine_m(a, b)
        n = max(1, int(math.ceil(d / max_step_m)))
        for k in range(1, n + 1):
            t = k / n
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


def resample(points: Sequence[Point], spacing_m: float) -> list[Point]:
    """Re-space a polyline at a fixed interval along its own length.

    The first and last vertices are preserved exactly. The final interval
    absorbs the remainder, so node spacing is `spacing_m` everywhere except the
    last step, which is between 0.5x and 1.5x of it.
    """
    if len(points) < 2:
        return list(points)
    cum = cumulative_m(points)
    total = cum[-1]
    if total <= 0.0:
        return [points[0]]
    n = max(1, int(round(total / spacing_m)))
    step = total / n
    out: list[Point] = [points[0]]
    j = 0
    for i in range(1, n):
        target = step * i
        while j + 1 < len(cum) - 1 and cum[j + 1] < target:
            j += 1
        seg = cum[j + 1] - cum[j]
        t = 0.0 if seg <= 0.0 else (target - cum[j]) / seg
        a, b = points[j], points[j + 1]
        out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    out.append(points[-1])
    return out


def dedupe(points: Sequence[Point], min_step_m: float) -> list[Point]:
    """Drop consecutive points closer together than `min_step_m`.

    The last point is always retained, so a loop stays closed.
    """
    if not points:
        return []
    out: list[Point] = [points[0]]
    for p in points[1:-1] if len(points) > 1 else []:
        if haversine_m(out[-1], p) >= min_step_m:
            out.append(p)
    if len(points) > 1:
        out.append(points[-1])
    return out


def point_segment_distance_m(p: Point, a: Point, b: Point) -> tuple[float, float]:
    """Distance from `p` to segment `a`-`b`, and the projection parameter t.

    Uses a local equirectangular projection about `p`; valid because the caller
    only ever asks about segments a few tens of metres away.
    """
    mx, my = local_scale(p[1])
    ax, ay = (a[0] - p[0]) * mx, (a[1] - p[1]) * my
    bx, by = (b[0] - p[0]) * mx, (b[1] - p[1]) * my
    dx, dy = bx - ax, by - ay
    den = dx * dx + dy * dy
    if den <= 0.0:
        return math.hypot(ax, ay), 0.0
    t = -(ax * dx + ay * dy) / den
    t = max(0.0, min(1.0, t))
    return math.hypot(ax + t * dx, ay + t * dy), t


def slice_fraction(points: Sequence[Point], t0: float, t1: float) -> list[Point]:
    """Sub-polyline between two fractional positions along the line's length."""
    if t1 < t0:
        return list(reversed(slice_fraction(points, t1, t0)))
    cum = cumulative_m(points)
    total = cum[-1]
    if total <= 0.0:
        return [points[0], points[-1]]
    d0, d1 = t0 * total, t1 * total

    def at(d: float) -> tuple[Point, int]:
        j = 0
        while j + 1 < len(cum) - 1 and cum[j + 1] < d:
            j += 1
        seg = cum[j + 1] - cum[j]
        t = 0.0 if seg <= 0.0 else (d - cum[j]) / seg
        a, b = points[j], points[j + 1]
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), j

    p0, j0 = at(d0)
    p1, j1 = at(d1)
    mid = [points[k] for k in range(j0 + 1, j1 + 1)]
    return [p0, *mid, p1]


def bbox_of(points: Iterable[Point]) -> tuple[float, float, float, float]:
    xs, ys = zip(*points)
    return (min(xs), min(ys), max(xs), max(ys))


def expand_bbox(bbox: tuple[float, float, float, float], margin_deg: float):
    x0, y0, x1, y1 = bbox
    return (x0 - margin_deg, y0 - margin_deg, x1 + margin_deg, y1 + margin_deg)


def point_in_ring(p: Point, ring: Sequence[Point]) -> bool:
    """Even-odd ray casting in lon/lat space. Adequate for the small water
    polygons the swim leg is drawn inside."""
    x, y = p
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xint > x:
                inside = not inside
    return inside


def ring_clearance_m(p: Point, ring: Sequence[Point]) -> float:
    """Shortest distance from `p` to a ring's boundary, in metres."""
    best = float("inf")
    for i in range(len(ring)):
        a = ring[i]
        b = ring[(i + 1) % len(ring)]
        d, _ = point_segment_distance_m(p, a, b)
        if d < best:
            best = d
    return best


# --------------------------------------------------------------- ring helpers
# Vectorised variants for the very large coastline rings Overture delivers
# (the Balearic Sea polygon carries ~100 000 vertices).


def ring_arrays(ring: Sequence[Point]):
    import numpy as np

    arr = np.asarray(ring, dtype=np.float64)
    return arr[:, 0], arr[:, 1]


def points_in_ring_np(points: Sequence[Point], ring_x, ring_y) -> list[bool]:
    """Even-odd ray casting, vectorised over ring edges."""
    import numpy as np

    x1 = ring_x
    y1 = ring_y
    x2 = np.roll(ring_x, -1)
    y2 = np.roll(ring_y, -1)
    dx = x2 - x1
    dy = y2 - y1
    out = []
    for px, py in points:
        straddles = (y1 > py) != (y2 > py)
        if not straddles.any():
            out.append(False)
            continue
        idx = np.nonzero(straddles)[0]
        xint = x1[idx] + (py - y1[idx]) * dx[idx] / dy[idx]
        out.append(bool(np.count_nonzero(xint > px) % 2))
    return out


def local_ring_segments(ring: Sequence[Point], bbox, margin_deg: float):
    """Ring edges whose endpoints fall inside `bbox` grown by `margin_deg`.

    Clearance only ever asks about nearby boundary, so restricting to a local
    window turns a 100 000-edge scan into a few hundred.
    """
    x0, y0, x1, y1 = bbox
    x0 -= margin_deg
    y0 -= margin_deg
    x1 += margin_deg
    y1 += margin_deg
    out: list[tuple[Point, Point]] = []
    n = len(ring)
    for i in range(n):
        a = ring[i]
        b = ring[(i + 1) % n]
        if (
            x0 <= a[0] <= x1 and y0 <= a[1] <= y1
        ) or (x0 <= b[0] <= x1 and y0 <= b[1] <= y1):
            out.append((a, b))
    return out


def clearance_to_segments(p: Point, segments) -> float:
    best = float("inf")
    for a, b in segments:
        d, _ = point_segment_distance_m(p, a, b)
        if d < best:
            best = d
    return best


def elevation_gain(heights: Sequence[float], threshold_m: float = 0.0) -> float:
    """Total ascent, optionally with a hysteresis threshold.

    With `threshold_m = 0` this is the plain sum of positive node-to-node
    differences -- the definition SOLVER_MODEL.md 1.1 uses for a segment's
    `elevation_gain_m`, and the one the solver recomputes from the node series.

    With a threshold it is the conventional surveyed ascent: a rise only counts
    once it clears `threshold_m` above the last reversal. This matters because
    a DEM sampled every 10 m has a vertical noise floor, and summing every
    positive difference credits that noise as climbing. On a measured
    pancake-flat coastal marathon spanning eleven vertical metres end to end,
    the unfiltered sum reports 382 m of ascent; at a 3 m threshold it reports
    what a surveyor would.

    Both numbers are emitted. Neither is smoothing: the delivered elevation
    series is untouched either way, and this is a statistic over it.
    """
    if len(heights) < 2:
        return 0.0
    if threshold_m <= 0.0:
        return sum(max(0.0, heights[i + 1] - heights[i]) for i in range(len(heights) - 1))

    total = 0.0
    anchor = heights[0]
    peak = heights[0]
    climbing = False
    for h in heights[1:]:
        if climbing:
            if h > peak:
                peak = h
            elif peak - h >= threshold_m:
                total += peak - anchor
                anchor = h
                peak = h
                climbing = False
        else:
            if h < anchor:
                anchor = h
                peak = h
            elif h - anchor >= threshold_m:
                climbing = True
                peak = h
    if climbing and peak - anchor >= threshold_m:
        total += peak - anchor
    return total
