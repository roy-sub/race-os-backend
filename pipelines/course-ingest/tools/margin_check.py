"""Cut-off margin spot-check.

    python tools/margin_check.py [bundles_dir]

**This is not the solver, and its numbers are not plan numbers.** It is a
stripped-down reimplementation of SOLVER_MODEL.md's own formulas -- Martin
power-speed integrated over the gradient histogram (§1.1, §I.2), Riegel distance
decay with the Minetti grade factor (§4.3), the CSS chain (§4.4), and the
transition tables (§4.5) -- run in cool conditions with no wind and no heat
factor. It exists to answer one question at build time: do the generated
cut-off ladders bite where they were intended to?

Deliberately omitted, all of which the real solver does: heat and humidity,
wind, altitude, air density, the barrier-protection intensity grid, fuelling,
and every clamp and binding-constraint rule. Expect these figures a few per cent
optimistic against a real solve in real conditions.
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_ingest.geo import cumulative_m  # noqa: E402
from course_ingest.validate import parse_ewkt  # noqa: E402

G = 9.80665
RHO = 1.2
FW = 0.0044
ETA = 0.976
D_PRIME = 15.0
K_SWIM = 0.0012
WETSUIT = 0.955

CRR = {"smooth_asphalt": 0.0040, "typical_road": 0.0050, "rough_chipseal": 0.0065}

#: Three age-group profiles spanning the field. `strong` is roughly
#: SOLVER_MODEL.md §B.2's Athlete M; `first-timer` is the shape of A-F;
#: `mid-pack` sits between them and is the athlete a cut-off question is
#: usually about.
PROFILES = {
    "strong": {
        "label": "Strong age-grouper",
        "level": "improver",
        "ftp_w": 224.0, "mass_kg": 85.0, "cda": 0.280,
        "run_threshold_s_km": 282.0, "css_s_100m": 105.0,
    },
    "mid-pack": {
        "label": "Mid-pack",
        "level": "improver",
        "ftp_w": 195.0, "mass_kg": 83.0, "cda": 0.300,
        "run_threshold_s_km": 312.0, "css_s_100m": 125.0,
    },
    "first-timer": {
        "label": "First-timer",
        "level": "first",
        "ftp_w": 170.0, "mass_kg": 82.0, "cda": 0.320,
        "run_threshold_s_km": 340.0, "css_s_100m": 145.0,
    },
}

#: SOLVER_MODEL.md refers to "the 20-minute clear/tight margin band" (§I.2.3,
#: §4.2.1). Using the model's own band rather than inventing one -- and an
#: absolute band rather than a percentage, because that is what the product
#: shows the athlete.
TIGHT_BAND_MINUTES = 20.0

IF_REF = {
    "improver": {"Full": 0.70, "70.3": 0.78, "Olympic": 0.85, "Sprint": 0.90},
    "first": {"Full": 0.65, "70.3": 0.72, "Olympic": 0.80, "Sprint": 0.85},
}
RIEGEL = {"improver": 1.07, "first": 1.08}
OW_OVERHEAD = {"improver": 8.0, "first": 12.0}
BIKE_COUPLING = {"Full": 0.08, "70.3": 0.05, "Olympic": 0.03, "Sprint": 0.02}
T1 = {
    "improver": {"Full": 9.0, "70.3": 6.5, "Olympic": 3.5, "Sprint": 2.6},
    "first": {"Full": 12.0, "70.3": 9.5, "Olympic": 6.5, "Sprint": 5.6},
}
T2 = {
    "improver": {"Full": 6.0, "70.3": 4.0, "Olympic": 2.0, "Sprint": 1.5},
    "first": {"Full": 8.0, "70.3": 5.0, "Olympic": 2.5, "Sprint": 2.0},
}


def solve_speed(power_w: float, gradient: float, crr: float, mass: float, cda: float) -> float:
    """Bisection on the Martin power balance, 60 fixed iterations (§I.2.4)."""
    wheel = max(0.0, power_w) * ETA
    lo, hi = 0.5, 30.0
    theta = math.atan(gradient)
    for _ in range(60):
        v = (lo + hi) / 2.0
        lhs = (
            0.5 * RHO * (cda + FW) * v * v * v
            + crr * mass * G * math.cos(theta) * v
            + mass * G * math.sin(theta) * v
            + v * (91 + 8.7 * v) * 1e-3
        )
        if lhs < wheel:
            lo = v
        else:
            hi = v
    return min((lo + hi) / 2.0, 20.83)


def minetti_cost(i: float) -> float:
    return 155.4 * i**5 - 30.4 * i**4 - 43.3 * i**3 + 46.3 * i**2 + 19.5 * i + 3.6


def grade_factor(i: float) -> float:
    alpha = 1.0 if i >= 0 else 0.5
    return max(0.85, min(2.00, (minetti_cost(i) / minetti_cost(0.0)) ** alpha))


def _leg(bundle, leg):
    row = next(l for l in bundle["course_bundle_legs"] if l["leg"] == leg)
    coords = parse_ewkt(row["geometry"])
    cum = cumulative_m([(x, y) for x, y, _ in coords])
    return row, cum, [z for _, _, z in coords]


def bike_minutes(bundle, profile) -> float:
    row, cum, hs = _leg(bundle, "BIKE")
    p = PROFILES[profile]
    base = p["ftp_w"] * IF_REF[p["level"]][bundle["course"]["distance_type"]]
    crr = CRR[row["surface_quality"]]
    histogram: dict[float, float] = {}
    for i in range(len(hs) - 1):
        ds = cum[i + 1] - cum[i]
        if ds <= 0:
            continue
        g = max(-0.30, min(0.30, (hs[i + 1] - hs[i]) / ds))
        b = round(g / 0.0025) * 0.0025
        histogram[b] = histogram.get(b, 0.0) + ds
    seconds = 0.0
    for b, d in sorted(histogram.items()):
        power = base * (1 + 0.12 * math.tanh(b / 0.04))
        seconds += d / solve_speed(power, b, crr, p["mass_kg"], p["cda"])
    return seconds / 60.0


def run_minutes(bundle, profile) -> float:
    _row, cum, hs = _leg(bundle, "RUN")
    p = PROFILES[profile]
    threshold = p["run_threshold_s_km"]
    d_run_km = cum[-1] / 1000.0
    d_threshold_km = 3600.0 / threshold
    decay = (d_run_km / d_threshold_km) ** (RIEGEL[p["level"]] - 1.0)
    coupling = 1 + BIKE_COUPLING[bundle["course"]["distance_type"]]
    pace = threshold * decay * coupling
    seconds = 0.0
    for i in range(len(hs) - 1):
        ds = cum[i + 1] - cum[i]
        if ds <= 0:
            continue
        g = max(-0.30, min(0.30, (hs[i + 1] - hs[i]) / ds))
        seconds += (ds / 1000.0) * pace * grade_factor(g)
    return seconds / 60.0


def swim_minutes(bundle, profile) -> float:
    row = next(l for l in bundle["course_bundle_legs"] if l["leg"] == "SWIM")
    p = PROFILES[profile]
    d = row["distance_m"]
    pace_max = p["css_s_100m"] * (d - D_PRIME) / d
    duration_min = d * pace_max / 100 / 60
    pace = pace_max * (1 + K_SWIM * max(0.0, duration_min - 30.0))
    pace = pace * WETSUIT + OW_OVERHEAD[p["level"]]
    return d / 100 * pace / 60


def evaluate(bundle, profile) -> dict:
    """Splits, barrier ETAs and margins for one course and one athlete."""
    p = PROFILES[profile]
    dist = bundle["course"]["distance_type"]
    swim = swim_minutes(bundle, profile)
    bike = bike_minutes(bundle, profile)
    run = run_minutes(bundle, profile)
    t1 = T1[p["level"]][dist]
    t2 = T2[p["level"]][dist]

    bike_km = next(l for l in bundle["course_bundle_legs"] if l["leg"] == "BIKE")["distance_m"] / 1000
    eta = {"swim_exit": swim, "bike_cutoff": swim + t1 + bike, "finish": swim + t1 + bike + t2 + run}
    for b in bundle["course_bundle"]["barriers"]:
        if b["name"].startswith("bike_km"):
            eta[b["name"]] = swim + t1 + bike * (b["km"] / bike_km)

    barriers = []
    for b in bundle["course_bundle"]["barriers"]:
        limit = float(b["limit_minutes_from_start"])
        barriers.append({
            "name": b["name"], "leg": b["leg"], "km": b["km"],
            "limit_minutes": limit,
            "eta_minutes": round(eta[b["name"]], 1),
            "margin_minutes": round(limit - eta[b["name"]], 1),
            "load_pct": round(100.0 * eta[b["name"]] / limit, 1),
        })
    worst = min(barriers, key=lambda x: x["margin_minutes"])
    return {
        "profile": profile, "label": p["label"],
        "splits": {"swim": round(swim, 1), "t1": t1, "bike": round(bike, 1),
                   "t2": t2, "run": round(run, 1), "total": round(eta["finish"], 1)},
        "barriers": barriers,
        "worst": worst,
        "verdict": "INFEASIBLE" if worst["margin_minutes"] < 0
        else ("TIGHT" if worst["margin_minutes"] < TIGHT_BAND_MINUTES else "CLEAR"),
        "peak_load_pct": round(max(b["load_pct"] for b in barriers), 1),
    }


def main() -> int:
    bundle_dir = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "out" / "bundles")
    paths = sorted(glob.glob(os.path.join(bundle_dir, "*.bundle.json")))
    if not paths:
        print(f"no bundles in {bundle_dir}", file=sys.stderr)
        return 1
    for profile in PROFILES:
        p = PROFILES[profile]
        print(
            f"\n--- {p['label']} (FTP {p['ftp_w']:.0f} W, run threshold "
            f"{p['run_threshold_s_km']:.0f} s/km, CSS {p['css_s_100m']:.0f} s/100m)"
            "  ·  cool conditions, no wind"
        )
        print(f"{'course':22s} {'swim':>6s} {'bike':>7s} {'run':>7s} {'total':>7s} "
              f"{'finish cut':>11s} {'peak load':>10s} | {'worst margin':>34s}")
        for path in paths:
            bundle = json.load(open(path, encoding="utf-8"))
            r = evaluate(bundle, profile)
            s = r["splits"]
            finish = next(b for b in r["barriers"] if b["name"] == "finish")
            print(
                f"{bundle['course']['name']:22s} {s['swim']:6.0f} {s['bike']:7.0f} "
                f"{s['run']:7.0f} {s['total']:7.0f} {finish['limit_minutes']:11.0f} "
                f"{r['peak_load_pct']:9.0f}% | "
                f"{r['worst']['name']:>16s} {r['worst']['margin_minutes']:+7.0f} min  {r['verdict']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
