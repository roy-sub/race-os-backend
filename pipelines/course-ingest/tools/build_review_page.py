"""Build the three-course visual check as a single reviewable page.

    python tools/build_review_page.py [out_dir]

One page carrying, per course: the static map and elevation profile, the
delivered distances and gain against nominal, the terrain character verdict,
the cut-off ladder, and the margin spot-check for two athlete profiles.

Images are embedded as data URIs so the page is one self-contained file.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from course_ingest.config import load_config  # noqa: E402
from course_ingest.validate import validate_bundle  # noqa: E402
from margin_check import PROFILES, evaluate  # noqa: E402

LEG_COLOR = {"SWIM": "#2b8cbe", "BIKE": "#d95f0e", "RUN": "#31a354"}


def collect(out_dir: Path):
    cfg = load_config()
    courses = []
    for path in sorted(glob.glob(str(out_dir / "bundles" / "*.bundle.json"))):
        bundle = json.load(open(path, encoding="utf-8"))
        slug = bundle["course"]["slug"]
        packed = out_dir / "bundles" / f"{slug}.bundle.bin"
        terrain = out_dir / "terrain" / f"{slug}.pmtiles"
        image = out_dir / "visual-check" / f"{slug}.png"
        report = validate_bundle(
            bundle, cfg, packed_bytes=packed.stat().st_size if packed.exists() else None
        )
        nominal = cfg["course"]["distances"][bundle["course"]["distance_type"]]
        legs = []
        for leg in ("SWIM", "BIKE", "RUN"):
            row = next(l for l in bundle["course_bundle_legs"] if l["leg"] == leg)
            target = float(nominal[f"{leg.lower()}_m"])
            profile = bundle["course_bundle"]["elevation_profile"]["legs"][leg]
            legs.append({
                "leg": leg,
                "distance_m": row["distance_m"],
                "target_m": target,
                "deviation_pct": 100.0 * (row["distance_m"] - target) / target,
                "gain_m": row["elevation_gain_m"],
                "gain_raw_m": profile["gain_m_raw_nodes"],
                "gain_per_km": row["elevation_gain_m"] / (row["distance_m"] / 1000.0),
                "nodes": row["node_count"],
                "surface": row["surface_quality"],
                "laps": profile["laps"],
                "min_m": profile["min_m"], "max_m": profile["max_m"],
            })
        character = bundle["provenance_detail"]["character"]
        gradients = {}
        for f in report.findings:
            if f.rule.startswith("gradient_"):
                gradients[f.rule.split("_", 1)[1].upper()] = f.message
        courses.append({
            "bundle": bundle,
            "slug": slug,
            "legs": legs,
            "character": character,
            "gradient_notes": gradients,
            "report": report,
            "packed_bytes": packed.stat().st_size if packed.exists() else 0,
            "terrain_bytes": terrain.stat().st_size if terrain.exists() else 0,
            "image_b64": base64.b64encode(image.read_bytes()).decode() if image.exists() else "",
            "margins": {k: evaluate(bundle, k) for k in PROFILES},
        })
    return cfg, courses


def esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def hhmm(minutes: float) -> str:
    return f"{int(minutes) // 60}:{int(round(minutes)) % 60:02d}"


def render(cfg, courses) -> str:
    from page_template import build_html

    return build_html(cfg, courses, esc=esc, hhmm=hhmm, leg_color=LEG_COLOR)


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "out"
    cfg, courses = collect(out_dir)
    if not courses:
        print(f"no bundles in {out_dir/'bundles'}", file=sys.stderr)
        return 1
    target = out_dir / "visual-check" / "three-course-review.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(cfg, courses), encoding="utf-8")
    print(f"{len(courses)} courses -> {target}  ({target.stat().st_size/1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
