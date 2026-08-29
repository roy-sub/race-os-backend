"""Overture Maps road source.

Reads the Overture `transportation/segment` theme (OSM-derived, ODbL-1.0)
directly from public S3 as GeoParquet, using HTTP range requests and row-group
bbox statistics so a course bounding box costs tens of megabytes rather than the
72 GB the theme weighs globally.

Why this and not a hosted routing API
-------------------------------------
The pipeline's headline requirement is that the same seed spec produces
byte-identical output on every run, this season and next. A hosted router
(OSRM / Valhalla / openrouteservice) re-imports OpenStreetMap continuously and
offers no way to pin a snapshot, so an identical request returns different
geometry over time; it is also rate-limited, which makes a nine-course rebuild
slow and flaky. Pinning an Overture release makes the road network an immutable
input. It also carries `road_surface`, `names.primary` and `sources[].license`
in the same row, which the bundle needs for `surface_quality`, segment naming
and licence compliance -- three more round trips against a routing API, two of
which have no API at all.
"""
from __future__ import annotations

import io
import json
import re
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Sequence

import pyarrow.parquet as pq
import requests

from ..geo import Point
from .base import RoadSource, RoadWay, SourceError
from .cache import BlobCache
from .http_range import HttpRangeFile
from .retry import check_status, with_retry
from .wkb import read_exterior_rings, read_linestring

_SEGMENT_COLUMNS = [
    "id",
    "names",
    "subtype",
    "class",
    "road_surface",
    "road_flags",
    "access_restrictions",
    "connectors",
    "sources",
    "geometry",
    "bbox",
]
_WATER_COLUMNS = ["id", "names", "subtype", "class", "geometry", "bbox"]


