"""Compact bundle container.

The packed bundle is the artefact behind `course_bundles.bundle_asset_key`: one
file the map, the solver and the FIT export all read, under the 400 KB budget so
the map draws the route immediately from memory while terrain streams behind it.

Layout
------
    magic        b"RCOSB1"                       6 bytes
    header_len   uint32 little-endian            4 bytes
    header       canonical JSON, UTF-8           header_len bytes
    legs         in the fixed order SWIM, BIKE, RUN:
                     count      uint32 LE
                     lon_e6     zigzag varint deltas (first value absolute)
                     lat_e6     zigzag varint deltas
                     elev_cm    zigzag varint deltas

Coordinates are stored at 1e-6 degrees (~0.11 m) and elevation at 1 cm, both far
finer than the DEM's real accuracy. Deltas between 10 m nodes are small, so the
varints are one or two bytes each and a 180 km leg costs roughly 100 KB.

No compression is used. zlib output can vary between library builds, and this
format has to be byte-identical across machines, not merely small.
"""
from __future__ import annotations

import json
import struct
from typing import Any, Sequence

MAGIC = b"RCOSB1"
LEG_ORDER = ("SWIM", "BIKE", "RUN")


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def _unzigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def encode_varints(values: Sequence[int]) -> bytes:
    out = bytearray()
    for v in values:
        z = _zigzag(int(v))
        while True:
            byte = z & 0x7F
            z >>= 7
            if z:
                out.append(byte | 0x80)
            else:
                out.append(byte)
                break
    return bytes(out)


def decode_varints(data: bytes, count: int, offset: int = 0) -> tuple[list[int], int]:
    out: list[int] = []
    pos = offset
    for _ in range(count):
        shift = 0
        acc = 0
        while True:
            byte = data[pos]
            pos += 1
            acc |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        out.append(_unzigzag(acc))
    return out, pos


def _deltas(values: Sequence[int]) -> list[int]:
    out = []
    prev = 0
    for v in values:
        out.append(v - prev)
        prev = v
    return out


def _undeltas(values: Sequence[int]) -> list[int]:
    out = []
    acc = 0
    for v in values:
        acc += v
        out.append(acc)
    return out


def canonical_json(payload: Any) -> bytes:
    """Deterministic JSON: sorted keys, no insignificant whitespace, ASCII-safe."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def pack(header: dict[str, Any], legs: dict[str, Sequence[tuple[float, float, float]]]) -> bytes:
    body = bytearray()
    for leg in LEG_ORDER:
        nodes = legs.get(leg) or []
        body += struct.pack("<I", len(nodes))
        if not nodes:
            continue
        lon = [int(round(p[0] * 1_000_000)) for p in nodes]
        lat = [int(round(p[1] * 1_000_000)) for p in nodes]
        elev = [int(round(p[2] * 100)) for p in nodes]
        body += encode_varints(_deltas(lon))
        body += encode_varints(_deltas(lat))
        body += encode_varints(_deltas(elev))
    head = canonical_json(header)
    return MAGIC + struct.pack("<I", len(head)) + head + bytes(body)


def unpack(data: bytes) -> tuple[dict[str, Any], dict[str, list[tuple[float, float, float]]]]:
    if data[:6] != MAGIC:
        raise ValueError("not a RaceOS course bundle")
    (head_len,) = struct.unpack_from("<I", data, 6)
    header = json.loads(data[10 : 10 + head_len].decode("utf-8"))
    pos = 10 + head_len
    legs: dict[str, list[tuple[float, float, float]]] = {}
    for leg in LEG_ORDER:
        (count,) = struct.unpack_from("<I", data, pos)
        pos += 4
        if count == 0:
            legs[leg] = []
            continue
        lon_d, pos = decode_varints(data, count, pos)
        lat_d, pos = decode_varints(data, count, pos)
        elev_d, pos = decode_varints(data, count, pos)
        lon = _undeltas(lon_d)
        lat = _undeltas(lat_d)
        elev = _undeltas(elev_d)
        legs[leg] = [
            (lon[i] / 1_000_000, lat[i] / 1_000_000, elev[i] / 100.0) for i in range(count)
        ]
    return header, legs


def ewkt_linestring_z(nodes: Sequence[tuple[float, float, float]], srid: int = 4326) -> str:
    """EWKT for a LineStringZ, directly insertable into a PostGIS
    `geometry(LineStringZ, 4326)` column.

    Fixed-precision formatting, never `repr`: the fixture files have to be
    byte-identical between runs and between platforms.
    """
    coords = ",".join(f"{x:.6f} {y:.6f} {z:.2f}" for x, y, z in nodes)
    return f"SRID={srid};LINESTRING Z ({coords})"
