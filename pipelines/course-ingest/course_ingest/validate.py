"""Bundle validation.

A bundle that fails any rule here is rejected, not published. The rules operate
on the assembled bundle dict, so `course-ingest validate <bundle.json>` checks
exactly what `generate` checked, and a hand-edited fixture cannot slip past.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .geo import cumulative_m

LEG_ORDER = ("SWIM", "BIKE", "RUN")
_EWKT = re.compile(r"^SRID=4326;LINESTRING Z \((.*)\)$", re.DOTALL)


@dataclass
class Finding:
    rule: str
    severity: str  # "error" | "info"
    message: str


@dataclass
class ValidationReport:
    course_id: str
    findings: list[Finding]

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines = [f"Validation report -- {self.course_id}"]
        width = max((len(f.rule) for f in self.findings), default=4)
        for f in self.findings:
            mark = "FAIL" if f.severity == "error" else "ok  "
            lines.append(f"  [{mark}] {f.rule.ljust(width)}  {f.message}")
        lines.append(f"  => {'PASS' if self.ok else f'REJECTED ({len(self.errors)} failing rules)'}")
        return "\n".join(lines)


def parse_ewkt(geometry: str) -> list[tuple[float, float, float]]:
    m = _EWKT.match(geometry.strip())
    if not m:
        raise ValueError("leg geometry is not SRID=4326;LINESTRING Z (...)")
    out = []
    for triple in m.group(1).split(","):
        x, y, z = triple.split()
        out.append((float(x), float(y), float(z)))
    return out


def validate_bundle(bundle: dict[str, Any], cfg: Config, packed_bytes: int | None = None) -> ValidationReport:
    vcfg = cfg["course"]["validation"]
    tolerance = cfg["course"]["distance_tolerance"]
    nominal = cfg["course"]["distances"]
    findings: list[Finding] = []

    cb = bundle["course_bundle"]
    course = bundle["course"]
    course_id = cb["course_id"]
    distance_type = course["distance_type"]
    legs = {leg["leg"]: leg for leg in bundle["course_bundle_legs"]}

    def add(rule: str, ok: bool, message: str) -> None:
        findings.append(Finding(rule, "info" if ok else "error", message))

    # 1. All three legs present, each within tolerance of nominal distance.
    missing = [leg for leg in LEG_ORDER if leg not in legs]
    add("legs_present", not missing, "all three legs present" if not missing else f"missing {missing}")

    for leg in LEG_ORDER:
        if leg not in legs:
            continue
        target = float(nominal[distance_type][f"{leg.lower()}_m"])
        actual = float(legs[leg]["distance_m"])
        tol = float(tolerance[leg])
        deviation = (actual - target) / target
        add(
            f"distance_{leg.lower()}",
            abs(deviation) <= tol,
            f"{actual/1000:.3f} km vs nominal {target/1000:.3f} km "
            f"({deviation*100:+.2f}%, tolerance +/-{tol*100:.0f}%)",
        )

    # 2. Barrier ordering chronologically sane.
    barriers = cb["barriers"]
    add(
        "barriers_present",
        len(barriers) >= int(vcfg["min_barriers"]),
        f"{len(barriers)} barriers (minimum {vcfg['min_barriers']})",
    )
    km_slack = float(vcfg["km_bound_tolerance_km"])
    minutes = [float(b["limit_minutes_from_start"]) for b in barriers]
    monotonic = all(minutes[i] < minutes[i + 1] for i in range(len(minutes) - 1))
    leg_indices = [LEG_ORDER.index(b["leg"]) for b in barriers]
    legs_ordered = all(leg_indices[i] <= leg_indices[i + 1] for i in range(len(leg_indices) - 1))
    add(
        "barrier_order",
        monotonic and legs_ordered,
        f"limits {minutes} across legs {[b['leg'] for b in barriers]}",
    )
    for b in barriers:
        leg = b["leg"]
        if leg in legs:
            within = -km_slack <= float(b["km"]) <= float(legs[leg]["distance_m"]) / 1000.0 + km_slack
            add(f"barrier_km_{b['name']}", within, f"{b['km']} km on {leg}")

    # 3. Aid-station km within leg distance.
    bad_aid = [
        a
        for a in cb["aid_stations"]
        if a["leg"] not in legs
        or not -km_slack <= float(a["km"]) <= float(legs[a["leg"]]["distance_m"]) / 1000.0 + km_slack
    ]
    add(
        "aid_station_km",
        not bad_aid,
        f"{len(cb['aid_stations'])} stations, all within leg distance"
        if not bad_aid
        else f"{len(bad_aid)} outside leg distance: {[a['name'] for a in bad_aid][:4]}",
    )
    bad_wp = [
        w
        for w in cb["waypoints"]
        if w["leg"] not in legs
        or not -km_slack <= float(w["km"]) <= float(legs[w["leg"]]["distance_m"]) / 1000.0 + km_slack
    ]
    add(
        "waypoint_km",
        not bad_wp,
        f"{len(cb['waypoints'])} waypoints, all within leg distance"
        if not bad_wp
        else f"{len(bad_wp)} outside leg distance: {[w['name'] for w in bad_wp][:4]}",
    )
    aid_only = {a.get("type", "aid") for a in cb["aid_stations"]}
    add(
        "aid_stations_pure",
        aid_only <= {"aid"},
        "aid_stations contains aid stations only" if aid_only <= {"aid"} else f"contains {sorted(aid_only)}",
    )

    # 4/5. Node counts, elevation series length, implausible gradients.
    max_grad = float(vcfg["max_abs_node_gradient"])
    max_frac = float(vcfg["max_gradient_outlier_fraction"])
    hard_max = float(vcfg["hard_max_node_gradient"])
    profile_legs = cb["elevation_profile"]["legs"]

    for leg in LEG_ORDER:
        if leg not in legs:
            continue
        coords = parse_ewkt(legs[leg]["geometry"])
        declared = int(legs[leg]["node_count"])
        add(
            f"node_count_{leg.lower()}",
            len(coords) == declared == int(profile_legs[leg]["node_count"]),
            f"geometry {len(coords)}, declared {declared}, profile {profile_legs[leg]['node_count']}",
        )
        pts = [(c[0], c[1]) for c in coords]
        hs = [c[2] for c in coords]
        cum = cumulative_m(pts)
        grads = [
            0.0 if cum[i + 1] - cum[i] <= 0 else (hs[i + 1] - hs[i]) / (cum[i + 1] - cum[i])
            for i in range(len(hs) - 1)
        ]
        outliers = [g for g in grads if abs(g) > max_grad]
        frac = len(outliers) / max(1, len(grads))
        add(
            f"gradient_{leg.lower()}",
            frac <= max_frac,
            f"{len(outliers)}/{len(grads)} nodes over {max_grad:.0%} ({frac:.3%}, limit {max_frac:.0%}); "
            f"max |g| {max((abs(g) for g in grads), default=0.0):.1%}",
        )
        span = max(hs) - min(hs)
        if leg != "SWIM":
            add(
                f"elevation_range_{leg.lower()}",
                span >= float(vcfg["min_leg_elevation_range_m"]),
                f"terrain spans {span:.1f} m over the leg "
                f"(a constant series means the DEM returned nothing usable)",
            )

        worst = max((abs(g) for g in grads), default=0.0)
        add(
            f"hard_gradient_{leg.lower()}",
            worst <= hard_max,
            f"steepest node {worst:.1%} (absolute limit {hard_max:.0%}); "
            "above this the route jumped a valley or crossed a structure the DEM cannot see",
        )

    # 6. Total elevation gain matches declared character.
    character = bundle["provenance_detail"].get("character", {})
    for leg in ("BIKE", "RUN"):
        if leg not in legs:
            continue
        band = character.get(leg)
        if not band:
            continue
        km = float(legs[leg]["distance_m"]) / 1000.0
        gain = float(legs[leg]["elevation_gain_m"])
        gain_per_km = gain / km
        lo, hi = float(band["min_gain_per_km"]), float(band["max_gain_per_km"])
        add(
            f"character_{leg.lower()}",
            lo <= gain_per_km <= hi,
            f"{gain:.0f} m over {km:.1f} km = {gain_per_km:.1f} m/km; "
            f"`{band['character']}` requires {lo:.1f}-{hi:.1f} m/km",
        )

    # 7. Size budget.
    if packed_bytes is not None:
        limit = int(vcfg["max_bundle_bytes"])
        add(
            "bundle_size",
            packed_bytes <= limit,
            f"{packed_bytes/1024:.1f} KB packed (limit {limit/1024:.0f} KB)",
        )

    # 8. Solver preconditions that are cheap to assert here and expensive later.
    add(
        "elevation_source",
        cb["elevation_source"] == "terrain",
        f"elevation_source={cb['elevation_source']!r} (SOLVER_MODEL.md 1.2 requires 'terrain')",
    )
    add(
        "attribution",
        bool(cb["attribution"]) and "OpenStreetMap" in cb["attribution"],
        cb["attribution"] or "<empty>",
    )
    add("provenance", cb["provenance"] in ("OFFICIAL", "CROWD", "ESTIMATED"), cb["provenance"])

    # Segments must tile each leg without gap or overlap: the solver aggregates
    # over [from_km, to_km) and a gap is silently unpaced road.
    for leg in ("BIKE", "RUN"):
        if leg not in legs:
            continue
        segs = [s for s in cb["segments"] if s["leg"] == leg]
        km = float(legs[leg]["distance_m"]) / 1000.0
        gaps: list[str] = []
        if not segs:
            gaps.append("no segments")
        else:
            if abs(segs[0]["from_km"]) > 0.002:
                gaps.append(f"starts at {segs[0]['from_km']}")
            if abs(segs[-1]["to_km"] - km) > 0.05:
                gaps.append(f"ends at {segs[-1]['to_km']} not {km:.3f}")
            for a, b in zip(segs, segs[1:]):
                if abs(a["to_km"] - b["from_km"]) > 0.002:
                    gaps.append(f"gap {a['to_km']}->{b['from_km']}")
        add(
            f"segments_{leg.lower()}",
            not gaps,
            f"{len(segs)} segments tiling 0-{km:.3f} km" if not gaps else "; ".join(gaps[:3]),
        )

    return ValidationReport(course_id=course_id, findings=findings)


def validate_file(path: str | Path, cfg: Config) -> ValidationReport:
    p = Path(path)
    bundle = json.loads(p.read_text(encoding="utf-8"))
    packed = p.with_suffix("").with_suffix(".bundle.bin")
    size = packed.stat().st_size if packed.exists() else None
    return validate_bundle(bundle, cfg, packed_bytes=size)