class OvertureRoadSource(RoadSource):
    def __init__(self, cfg, cache: BlobCache) -> None:
        roads = cfg["sources"]["roads"]
        self.release: str = roads["release"]
        self.bucket_url: str = roads["bucket_url"].rstrip("/")
        self._segment_prefix = roads["segment_prefix"].format(release=self.release)
        self._water_prefix = roads["water_prefix"].format(release=self.release)
        self._timeout = int(roads["request_timeout_s"])
        self._parallel = int(roads["max_parallel_footer_reads"])
        self._cache = cache
        self._session = requests.Session()
        self.snapshot_id = f"overture:{self.release}"

    # ---------------------------------------------------------------- listing

    def _list_keys(self, prefix: str) -> list[str]:
        cached = self._cache.get("overture-listing", f"{self.bucket_url}|{prefix}")
        if cached is not None:
            return json.loads(cached.decode("utf-8"))
        keys: list[str] = []
        token: str | None = None
        while True:
            url = (
                f"{self.bucket_url}/?list-type=2&prefix={urllib.parse.quote(prefix, safe='')}"
                f"&max-keys=1000"
            )
            if token:
                url += "&continuation-token=" + urllib.parse.quote(token, safe="")
            resp = with_retry(
                lambda: check_status(self._session.get(url, timeout=self._timeout))
            )
            body = resp.text
            keys.extend(k for k in re.findall(r"<Key>(.*?)</Key>", body) if k.endswith(".parquet"))
            m = re.search(r"<NextContinuationToken>(.*?)</NextContinuationToken>", body)
            if not m:
                break
            token = m.group(1)
        keys.sort()
        if not keys:
            raise SourceError(f"no parquet files under {prefix}")
        self._cache.put("overture-listing", f"{self.bucket_url}|{prefix}", json.dumps(keys).encode())
        return keys

    # --------------------------------------------------------------- manifest

    def _row_group_manifest(self, prefix: str) -> dict[str, list[list[float]]]:
        """Per-row-group bbox extents for every file under `prefix`.

        Roughly 2 MB of footer per file, read once and cached. This is what makes
        a bbox query cost one pass over 16 384 row-group summaries instead of a
        scan.
        """
        cache_key = f"{self.bucket_url}|{prefix}|manifest-v1"
        cached = self._cache.get("overture-manifest", cache_key)
        if cached is not None:
            return json.loads(cached.decode("utf-8"))

        keys = self._list_keys(prefix)

        def one(key: str):
            fh = HttpRangeFile(f"{self.bucket_url}/{urllib.parse.quote(key)}", self._session, self._timeout)
            pf = pq.ParquetFile(io.BufferedReader(fh, buffer_size=1 << 20))
            md = pf.metadata
            leaf = {}
            for i in range(md.num_columns):
                name = md.schema.column(i).name
                if name in ("xmin", "xmax", "ymin", "ymax") and name not in leaf:
                    leaf[name] = i
            if len(leaf) != 4:
                raise SourceError(f"{key}: bbox statistics not found in parquet footer")
            rows = []
            for r in range(md.num_row_groups):
                rg = md.row_group(r)
                rows.append(
                    [
                        float(r),
                        float(rg.num_rows),
                        rg.column(leaf["xmin"]).statistics.min,
                        rg.column(leaf["xmax"]).statistics.max,
                        rg.column(leaf["ymin"]).statistics.min,
                        rg.column(leaf["ymax"]).statistics.max,
                    ]
                )
            return key, rows

        manifest: dict[str, list[list[float]]] = {}
        with ThreadPoolExecutor(self._parallel) as pool:
            for key, rows in pool.map(one, keys):
                manifest[key] = rows
        self._cache.put("overture-manifest", cache_key, json.dumps(manifest, sort_keys=True).encode())
        return manifest

    # ------------------------------------------------------------------ reads

    def _read_bbox(self, prefix: str, bbox, columns: Sequence[str]):
        """Every row in `bbox`, cached.

        A course bbox is roughly 80 MB of Parquet row groups. Caching the
        assembled result rather than only the footers makes a re-run cost
        nothing and immune to a dropped connection -- which matters, because the
        determinism proof regenerates every course twice in a row.
        """
        import io

        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.parquet as _pq

        cache_key = "|".join([
            self.bucket_url, prefix,
            ",".join(f"{v:.6f}" for v in bbox),
            ",".join(columns), "bbox-v1",
        ])
        cached = self._cache.get("overture-bbox", cache_key)
        if cached is not None:
            return _pq.read_table(io.BytesIO(cached))

        table = self._read_bbox_uncached(prefix, bbox, columns)
        buf = io.BytesIO()
        _pq.write_table(table, buf, compression="zstd")
        self._cache.put("overture-bbox", cache_key, buf.getvalue())
        return table

    def _read_bbox_uncached(self, prefix: str, bbox, columns: Sequence[str]):
        import pyarrow as pa
        import pyarrow.compute as pc

        minx, miny, maxx, maxy = bbox
        manifest = self._row_group_manifest(prefix)
        wanted: dict[str, list[int]] = defaultdict(list)
        for key in sorted(manifest):
            for row, _n, x0, x1, y0, y1 in manifest[key]:
                if x1 >= minx and x0 <= maxx and y1 >= miny and y0 <= maxy:
                    wanted[key].append(int(row))
        if not wanted:
            raise SourceError(f"no Overture row groups intersect bbox {bbox} under {prefix}")

        tables = []
        for key in sorted(wanted):
            fh = HttpRangeFile(f"{self.bucket_url}/{urllib.parse.quote(key)}", self._session, self._timeout)
            pf = pq.ParquetFile(io.BufferedReader(fh, buffer_size=1 << 20))
            tables.append(pf.read_row_groups(sorted(wanted[key]), columns=list(columns)))
        table = pa.concat_tables(tables)

        bx = table["bbox"]
        mask = pc.and_(
            pc.and_(
                pc.greater_equal(pc.struct_field(bx, "xmax"), minx),
                pc.less_equal(pc.struct_field(bx, "xmin"), maxx),
            ),
            pc.and_(
                pc.greater_equal(pc.struct_field(bx, "ymax"), miny),
                pc.less_equal(pc.struct_field(bx, "ymin"), maxy),
            ),
        )
        return table.filter(mask)

    # ---------------------------------------------------------------- public

    def ways_in_bbox(self, bbox) -> list[RoadWay]:
        import pyarrow.compute as pc

        table = self._read_bbox(self._segment_prefix, bbox, _SEGMENT_COLUMNS)
        table = table.filter(pc.equal(table["subtype"], "road"))

        ids = table["id"].to_pylist()
        names = table["names"].to_pylist()
        classes = table["class"].to_pylist()
        surfaces = table["road_surface"].to_pylist()
        flags = table["road_flags"].to_pylist()
        access = table["access_restrictions"].to_pylist()
        connectors = table["connectors"].to_pylist()
        sources = table["sources"].to_pylist()
        geoms = table["geometry"].to_pylist()

        ways: list[RoadWay] = []
        for i in range(table.num_rows):
            try:
                line = read_linestring(geoms[i])
            except Exception:
                continue
            if len(line) < 2:
                continue
            conns = tuple(
                (c["connector_id"], float(c["at"]))
                for c in sorted(connectors[i] or [], key=lambda c: (float(c["at"]), c["connector_id"]))
            )
            ways.append(
                RoadWay(
                    way_id=ids[i],
                    geometry=tuple(line),
                    road_class=classes[i] or "unknown",
                    name=_primary_name(names[i]),
                    surface=_dominant_surface(surfaces[i]),
                    connectors=conns,
                    access_denied=_is_denied(access[i]),
                    sources=_source_pairs(sources[i]),
                    flags=_flag_set(flags[i]),
                )
            )
        ways.sort(key=lambda w: w.way_id)
        return ways

    def water_rings_in_bbox(self, bbox):
        table = self._read_bbox(self._water_prefix, bbox, _WATER_COLUMNS)
        subtypes = table["subtype"].to_pylist()
        names = table["names"].to_pylist()
        geoms = table["geometry"].to_pylist()
        out: list[tuple[str, str | None, tuple[Point, ...]]] = []
        for i in range(table.num_rows):
            try:
                rings = read_exterior_rings(geoms[i])
            except Exception:
                continue
            for ring in rings:
                if len(ring) >= 4:
                    out.append((subtypes[i] or "unknown", _primary_name(names[i]), tuple(ring)))
        out.sort(key=lambda r: (-len(r[2]), r[0], r[1] or ""))
        return out


