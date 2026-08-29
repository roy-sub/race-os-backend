"""Build the offline test fixtures.

Run once, with network access, to snapshot a small real slice of Overture roads
and the AWS Terrarium tiles that cover it:

    python tests/build_fixtures.py

The result is checked in, so the test suite proves determinism and validation
against real data without reaching the network in CI.
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_ingest.config import load_config  # noqa: E402
from course_ingest.pipeline import default_sources  # noqa: E402
from course_ingest.sources.terrarium import lonlat_to_pixel  # noqa: E402
from course_ingest.spec import load_spec  # noqa: E402
from course_ingest.stages.s01_ingest import ingest  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
#: See the comment beside the tile loop below.
FIXTURE_DEM_ZOOM = 12
SPEC = ROOT / "tests" / "fixtures" / "test-sprint.yaml"


def main() -> int:
    cfg = load_config()
    roads, dem = default_sources(cfg, sys.argv[1] if len(sys.argv) > 1 else None)
    spec = load_spec(SPEC)
    plan = ingest(spec, cfg)

    bbox = (
        min(plan.bike_bbox[0], plan.run_bbox[0], plan.swim_bbox[0]),
        min(plan.bike_bbox[1], plan.run_bbox[1], plan.swim_bbox[1]),
        max(plan.bike_bbox[2], plan.run_bbox[2], plan.swim_bbox[2]),
        max(plan.bike_bbox[3], plan.run_bbox[3], plan.swim_bbox[3]),
    )
    print(f"fixture bbox {bbox}")

    ways = roads.ways_in_bbox(bbox)
    keep_classes = set(cfg["routing"]["class_cost"]["bike"]) | set(cfg["routing"]["class_cost"]["run"])
    keep_classes = {c for c in keep_classes if
                    cfg["routing"]["class_cost"]["bike"].get(c) is not None
                    or cfg["routing"]["class_cost"]["run"].get(c) is not None}
    ways = [w for w in ways if w.road_class in keep_classes]
    print(f"{len(ways)} ways kept")

    water = roads.water_rings_in_bbox(bbox)
    # Only rings that could plausibly hold the swim: the big ones near the start.
    water = [w for w in water if len(w[2]) >= 8][:60]
    print(f"{len(water)} water rings kept")

    payload = {
        "snapshot_id": roads.snapshot_id,
        "bbox": list(bbox),
        "ways": [
            {
                "way_id": w.way_id,
                "geometry": [[round(x, 6), round(y, 6)] for x, y in w.geometry],
                "road_class": w.road_class,
                "name": w.name,
                "surface": w.surface,
                "connectors": [[c[0], round(c[1], 6)] for c in w.connectors],
                "access_denied": w.access_denied,
                "sources": [list(s) for s in w.sources],
                "flags": list(w.flags),
            }
            for w in ways
        ],
        "water": [
            {"subtype": st, "name": nm, "ring": [[round(x, 6), round(y, 6)] for x, y in ring]}
            for st, nm, ring in water
        ],
    }
    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / "roads.json.gz"
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    # mtime=0 so the archive is byte-identical when rebuilt from identical data.
    with gzip.GzipFile(out, "wb", compresslevel=9, mtime=0) as fh:
        fh.write(blob)
    print(f"roads fixture {out.stat().st_size/1e6:.2f} MB (from {len(blob)/1e6:.2f} MB raw)")

    # DEM tiles covering the bbox.
    #
    # Stored at FIXTURE_DEM_ZOOM rather than production's sampling zoom: z14
    # over this bbox is 340 tiles and 18 MB, which is too much to check in, and
    # nothing the test suite asserts depends on DEM resolution. Determinism,
    # the validation rules and the end-to-end shape all hold identically on a
    # coarser terrain model.
    tile_dir = FIXTURES / "dem"
    for stale in tile_dir.glob("*.png"):
        stale.unlink()
    tile_dir.mkdir(parents=True, exist_ok=True)
    z, ts = FIXTURE_DEM_ZOOM, dem.tile_size
    x0, y0 = lonlat_to_pixel(bbox[0], bbox[3], z, ts)
    x1, y1 = lonlat_to_pixel(bbox[2], bbox[1], z, ts)
    total = 0
    for tx in range(int(x0 // ts) - 1, int(x1 // ts) + 2):
        for ty in range(int(y0 // ts) - 1, int(y1 // ts) + 2):
            data = dem.tile_bytes(z, tx, ty)
            (tile_dir / f"{z}_{tx}_{ty}.png").write_bytes(data)
            total += len(data)
    print(f"{len(list(tile_dir.glob('*.png')))} DEM tiles, {total/1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
