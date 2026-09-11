"""Terrain elevation for a submitted route, behind one interface.

``SOLVER_MODEL.md`` §1.2 requires a bundle's elevation to be **terrain-sampled**
— never barometric, never the consumer GPS trace in the uploaded file. A
watch's altimeter drifts tens of metres over a long ride and swings with the
weather, and the solver reads gradient straight off the node series, so a noisy
elevation profile does not produce a slightly wrong plan: it produces a plan
that invents climbs the road does not have.

So the uploaded file's own ``<ele>`` values are read for nothing. Elevation
comes from a digital elevation model, sampled at the resampled node positions,
and the interface below is what every caller depends on:

:class:`TerrariumTiles`
    The real one. Terrarium-encoded PNG tiles, bilinearly interpolated — the
    same tile set and the same decode the offline ingest pipeline uses
    (``pipelines/course-ingest/config/sources.yaml``), so a course an athlete
    submits and a course the pipeline generates get their heights from one
    source and are comparable.

:class:`ConstantElevation`
    A real implementation, not a mock: every sample is one height. It is what
    the test suite runs against, because the brief requires the suite to work
    fully offline, and it is what a swim leg legitimately uses — a water
    surface is level, and sampling a DEM across it would inject gradient into
    a leg that physically has none.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from io import BytesIO

import httpx

from raceos.logging import get_logger

logger = get_logger(__name__)

#: A sample point: ``(longitude, latitude)`` — GeoJSON order, which is the
#: order every coordinate in this codebase travels in.
LngLat = tuple[float, float]


class ElevationError(RuntimeError):
    """The DEM could not answer for a point that was asked about.

    Never softened into an interpolation across the gap. A missing tile means
    the route leaves the covered area, and guessing a height there would put a
    fabricated gradient into a solved plan.
    """


class ElevationSource(ABC):
    """What the rest of the application may assume about terrain heights."""

    #: Carried into ``course_bundles.attribution``. ODbL and the DEM licence
    #: both oblige it wherever the derived data is displayed.
    attribution: str = ""

    @abstractmethod
    def sample(self, points: Sequence[LngLat]) -> list[float]:
        """Height in metres for each point, in the order given."""

    def close(self) -> None:  # noqa: B027 - optional hook, not every source holds anything
        """Release anything held open. Safe to call more than once."""


class ConstantElevation(ElevationSource):
    """Every point at one height. Offline, deterministic, and legitimate.

    Used by the test suite, and used in production for the swim leg, where a
    level water surface is the physically correct answer.
    """

    def __init__(self, height_m: float = 0.0, *, attribution: str = "Level water surface") -> None:
        self.height_m = float(height_m)
        self.attribution = attribution

    def sample(self, points: Sequence[LngLat]) -> list[float]:
        return [self.height_m] * len(points)


def _tile_xy(lng: float, lat: float, zoom: int) -> tuple[float, float]:
    """Web-Mercator tile coordinates, fractional so a pixel can be located."""
    n = 2.0**zoom
    x = (lng + 180.0) / 360.0 * n
    lat_rad = math.radians(max(-85.05112878, min(85.05112878, lat)))
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def decode_terrarium(r: int, g: int, b: int) -> float:
    """Terrarium encoding: ``(R * 256 + G + B / 256) - 32768`` metres.

    Exposed by name because it is the one piece of arithmetic here that is
    worth testing directly against a known summit — a decode that is wrong by
    a constant looks entirely plausible on a chart.
    """
    return (r * 256.0 + g + b / 256.0) - 32768.0


class TerrariumTiles(ElevationSource):
    """Terrarium-encoded DEM tiles over HTTP, bilinearly interpolated.

    Tiles are fetched once and held for the life of the object. A 90 km route
    resampled to 10 m is nine thousand points over a few dozen tiles, so
    fetching per point would be nine thousand requests for the same forty
    images; the cache is what makes this affordable rather than an
    optimisation to add later.
    """

    def __init__(
        self,
        tile_url: str,
        *,
        zoom: int = 14,
        tile_size: int = 256,
        timeout_seconds: float = 120.0,
        attribution: str = "AWS Terrain Tiles",
    ) -> None:
        self.tile_url = tile_url
        self.zoom = zoom
        self.tile_size = tile_size
        self.attribution = attribution
        self._client = httpx.Client(timeout=timeout_seconds, follow_redirects=True)
        self._tiles: dict[tuple[int, int], list[list[tuple[int, int, int]]]] = {}

    def close(self) -> None:
        self._client.close()
        self._tiles.clear()

    def __enter__(self) -> TerrariumTiles:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _tile(self, tx: int, ty: int) -> list[list[tuple[int, int, int]]]:
        cached = self._tiles.get((tx, ty))
        if cached is not None:
            return cached

        from PIL import Image

        url = self.tile_url.format(z=self.zoom, x=tx, y=ty)
        try:
            response = self._client.get(url)
            response.raise_for_status()
            image = Image.open(BytesIO(response.content)).convert("RGB")
        except Exception as exc:  # every failure here is the same failure
            raise ElevationError(
                f"No elevation tile at z{self.zoom}/{tx}/{ty}. The route may "
                f"leave the covered area, or the tile service may be down. "
                f"Nothing is interpolated across a gap."
            ) from exc

        if image.size != (self.tile_size, self.tile_size):
            image = image.resize((self.tile_size, self.tile_size))
        pixels = list(image.getdata())
        rows = [
            pixels[row * self.tile_size : (row + 1) * self.tile_size]
            for row in range(self.tile_size)
        ]
        self._tiles[(tx, ty)] = rows
        return rows

    def _height_at_pixel(self, tx: int, ty: int, px: int, py: int) -> float:
        """One pixel, following the tile boundary when the index runs off."""
        tx += px // self.tile_size
        ty += py // self.tile_size
        px %= self.tile_size
        py %= self.tile_size
        r, g, b = self._tile(tx, ty)[py][px]
        return decode_terrarium(r, g, b)

    def sample(self, points: Sequence[LngLat]) -> list[float]:
        """Bilinear over the four pixels around each point.

        Bilinear rather than nearest because the node spacing (10 m) is finer
        than the DEM's own resolution (~30 m): nearest-neighbour would
        staircase the profile into a series of flats and steps, and every step
        would read to the solver as a wall.
        """
        out: list[float] = []
        for lng, lat in points:
            fx, fy = _tile_xy(lng, lat, self.zoom)
            tx, ty = int(math.floor(fx)), int(math.floor(fy))
            # Pixel position within the tile, offset by half a pixel so the
            # interpolation is between pixel *centres*.
            x = (fx - tx) * self.tile_size - 0.5
            y = (fy - ty) * self.tile_size - 0.5
            x0, y0 = math.floor(x), math.floor(y)
            dx, dy = x - x0, y - y0
            h00 = self._height_at_pixel(tx, ty, int(x0), int(y0))
            h10 = self._height_at_pixel(tx, ty, int(x0) + 1, int(y0))
            h01 = self._height_at_pixel(tx, ty, int(x0), int(y0) + 1)
            h11 = self._height_at_pixel(tx, ty, int(x0) + 1, int(y0) + 1)
            top = h00 * (1 - dx) + h10 * dx
            bottom = h01 * (1 - dx) + h11 * dx
            out.append(top * (1 - dy) + bottom * dy)
        return out

    @property
    def tiles_fetched(self) -> int:
        return len(self._tiles)