# --------------------------------------------------------------- row helpers


def _primary_name(names: Any) -> str | None:
    if not names:
        return None
    value = names.get("primary") if isinstance(names, dict) else None
    return value or None


def _dominant_surface(rules: Any) -> str | None:
    """The surface covering the most of the way.

    Overture expresses surface as a list of rules with optional `between`
    ranges. A rule with no range covers the whole way.
    """
    if not rules:
        return None
    best_value, best_span = None, -1.0
    for rule in rules:
        between = rule.get("between")
        span = 1.0 if not between else max(0.0, float(between[1]) - float(between[0]))
        if span > best_span or (span == best_span and best_value is not None and str(rule["value"]) < best_value):
            best_span = span
            best_value = str(rule["value"])
    return best_value


def _is_denied(rules: Any) -> bool:
    if not rules:
        return False
    for rule in rules:
        if rule.get("access_type") == "denied" and not rule.get("when"):
            return True
    return False


def _flag_set(rules: Any) -> tuple[str, ...]:
    if not rules:
        return ()
    out: set[str] = set()
    for rule in rules:
        for value in rule.get("values") or ():
            out.add(str(value))
    return tuple(sorted(out))


def _source_pairs(sources: Any) -> tuple[tuple[str, str], ...]:
    if not sources:
        return ()
    pairs = {
        (str(s.get("dataset") or "unknown"), str(s.get("license") or "unknown"))
        for s in sources
    }
    return tuple(sorted(pairs))


class FixtureRoadSource(RoadSource):
    """Road source backed by a checked-in JSON fixture.

    Used by the offline test suite so determinism and validation can be proven
    in CI without network access, against real Overture-shaped data.
    """

    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        if path.suffix == ".gz":
            import gzip

            payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.snapshot_id = payload["snapshot_id"]
        self._ways = [
            RoadWay(
                way_id=w["way_id"],
                geometry=tuple((float(x), float(y)) for x, y in w["geometry"]),
                road_class=w["road_class"],
                name=w.get("name"),
                surface=w.get("surface"),
                connectors=tuple((c[0], float(c[1])) for c in w["connectors"]),
                access_denied=bool(w.get("access_denied", False)),
                sources=tuple((s[0], s[1]) for s in w.get("sources", [])),
                flags=tuple(w.get("flags", ())),
            )
            for w in payload["ways"]
        ]
        self._water = [
            (r["subtype"], r.get("name"), tuple((float(x), float(y)) for x, y in r["ring"]))
            for r in payload.get("water", [])
        ]

    @staticmethod
    def _clip(bbox, geometry: Iterable[Point]) -> bool:
        minx, miny, maxx, maxy = bbox
        xs = [p[0] for p in geometry]
        ys = [p[1] for p in geometry]
        return max(xs) >= minx and min(xs) <= maxx and max(ys) >= miny and min(ys) <= maxy

    def ways_in_bbox(self, bbox) -> list[RoadWay]:
        return [w for w in self._ways if self._clip(bbox, w.geometry)]

    def water_rings_in_bbox(self, bbox):
        return [r for r in self._water if self._clip(bbox, r[2])]
