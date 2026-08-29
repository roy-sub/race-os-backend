"""Terrarium-encoded DEM source.

Elevation comes from the terrain model and nowhere else. GPS and barometric
altitude are never consulted, even when a source carries them: barometers drift
with weather, and a bundle whose gradients drift is a bundle whose pacing
targets drift.

Terrarium encodes height as `(R * 256 + G + B / 256) - 32768` metres.

Tileset substitution
--------------------
RaceOS_Build_Spec.md Part 2 names the Mapterhorn tileset (Copernicus GLO-30 plus
national LiDAR). AWS Terrain Tiles is used here instead, because Mapterhorn is
not reachable from this build environment. The encoding is identical, so the
frontend's `raster-dem` source consumes either without change, and the swap is a
change to `sources.yaml: elevation.tile_url` alone. See README.md.
"""
from __future__ import annotations

import io
import math
from concurrent.futures import ThreadPoolExecutor
from typing import Sequence

import numpy as np
import requests
from PIL import Image

from ..geo import Point
from .base import DemSource, MissingDemTile
from .cache import BlobCache


def lonlat_to_pixel(lon: float, lat: float, zoom: int, tile_size: int) -> tuple[float, float]:
    """Web-Mercator pixel coordinates at `zoom`, in whole-world pixel space."""
    n = float(1 << zoom) * tile_size
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    s = max(-0.9999, min(0.9999, s))
    y = (0.5 - math.log((1.0 + s) / (1.0 - s)) / (4.0 * math.pi)) * n
    return x, y


class TerrariumDemSource(DemSource):
    def __init__(self, cfg, cache: BlobCache) -> None:
        elev = cfg["sources"]["elevation"]
        self.tile_url: str = elev["tile_url"]
        self.sample_zoom: int = int(elev["sample_zoom"])
        self.extract_min_zoom: int = int(elev["extract_min_zoom"])
        self.extract_max_zoom: int = int(elev["extract_max_zoom"])
        self.tile_size: int = int(elev["tile_size"])
        self._timeout = int(elev["request_timeout_s"])
        self._parallel = int(elev["max_parallel_tile_reads"])
        self._fail_on_missing = bool(elev["fail_on_missing_tile"])
        self._attribution: str = elev["attribution"]
        self._cache = cache
        self._session = requests.Session()
        self._tiles: dict[tuple[int, int, int], np.ndarray] = {}
        self.snapshot_id = f"terrarium:{self.tile_url}"
        self.tiles_fetched = 0

    def attribution(self) -> str:
        return self._attribution

    # ------------------------------------------------------------------ tiles

    def tile_bytes(self, z: int, x: int, y: int) -> bytes:
        url = self.tile_url.format(z=z, x=x, y=y)
        cached = self._cache.get("terrarium", url)
        if cached is not None:
            return cached
        resp = self._session.get(url, timeout=self._timeout)
        if resp.status_code == 404:
            raise MissingDemTile(
                f"DEM tile {z}/{x}/{y} is absent from {self.tile_url}. "
                "Refusing to interpolate across a coverage gap."
            )
        resp.raise_for_status()
        self.tiles_fetched += 1
        self._cache.put("terrarium", url, resp.content)
        return resp.content

    def _tile(self, z: int, x: int, y: int) -> np.ndarray:
        key = (z, x, y)
        grid = self._tiles.get(key)
        if grid is not None:
            return grid
        try:
            raw = self.tile_bytes(z, x, y)
        except MissingDemTile:
            if self._fail_on_missing:
                raise
            raise
        img = np.asarray(Image.open(io.BytesIO(raw)).convert("RGB")).astype(np.float64)
        grid = (img[:, :, 0] * 256.0 + img[:, :, 1] + img[:, :, 2] / 256.0) - 32768.0
        self._tiles[key] = grid
        return grid

    def prefetch(self, points: Sequence[Point]) -> None:
        """Warm the tile cache in parallel. Ordering of the fetch does not
        affect results; only the cache is mutated."""
        z, ts = self.sample_zoom, self.tile_size
        needed = set()
        for lon, lat in points:
            px, py = lonlat_to_pixel(lon, lat, z, ts)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    needed.add((int((px + dx) // ts), int((py + dy) // ts)))
        todo = sorted(t for t in needed if (z, t[0], t[1]) not in self._tiles)
        if not todo:
            return
        with ThreadPoolExecutor(self._parallel) as pool:
            list(pool.map(lambda t: self.tile_bytes(z, t[0], t[1]), todo))

    # --------------------------------------------------------------- sampling

    def sample(self, points: Sequence[Point]) -> list[float]:
        """Bilinear sample at `sample_zoom`.

        Bilinear rather than nearest because a 10 m node spacing against a ~7 m
        pixel would otherwise produce a staircase, and a staircase in elevation
        is a square wave in gradient.
        """
        self.prefetch(points)
        z, ts = self.sample_zoom, self.tile_size
        out: list[float] = []
        for lon, lat in points:
            px, py = lonlat_to_pixel(lon, lat, z, ts)
            fx, fy = px - 0.5, py - 0.5
            x0, y0 = math.floor(fx), math.floor(fy)
            tx, ty = fx - x0, fy - y0
            h = 0.0
            for dx, dy, w in ((0, 0, (1 - tx) * (1 - ty)), (1, 0, tx * (1 - ty)),
                              (0, 1, (1 - tx) * ty), (1, 1, tx * ty)):
                if w == 0.0:
                    continue
                gx, gy = x0 + dx, y0 + dy
                grid = self._tile(z, gx // ts, gy // ts)
                h += w * float(grid[gy % ts, gx % ts])
            out.append(h)
        return out


class FixtureDemSource(DemSource):
    """DEM source backed by checked-in Terrarium tiles, for offline tests."""

    def __init__(self, tile_dir, sample_zoom: int = 14, tile_size: int = 256) -> None:
        from pathlib import Path

        self.dir = Path(tile_dir)
        self.sample_zoom = sample_zoom
        self.tile_size = tile_size
        self.snapshot_id = f"terrarium-fixture:{self.dir.name}"
        self._tiles: dict[tuple[int, int, int], np.ndarray] = {}

    def attribution(self) -> str:
        return "AWS Terrain Tiles (test fixture)"

    def _tile(self, z: int, x: int, y: int) -> np.ndarray:
        key = (z, x, y)
        if key in self._tiles:
            return self._tiles[key]
        path = self.dir / f"{z}_{x}_{y}.png"
        if not path.exists():
            raise MissingDemTile(f"fixture DEM tile missing: {path}")
        img = np.asarray(Image.open(path).convert("RGB")).astype(np.float64)
        grid = (img[:, :, 0] * 256.0 + img[:, :, 1] + img[:, :, 2] / 256.0) - 32768.0
        self._tiles[key] = grid
        return grid

    def sample(self, points: Sequence[Point]) -> list[float]:
        z, ts = self.sample_zoom, self.tile_size
        out = []
        for lon, lat in points:
            px, py = lonlat_to_pixel(lon, lat, z, ts)
            fx, fy = px - 0.5, py - 0.5
            x0, y0 = math.floor(fx), math.floor(fy)
            tx, ty = fx - x0, fy - y0
            h = 0.0
            for dx, dy, w in ((0, 0, (1 - tx) * (1 - ty)), (1, 0, tx * (1 - ty)),
                              (0, 1, (1 - tx) * ty), (1, 1, tx * ty)):
                if w == 0.0:
                    continue
                gx, gy = x0 + dx, y0 + dy
                h += w * float(self._tile(z, gx // ts, gy // ts)[gy % ts, gx % ts])
            out.append(h)
        return out
