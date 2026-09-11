"""Baking a real course into the shape the 3D map renders.

The map engine (`course-map-3d`) draws a fixed 228 × 156 unit slab. Kalmar's
landform is *generated* from sine shelves and Gaussian peaks — a tuned graphic,
not a survey — and `TRACKS.md` §5-6 sets out exactly what a real venue needs
instead: project the course to metres, choose one horizontal scale for all
three legs, resample the elevation model onto the slab grid, and drape the
routes. This module is that preprocessing, done once per bundle version and
served as a static field.

**The browser never does geography.** It receives a height grid, a water mask,
a distance-to-shore field and three route lines already in slab coordinates.
That is deliberate and it is what `TRACKS.md` §6 asks for: a client that had to
fetch and decode elevation tiles would be slow, would depend on a third party at
render time, and would give a different answer per viewer.

Three decisions the field records rather than leaves implicit:

* **One horizontal scale for all three legs.** Kalmar's three legs disagree by
  14.5×, which is what makes its composition read and is exactly what a real
  map must not do. Here the swim is a speck next to the bike, honestly.
* **Vertical exaggeration is chosen per venue and stated.** A flat coastal
  plain drawn at true scale is a flat plate. The exaggeration that makes it
  legible is computed from the venue's own relief, carried in the payload, and
  shown to the athlete — rather than picked to look dramatic and never
  mentioned.
* **Water comes from the elevation model**, not from a drawn coastline. Real
  coasts have bays and islands, which the generated map's single-valued
  ``coastX(z)`` cannot express at all.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from raceos.api.errors import NotFound
from raceos.config import Settings
from raceos.db.models import Course, CourseBundle
from raceos.ingest.elevation import ElevationSource, TerrariumTiles
from raceos.logging import get_logger
from raceos.storage.base import ObjectNotFoundError, get_storage_backend

logger = get_logger(__name__)

#: The slab, in world units. Fixed, because the camera, the contour parameters
#: and the marker sizes in the renderer are all tuned against these numbers.
SLAB_W = 228.0
SLAB_D = 156.0
#: Breathing room so a course does not run to the slab edge, where the terrain
#: is cut off and reads as a broken plate.
SLAB_PAD = 14.0

#: Mesh grid. Matches the renderer's terrain plane exactly, so every vertex it
#: displaces has a measured height under it rather than an interpolated one.
FIELD_NX = 240
FIELD_NZ = 164

#: Points per leg in the drawn route. Enough that a hairpin still reads as a
#: hairpin, few enough that the payload stays small and the spline stays smooth.
ROUTE_POINTS = 240

#: Metres at or below which the elevation model *may* be water. Terrarium
#: reports open sea as 0 or slightly below, and a small positive threshold
#: catches the foreshore.
#:
#: On its own this is not enough, and Cervia is exactly why: the Romagna plain
#: behind the beach is reclaimed marsh and salt pan sitting at or under this
#: height, so a plain threshold floods the farmland the bike leg is ridden
#: across. Sea is therefore the low ground **connected to the edge of the
#: slab** — see :func:`_sea_mask`. An inland hollow at 0 m is a hollow.
SEA_LEVEL_M = 0.5

#: The foreshore, in world units either side of the water line. A coast that
#: steps vertically reads as a cut-out slab rather than as a shore — the same
#: defect the showcase map's own notes record and fix.
FORESHORE_OFFSHORE = 3.0
FORESHORE_INLAND = 2.0
#: How far the land sits above the water line where it meets it. Enough that
#: the sea sheet never shows through low ground, small enough to be a beach.
SHORE_LIFT = 0.30
#: Depth at the water line, and how much deeper it gets per unit offshore.
SEABED_DEPTH = 0.55
SEABED_SLOPE = 0.03

#: Target relief in world units — how tall the highest ground stands above the
#: lowest on a slab 228 units wide. Chosen so a flat course still has readable
#: contours and a mountainous one does not turn into a spike.
TARGET_RELIEF_UNITS = 17.0
#: Bounds on the exaggeration. Below 3x a coastal plain is a plate; above 30x
#: even a real mountain stops being believable. Kalmar's generated map sits at
#: about 40x, which is why it is a graphic and not a survey.
MIN_EXAGGERATION = 3.0
MAX_EXAGGERATION = 30.0

EARTH_RADIUS_M = 6_371_008.8

#: Bumped whenever the bake changes shape. A cached field from an older
#: version is ignored rather than served, because a renderer written against
#: the new shape would read the old one as a broken map.
FIELD_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Projection:
    """A local tangent plane, metres, centred on the course."""

    lat0: float
    lng0: float

    def to_metres(self, lng: float, lat: float) -> tuple[float, float]:
        east = math.radians(lng - self.lng0) * EARTH_RADIUS_M * math.cos(math.radians(self.lat0))
        north = math.radians(lat - self.lat0) * EARTH_RADIUS_M
        return east, north

    def to_lnglat(self, east: float, north: float) -> tuple[float, float]:
        lat = self.lat0 + math.degrees(north / EARTH_RADIUS_M)
        lng = self.lng0 + math.degrees(east / (EARTH_RADIUS_M * math.cos(math.radians(self.lat0))))
        return lng, lat


def _leg_coordinates(leg: Any) -> list[tuple[float, float]]:
    from geoalchemy2.shape import to_shape

    shape = to_shape(leg.geometry)
    return [(float(c[0]), float(c[1])) for c in shape.coords]


def _resample_indices(count: int, target: int) -> list[int]:
    if count <= target:
        return list(range(count))
    step = (count - 1) / (target - 1)
    return [round(index * step) for index in range(target)]


def _sea_mask(heights: list[float], nx: int, nz: int) -> list[bool]:
    """Low ground connected to the edge of the slab.

    Connectivity is the whole test. Height alone says "at sea level", which a
    salt pan, a drained polder and a gravel pit all are; what makes a cell
    *sea* is that you could swim from it off the edge of the map. A flood fill
    inward from the border answers exactly that, and costs one pass.
    """
    candidate = [height <= SEA_LEVEL_M for height in heights]
    sea = [False] * len(candidate)
    stack: list[int] = []

    for ix in range(nx):
        for iz in (0, nz - 1):
            index = iz * nx + ix
            if candidate[index] and not sea[index]:
                sea[index] = True
                stack.append(index)
    for iz in range(nz):
        for ix in (0, nx - 1):
            index = iz * nx + ix
            if candidate[index] and not sea[index]:
                sea[index] = True
                stack.append(index)

    while stack:
        index = stack.pop()
        ix, iz = index % nx, index // nx
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            jx, jz = ix + dx, iz + dz
            if 0 <= jx < nx and 0 <= jz < nz:
                neighbour = jz * nx + jx
                if candidate[neighbour] and not sea[neighbour]:
                    sea[neighbour] = True
                    stack.append(neighbour)
    return sea


def _signed_shore(water: list[bool], nx: int, nz: int, cell: float) -> list[float]:
    """Distance to the water line: positive inland, negative out to sea.

    One field, both signs, which is what lets a single smoothstep across it
    produce a continuous land-to-sea profile instead of a step — and what the
    sea shader reads for its depth falloff, in place of the coastline formula
    the generated map evaluates in GLSL.
    """
    to_water = _distance_transform(water, nx, nz, cell)
    to_land = _distance_transform([not wet for wet in water], nx, nz, cell)
    return [-to_land[index] if water[index] else to_water[index] for index in range(len(water))]


def _distance_transform(mask: list[bool], nx: int, nz: int, cell: float) -> list[float]:
    """Chamfer distance to the nearest ``True`` cell, in world units.

    Two passes over the grid rather than a proper Euclidean transform: the
    field is used for shading the foreshore and for the sea shader's falloff,
    where a few per cent of anisotropy is invisible, and an exact transform
    would be several times the code for no visible difference.
    """
    big = float(nx + nz) * cell
    dist = [0.0 if value else big for value in mask]

    def at(ix: int, iz: int) -> float:
        return dist[iz * nx + ix]

    diagonal = cell * math.sqrt(2.0)
    for iz in range(nz):
        for ix in range(nx):
            best = at(ix, iz)
            if iz > 0:
                best = min(best, at(ix, iz - 1) + cell)
                if ix > 0:
                    best = min(best, at(ix - 1, iz - 1) + diagonal)
                if ix < nx - 1:
                    best = min(best, at(ix + 1, iz - 1) + diagonal)
            if ix > 0:
                best = min(best, at(ix - 1, iz) + cell)
            dist[iz * nx + ix] = best
    for iz in range(nz - 1, -1, -1):
        for ix in range(nx - 1, -1, -1):
            best = at(ix, iz)
            if iz < nz - 1:
                best = min(best, at(ix, iz + 1) + cell)
                if ix > 0:
                    best = min(best, at(ix - 1, iz + 1) + diagonal)
                if ix < nx - 1:
                    best = min(best, at(ix + 1, iz + 1) + diagonal)
            if ix < nx - 1:
                best = min(best, at(ix + 1, iz) + cell)
            dist[iz * nx + ix] = best
    return dist


def _gaussian_blur(values: list[float], nx: int, nz: int, sigma_cells: float) -> list[float]:
    """Separable Gaussian. The renderer's contours need a C1 field.

    A 30 m elevation model resampled onto a finer grid carries step artefacts
    that come out as kinked, fragmented contour lines. A light blur — below the
    size of any real landform — is what makes every line a true isoline of a
    smooth surface rather than of a staircase.
    """
    if sigma_cells <= 0:
        return values
    radius = max(1, int(sigma_cells * 3))
    kernel = [math.exp(-((offset / sigma_cells) ** 2) / 2) for offset in range(-radius, radius + 1)]
    total = sum(kernel)
    kernel = [k / total for k in kernel]

    horizontal = [0.0] * len(values)
    for iz in range(nz):
        row = iz * nx
        for ix in range(nx):
            acc = 0.0
            for offset, weight in enumerate(kernel, start=-radius):
                sample = min(nx - 1, max(0, ix + offset))
                acc += values[row + sample] * weight
            horizontal[row + ix] = acc

    out = [0.0] * len(values)
    for ix in range(nx):
        for iz in range(nz):
            acc = 0.0
            for offset, weight in enumerate(kernel, start=-radius):
                sample = min(nz - 1, max(0, iz + offset))
                acc += horizontal[sample * nx + ix] * weight
            out[iz * nx + ix] = acc
    return out


# ---------------------------------------------------------------------------
# The bake
# ---------------------------------------------------------------------------


def bake(
    session: Session,
    *,
    course: Course,
    bundle: CourseBundle,
    elevation: ElevationSource,
) -> dict[str, Any]:
    """Project, scale, sample and drape. Returns the field the renderer reads."""
    from raceos.services.course_service import _ordered_legs

    legs = {leg.leg: _leg_coordinates(leg) for leg in _ordered_legs(bundle)}
    if not legs:
        raise NotFound(f"{course.name} has no geometry to build a map from.")

    every_point = [point for points in legs.values() for point in points]
    lngs = [p[0] for p in every_point]
    lats = [p[1] for p in every_point]
    projection = Projection(lat0=(min(lats) + max(lats)) / 2, lng0=(min(lngs) + max(lngs)) / 2)

    projected = {
        leg: [projection.to_metres(lng, lat) for lng, lat in points] for leg, points in legs.items()
    }
    easts = [e for points in projected.values() for e, _ in points]
    norths = [n for points in projected.values() for _, n in points]
    width_m = max(easts) - min(easts)
    depth_m = max(norths) - min(norths)

    # One scale for all three legs. This is the discipline the generated map
    # does not have, and the reason a real 70.3's swim is a speck: at true
    # scale, next to a 90 km bike loop, it is.
    metres_per_unit = max(
        width_m / (SLAB_W - 2 * SLAB_PAD),
        depth_m / (SLAB_D - 2 * SLAB_PAD),
        1.0,
    )
    centre_e = (min(easts) + max(easts)) / 2
    centre_n = (min(norths) + max(norths)) / 2

    def to_world(east: float, north: float) -> tuple[float, float]:
        # Slab z runs south-positive, matching the renderer's plane.
        return ((east - centre_e) / metres_per_unit, -(north - centre_n) / metres_per_unit)

    # --- sample the elevation model onto the slab grid ------------------
    cell_x = SLAB_W / (FIELD_NX - 1)
    cell_z = SLAB_D / (FIELD_NZ - 1)
    samples: list[tuple[float, float]] = []
    for iz in range(FIELD_NZ):
        world_z = -SLAB_D / 2 + iz * cell_z
        for ix in range(FIELD_NX):
            world_x = -SLAB_W / 2 + ix * cell_x
            east = centre_e + world_x * metres_per_unit
            north = centre_n - world_z * metres_per_unit
            samples.append(projection.to_lnglat(east, north))

    heights_m = elevation.sample(samples)

    # --- water, from the model rather than from a drawn coastline -------
    water_mask = _sea_mask(heights_m, FIELD_NX, FIELD_NZ)
    has_water = sum(water_mask) > FIELD_NX * FIELD_NZ * 0.01
    if not has_water:
        water_mask = [False] * len(heights_m)

    land_heights = [h for h, wet in zip(heights_m, water_mask, strict=True) if not wet]
    if not land_heights:
        land_heights = list(heights_m)
    low_m = min(land_heights)
    high_m = max(land_heights)
    relief_m = max(high_m - low_m, 1.0)

    # --- vertical scale, chosen and recorded ---------------------------
    metres_per_unit_vertical = max(relief_m / TARGET_RELIEF_UNITS, 1e-6)
    exaggeration = metres_per_unit / metres_per_unit_vertical
    if exaggeration < MIN_EXAGGERATION:
        exaggeration = MIN_EXAGGERATION
        metres_per_unit_vertical = metres_per_unit / exaggeration
    elif exaggeration > MAX_EXAGGERATION:
        exaggeration = MAX_EXAGGERATION
        metres_per_unit_vertical = metres_per_unit / exaggeration

    #: World y of the sea surface. Land sits above it; the seabed drops below.
    water_level = 0.0

    signed_shore = (
        _signed_shore(water_mask, FIELD_NX, FIELD_NZ, cell_x)
        if has_water
        else [FORESHORE_INLAND * 4] * len(water_mask)
    )

    def smoothstep(edge0: float, edge1: float, value: float) -> float:
        t = min(1.0, max(0.0, (value - edge0) / (edge1 - edge0)))
        return t * t * (3 - 2 * t)

    # One continuous land-to-sea profile, not two surfaces meeting at a step.
    # The seabed descends offshore, a short foreshore lifts out of the water,
    # and the coastal plain carries the measured terrain above it.
    field: list[float] = []
    for height_m, shore in zip(heights_m, signed_shore, strict=True):
        plain = water_level + SHORE_LIFT + (height_m - low_m) / metres_per_unit_vertical
        if not has_water:
            field.append(plain)
            continue
        seabed = water_level - SEABED_DEPTH - SEABED_SLOPE * max(-shore, 0.0)
        surf = smoothstep(-FORESHORE_OFFSHORE, FORESHORE_INLAND, shore)
        field.append(seabed * (1 - surf) + plain * surf)

    field = _gaussian_blur(field, FIELD_NX, FIELD_NZ, sigma_cells=0.7)

    # --- drape the routes ------------------------------------------------
    routes: dict[str, list[list[float]]] = {}
    for leg, points in projected.items():
        kept = _resample_indices(len(points), ROUTE_POINTS)
        routes[leg.value.lower()] = [
            [round(value, 3) for value in to_world(*points[index])] for index in kept
        ]

    legs_summary = {
        leg.leg.value.lower(): {
            "distance_m": float(leg.distance_m),
            "elevation_gain_m": float(leg.elevation_gain_m),
            "surface_quality": leg.surface_quality.value,
        }
        for leg in _ordered_legs(bundle)
    }

    return {
        "schema_version": FIELD_SCHEMA_VERSION,
        "course": {
            "slug": course.slug,
            "name": course.name,
            "place": course.place,
            "distance_type": course.distance_type.value,
        },
        "bundle_version": bundle.version,
        "attribution": bundle.attribution,
        "world": {
            "shore_lift": SHORE_LIFT,
            "width": SLAB_W,
            "depth": SLAB_D,
            "metres_per_unit": round(metres_per_unit, 4),
            "metres_per_unit_vertical": round(metres_per_unit_vertical, 5),
            "vertical_exaggeration": round(exaggeration, 1),
            "water_level": water_level,
            "has_water": has_water,
            "datum_m": round(low_m, 1),
            "relief_m": round(relief_m, 1),
            "origin": {"lat": projection.lat0, "lng": projection.lng0},
            "centre_offset_m": [round(centre_e, 1), round(centre_n, 1)],
        },
        "field": {
            "nx": FIELD_NX,
            "nz": FIELD_NZ,
            # World y, not metres: the renderer works in world units and
            # converting per vertex in the browser would be 39,360 divisions
            # on every rebuild.
            "heights": [round(value, 2) for value in field],
            "water": [1 if wet else 0 for wet in water_mask],
            # Signed: positive inland, negative offshore. One field serves the
            # cursor's SHORE label and the sea shader's depth falloff, which in
            # the generated map are two separate evaluations of `coastX`.
            "signed_shore": [round(value, 1) for value in signed_shore],
        },
        "routes": routes,
        "legs": legs_summary,
        "barriers": list(bundle.barriers or []),
        "aid_stations": list(bundle.aid_stations or []),
        "waypoints": list(bundle.waypoints or []),
    }


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def storage_key(course: Course, bundle: CourseBundle) -> str:
    """Keyed by bundle version, because a published bundle is immutable.

    A new version is a new key rather than an overwrite, so a client holding
    the old one keeps a field that matches the geometry it was solved against.
    """
    return f"terrain-fields/v{FIELD_SCHEMA_VERSION}/{course.slug}/{bundle.version}.json"


def field_for(
    session: Session,
    *,
    course: Course,
    bundle: CourseBundle,
    settings: Settings,
    elevation: ElevationSource | None = None,
) -> dict[str, Any]:
    """The baked field, from object storage or baked now and stored.

    Baking samples forty thousand elevation points across a few dozen tiles —
    seconds on a cold course and nothing at all thereafter. Storing the result
    rather than recomputing is what keeps a map open instantly for every
    athlete after the first.
    """
    key = storage_key(course, bundle)
    storage = get_storage_backend(settings)
    try:
        return json.loads(storage.get(key))
    except (ObjectNotFoundError, ValueError):
        pass

    source = elevation or TerrariumTiles(
        settings.elevation_tile_url,
        zoom=settings.elevation_sample_zoom,
        timeout_seconds=settings.elevation_request_timeout_seconds,
        attribution=settings.elevation_attribution,
    )
    try:
        payload = bake(session, course=course, bundle=bundle, elevation=source)
    finally:
        if elevation is None:
            source.close()

    storage.put(key, json.dumps(payload).encode(), content_type="application/json")
    logger.info(
        "terrain field baked",
        extra={
            "course_slug": course.slug,
            "bundle_version": bundle.version,
            "metres_per_unit": payload["world"]["metres_per_unit"],
            "vertical_exaggeration": payload["world"]["vertical_exaggeration"],
        },
    )
    return payload
