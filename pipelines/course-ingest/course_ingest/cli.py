"""Command-line interface.

    course-ingest generate specs/01-tramuntana-full.yaml
    course-ingest validate out/bundles/tramuntana-full.bundle.json
    course-ingest regenerate-all
    course-ingest visual-check
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .pipeline import default_sources, generate
from .spec import load_all_specs, load_spec
from .stages.s10_emit import BundleRejected
from .validate import validate_file

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SPECS = PACKAGE_ROOT / "specs"
DEFAULT_OUT = PACKAGE_ROOT / "out"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _summary(result) -> str:
    legs = result.bundle["course_bundle_legs"]
    parts = []
    for leg in legs:
        parts.append(
            f"{leg['leg'].lower()} {leg['distance_m']/1000:.2f} km "
            f"+{leg['elevation_gain_m']} m ({leg['node_count']} nodes, {leg['surface_quality']})"
        )
    return " | ".join(parts)


def cmd_generate(args) -> int:
    cfg = load_config()
    out_root = Path(args.out)
    roads, dem = default_sources(cfg, args.cache)
    failures = 0
    for spec_path in args.specs:
        spec = load_spec(spec_path)
        _log(f"\n=== {spec.name} ({spec.course_id}) -- {spec.distance_type}, {spec.character}")
        try:
            result = generate(
                spec,
                out_root / "bundles",
                cfg=cfg,
                roads=roads,
                dem=dem,
                dry_run=args.dry_run,
                log=_log,
            )
        except BundleRejected as exc:
            failures += 1
            _log(exc.report.render())
            continue
        _log(_summary(result))
        _log(result.emit_result.report.render())
        _log(
            f"  packed {result.emit_result.packed_bytes/1024:.1f} KB -> "
            f"{result.emit_result.packed_path}"
        )
        if not args.dry_run and not args.skip_terrain:
            _write_terrain(result, dem, out_root, cfg)
        if not args.dry_run and not args.skip_visuals:
            from .render import render_course

            paths = render_course(result, out_root / "visual-check")
            _log(f"  visuals -> {', '.join(str(p.name) for p in paths)}")
    return 1 if failures else 0


def _write_terrain(result, dem, out_root: Path, cfg) -> None:
    from .geo import bbox_of, expand_bbox
    from .terrain_extract import write_extract

    pts = [p for leg in result.legs.values() for p in leg.nodes]
    bbox = expand_bbox(bbox_of(pts), 0.01)
    extract = write_extract(
        dem,
        bbox,
        out_root / "terrain" / f"{result.spec.slug}.pmtiles",
        result.spec.name,
        result.bundle["course_bundle"]["attribution"],
    )
    _log(
        f"  terrain {extract.tile_count} tiles z{extract.min_zoom}-{extract.max_zoom}, "
        f"{extract.bytes_written/1e6:.1f} MB -> {extract.path}"
    )


def cmd_validate(args) -> int:
    cfg = load_config()
    failures = 0
    for path in args.bundles:
        report = validate_file(path, cfg)
        print(report.render())
        if not report.ok:
            failures += 1
    return 1 if failures else 0


def cmd_regenerate_all(args) -> int:
    paths = sorted(Path(args.specs_dir).glob("*.yaml"))
    if not paths:
        print(f"no specs found in {args.specs_dir}", file=sys.stderr)
        return 1
    # Parse every spec up front so a typo in the ninth file fails before the
    # first course spends two minutes routing.
    load_all_specs(args.specs_dir)
    ns = argparse.Namespace(
        specs=paths,
        out=args.out,
        cache=args.cache,
        dry_run=args.dry_run,
        skip_terrain=args.skip_terrain,
        skip_visuals=args.skip_visuals,
    )
    return cmd_generate(ns)


def cmd_visual_check(args) -> int:
    from .render import render_contact_sheet

    out = Path(args.out)
    path = render_contact_sheet(out / "bundles", out / "visual-check")
    print(f"contact sheet -> {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="course-ingest", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--out", default=str(DEFAULT_OUT), help="output root directory")
        sp.add_argument("--cache", default=None, help="blob cache directory")
        sp.add_argument("--dry-run", action="store_true", help="validate without writing artefacts")
        sp.add_argument("--skip-terrain", action="store_true", help="skip the PMTiles extract")
        sp.add_argument("--skip-visuals", action="store_true", help="skip map and profile images")

    g = sub.add_parser("generate", help="generate one or more course bundles")
    g.add_argument("specs", nargs="+", type=Path)
    common(g)
    g.set_defaults(func=cmd_generate)

    v = sub.add_parser("validate", help="validate emitted bundle fixtures")
    v.add_argument("bundles", nargs="+", type=Path)
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("regenerate-all", help="regenerate every course in specs/")
    r.add_argument("--specs-dir", default=str(DEFAULT_SPECS))
    common(r)
    r.set_defaults(func=cmd_regenerate_all)

    c = sub.add_parser("visual-check", help="render the nine-course contact sheet")
    c.add_argument("--out", default=str(DEFAULT_OUT))
    c.set_defaults(func=cmd_visual_check)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
