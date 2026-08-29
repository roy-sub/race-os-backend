"""Clipped terrain PMTiles extract, one per course bounding box.

The frontend consumes terrain as a Terrarium-encoded `raster-dem` source
(Part 10.4). Serving the global tileset for a course that occupies 0.3 degrees
would stream far more than the map ever draws, so each bundle ships its own
clipped archive and `course_bundles.terrain_pmtiles_key` points at it.

Tiles are copied byte-for-byte from the same tileset the pipeline sampled
elevation from, so the renderer's terrain and the solver's gradients cannot
disagree -- which is the whole point of Part 10.3's "one geometry, three
consumers".
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

from .sources.terrarium import TerrariumDemSource


@dataclass(frozen=True)
class TerrainExtract:
    path: Path
    tile_count: int
    bytes_written: int
    min_zoom: int
    max_zoom: int
    bbox: tuple[float, float, float, float]


def _tile_range(bbox, zoom: int):
    minx, miny, maxx, maxy = bbox
    n = 1 << zoom

    def xtile(lon):
        return int((lon + 180.0) / 360.0 * n)

    def ytile(lat):
        lat = max(-85.05112878, min(85.05112878, lat))
        r = math.radians(lat)
        return int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)

    x0, x1 = xtile(minx), xtile(maxx)
    y0, y1 = ytile(maxy), ytile(miny)
    return (
        max(0, min(x0, x1)),
        max(0, min(y0, y1)),
        min(n - 1, max(x0, x1)),
        min(n - 1, max(y0, y1)),
    )


def write_extract(
    dem: TerrariumDemSource,
    bbox: tuple[float, float, float, float],
    out_path: str | Path,
    course_name: str,
    attribution: str,
) -> TerrainExtract:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    minx, miny, maxx, maxy = bbox

    written = 0
    total_bytes = 0
    with out_path.open("wb") as fh:
        writer = Writer(fh)
        for z in range(dem.extract_min_zoom, dem.extract_max_zoom + 1):
            x0, y0, x1, y1 = _tile_range(bbox, z)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    data = dem.tile_bytes(z, x, y)
                    writer.write_tile(zxy_to_tileid(z, x, y), data)
                    written += 1
                    total_bytes += len(data)
        writer.finalize(
            {
                "tile_type": TileType.PNG,
                "tile_compression": Compression.NONE,
                "min_zoom": dem.extract_min_zoom,
                "max_zoom": dem.extract_max_zoom,
                "min_lon_e7": int(minx * 1e7),
                "min_lat_e7": int(miny * 1e7),
                "max_lon_e7": int(maxx * 1e7),
                "max_lat_e7": int(maxy * 1e7),
                "center_zoom": dem.extract_min_zoom,
                "center_lon_e7": int((minx + maxx) / 2 * 1e7),
                "center_lat_e7": int((miny + maxy) / 2 * 1e7),
            },
            {
                "name": f"{course_name} terrain",
                "type": "baselayer",
                "format": "png",
                "encoding": "terrarium",
                "attribution": attribution,
                "description": "Terrarium-encoded DEM clipped to the course bounding box.",
            },
        )

    return TerrainExtract(
        path=out_path,
        tile_count=written,
        bytes_written=out_path.stat().st_size,
        min_zoom=dem.extract_min_zoom,
        max_zoom=dem.extract_max_zoom,
        bbox=bbox,
    )
