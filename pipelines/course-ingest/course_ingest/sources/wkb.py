"""Minimal WKB reader for the geometry types Overture delivers.

Only LineString, Polygon, MultiLineString and MultiPolygon are needed. Written
out rather than pulling in a geometry library because the pipeline needs exactly
this and nothing else, and a smaller dependency surface is a smaller
determinism surface.
"""
from __future__ import annotations

import struct

from ..geo import Point

_POINT, _LINESTRING, _POLYGON, _MULTIPOINT, _MULTILINESTRING, _MULTIPOLYGON, _COLLECTION = range(1, 8)


class WkbError(ValueError):
    pass


class _Reader:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0

    def byte(self) -> int:
        b = self.buf[self.pos]
        self.pos += 1
        return b

    def uint32(self, little: bool) -> int:
        v = struct.unpack_from("<I" if little else ">I", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def doubles(self, little: bool, n: int) -> tuple[float, ...]:
        fmt = ("<" if little else ">") + "d" * n
        v = struct.unpack_from(fmt, self.buf, self.pos)
        self.pos += 8 * n
        return v


def _read_geom(r: _Reader) -> tuple[int, object]:
    little = r.byte() == 1
    raw = r.uint32(little)
    # Strip SRID / Z / M flags: Overture emits plain 2D, but be tolerant.
    has_srid = bool(raw & 0x20000000)
    dims = 2 + (1 if raw & 0x80000000 else 0) + (1 if raw & 0x40000000 else 0)
    gtype = raw & 0xFF
    if raw > 1000 and gtype == raw & 0xFFF:
        # ISO WKB encodes Z as +1000, M as +2000, ZM as +3000.
        iso = raw % 1000
        if iso in range(1, 8):
            dims = 2 + (1 if 1000 <= raw < 3000 else 0) + (1 if raw >= 2000 else 0)
            gtype = iso
    if has_srid:
        r.uint32(little)

    if gtype == _POINT:
        c = r.doubles(little, dims)
        return gtype, (c[0], c[1])
    if gtype == _LINESTRING:
        n = r.uint32(little)
        flat = r.doubles(little, n * dims)
        return gtype, [(flat[i * dims], flat[i * dims + 1]) for i in range(n)]
    if gtype == _POLYGON:
        nrings = r.uint32(little)
        rings = []
        for _ in range(nrings):
            n = r.uint32(little)
            flat = r.doubles(little, n * dims)
            rings.append([(flat[i * dims], flat[i * dims + 1]) for i in range(n)])
        return gtype, rings
    if gtype in (_MULTILINESTRING, _MULTIPOLYGON, _COLLECTION, _MULTIPOINT):
        n = r.uint32(little)
        parts = []
        for _ in range(n):
            parts.append(_read_geom(r)[1])
        return gtype, parts
    raise WkbError(f"unsupported WKB geometry type {gtype}")


def read_linestring(data: bytes) -> list[Point]:
    gtype, geom = _read_geom(_Reader(data))
    if gtype == _LINESTRING:
        return geom  # type: ignore[return-value]
    if gtype == _MULTILINESTRING and geom:
        # Take the longest part; Overture segments are single-part in practice.
        return max(geom, key=len)  # type: ignore[arg-type]
    raise WkbError(f"expected LineString, got type {gtype}")


def read_exterior_rings(data: bytes) -> list[list[Point]]:
    """Exterior rings of a Polygon or MultiPolygon."""
    gtype, geom = _read_geom(_Reader(data))
    if gtype == _POLYGON:
        return [geom[0]] if geom else []  # type: ignore[index]
    if gtype == _MULTIPOLYGON:
        return [poly[0] for poly in geom if poly]  # type: ignore[union-attr]
    if gtype == _LINESTRING:
        return [geom]  # type: ignore[list-item]
    return []
