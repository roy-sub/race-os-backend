"""Visual check artefacts.

The one review step in this pipeline that genuinely needs a human is looking at
the generated courses and deciding whether they look like races someone would
enter, rather than routes that wander implausibly. So every build renders a
static map and an elevation profile, and `visual-check` assembles all nine onto
one contact sheet.

Rendered with matplotlib and no basemap tiles: the point is the shape of the
route and the shape of the terrain, and a tiled backdrop would add a network
dependency and a second attribution obligation for no extra decision value.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

from .geo import cumulative_m, haversine_m, local_scale  # noqa: E402

LEG_STYLE = {
    "SWIM": {"color": "#2b8cbe", "lw": 2.2, "label": "Swim"},
    "BIKE": {"color": "#d95f0e", "lw": 1.5, "label": "Bike"},
    "RUN": {"color": "#31a354", "lw": 1.8, "label": "Run"},
}
BACKGROUND = "#f7f6f3"


def _equal_aspect(ax, lat: float) -> None:
    m_lon, m_lat = local_scale(lat)
    ax.set_aspect(m_lat / m_lon)


def _draw_map(ax, legs: dict, title: str, aid_stations=None) -> None:
    ax.set_facecolor(BACKGROUND)
    lats = []
    for leg in ("BIKE", "RUN", "SWIM"):
        data = legs.get(leg)
        if not data:
            continue
        xs = [p[0] for p in data["nodes"]]
        ys = [p[1] for p in data["nodes"]]
        lats.extend(ys)
        style = LEG_STYLE[leg]
        ax.plot(xs, ys, color=style["color"], lw=style["lw"], solid_joinstyle="round", zorder=2)

    # Aid stations, so the reviewer can see the spacing rather than trust it.
    for leg, km in aid_stations or ():
        data = legs.get(leg)
        if not data:
            continue
        point = _point_at_km(data["nodes"], km)
        if point:
            ax.plot([point[0]], [point[1]], marker="o", ms=2.6,
                    mfc="#ffffff", mec=LEG_STYLE[leg]["color"], mew=0.8, zorder=3)

    start = legs["BIKE"]["nodes"][0] if legs.get("BIKE") else legs["SWIM"]["nodes"][0]
    ax.plot([start[0]], [start[1]], marker="o", ms=6, mfc="#ffffff", mec="#222222", mew=1.4, zorder=4)
    if lats:
        _equal_aspect(ax, sum(lats) / len(lats))
    ax.set_title(title, fontsize=10, loc="left")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_color("#cccccc")


def _draw_profile(ax, legs: dict, attribution: str | None = None) -> None:
    ax.set_facecolor(BACKGROUND)
    offset = 0.0
    for leg in ("SWIM", "BIKE", "RUN"):
        data = legs.get(leg)
        if not data:
            continue
        cum = cumulative_m(data["nodes"])
        xs = [offset + c / 1000.0 for c in cum]
        ys = data["heights"]
        style = LEG_STYLE[leg]
        ax.fill_between(xs, min(ys) - 5, ys, color=style["color"], alpha=0.22, lw=0)
        ax.plot(xs, ys, color=style["color"], lw=1.0, label=style["label"])
        offset = xs[-1]
        ax.axvline(offset, color="#999999", lw=0.6, ls=":")
    ax.set_xlabel("cumulative distance (km)", fontsize=8)
    ax.set_ylabel("elevation (m)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.grid(True, color="#e2e0dc", lw=0.6)
    ax.set_axisbelow(True)
    if attribution:
        ax.text(
            0.0, -0.34, attribution, transform=ax.transAxes, fontsize=6, color="#666666",
            va="top", ha="left",
        )


def _point_at_km(nodes, km: float):
    target = km * 1000.0
    total = 0.0
    for i in range(len(nodes) - 1):
        step = haversine_m(nodes[i], nodes[i + 1])
        if total + step >= target:
            return nodes[i]
        total += step
    return nodes[-1] if nodes else None


def _aid_positions(bundle: dict):
    return [(a["leg"], float(a["km"])) for a in bundle["course_bundle"]["aid_stations"]]


def _annotate_climbs(ax, bundle: dict, legs: dict, top: int = 3) -> None:
    """Label the steepest named climbs on the profile.

    The point of the visual check is deciding whether these look like races
    someone would enter; a profile with its real climbs named answers that
    faster than a bare line does.
    """
    segments = [
        s for s in bundle["course_bundle"]["segments"]
        if s["leg"] == "BIKE" and s["name_source"] == "OSM_WAY" and s["net_gradient"] > 0.02
    ]
    if not segments:
        return
    segments.sort(key=lambda s: (-s["elevation_gain_m"], s["ordinal"]))
    swim_km = legs["SWIM"]["nodes"] and _leg_km(legs["SWIM"]) or 0.0
    for seg in segments[:top]:
        mid_km = (seg["from_km"] + seg["to_km"]) / 2.0
        data = legs["BIKE"]
        point = _point_at_km(data["nodes"], mid_km)
        if point is None:
            continue
        idx = min(len(data["heights"]) - 1, int(mid_km * 1000 / 10))
        ax.annotate(
            f"{seg['name']}\n{seg['to_km'] - seg['from_km']:.1f} km @ {seg['net_gradient']*100:.1f}%",
            xy=(swim_km + mid_km, data["heights"][idx]),
            xytext=(0, 16), textcoords="offset points",
            fontsize=6, ha="center", color="#333333",
            arrowprops=dict(arrowstyle="-", lw=0.5, color="#999999"),
        )


def _leg_km(data) -> float:
    return cumulative_m(data["nodes"])[-1] / 1000.0


def _legs_from_result(result) -> dict:
    return {
        leg: {"nodes": data.nodes, "heights": data.heights}
        for leg, data in result.legs.items()
    }


def _legs_from_fixture(bundle: dict) -> dict:
    from .validate import parse_ewkt

    out = {}
    for leg in bundle["course_bundle_legs"]:
        coords = parse_ewkt(leg["geometry"])
        out[leg["leg"]] = {
            "nodes": [(c[0], c[1]) for c in coords],
            "heights": [c[2] for c in coords],
        }
    return out


def _headline(bundle: dict) -> str:
    course = bundle["course"]
    legs = {leg["leg"]: leg for leg in bundle["course_bundle_legs"]}
    return (
        f"{course['name']}  ({course['distance_type']}, {course['difficulty']})\n"
        f"{legs['SWIM']['distance_m']/1000:.2f} / {legs['BIKE']['distance_m']/1000:.1f} / "
        f"{legs['RUN']['distance_m']/1000:.1f} km  ·  bike +{legs['BIKE']['elevation_gain_m']} m  ·  "
        f"run +{legs['RUN']['elevation_gain_m']} m"
    )


def _draw_start_inset(ax_map, legs: dict) -> None:
    """Inset on the transition area.

    A 3.8 km swim beside a 180 km bike leg is four pixels wide on the main map,
    so the leg that is the only DRAWN geometry in the bundle -- the one most
    worth eyeballing -- is invisible exactly where it matters.
    """
    swim = legs.get("SWIM")
    if not swim or not swim["nodes"]:
        return
    inset = ax_map.inset_axes([0.02, 0.02, 0.30, 0.30])
    inset.set_facecolor(BACKGROUND)
    xs = [p[0] for p in swim["nodes"]]
    ys = [p[1] for p in swim["nodes"]]
    pad_x = (max(xs) - min(xs)) * 0.8 + 1e-4
    pad_y = (max(ys) - min(ys)) * 0.8 + 1e-4
    for leg in ("BIKE", "RUN", "SWIM"):
        data = legs.get(leg)
        if not data:
            continue
        style = LEG_STYLE[leg]
        inset.plot([p[0] for p in data["nodes"]], [p[1] for p in data["nodes"]],
                   color=style["color"], lw=style["lw"], zorder=2)
    inset.plot([xs[0]], [ys[0]], marker="o", ms=5, mfc="#ffffff", mec="#222222", mew=1.2, zorder=4)
    inset.set_xlim(min(xs) - pad_x, max(xs) + pad_x)
    inset.set_ylim(min(ys) - pad_y, max(ys) + pad_y)
    _equal_aspect(inset, sum(ys) / len(ys))
    inset.set_xticks([])
    inset.set_yticks([])
    inset.set_title("swim & transition", fontsize=6, pad=2)
    for spine in inset.spines.values():
        spine.set_color("#999999")


def render_course(result, out_dir: str | Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    legs = _legs_from_result(result)
    bundle = result.bundle
    slug = result.spec.slug

    fig, (ax_map, ax_prof) = plt.subplots(
        2, 1, figsize=(8.5, 10.0), gridspec_kw={"height_ratios": [2.2, 1.0]}
    )
    fig.patch.set_facecolor("#ffffff")
    _draw_map(ax_map, legs, _headline(bundle), _aid_positions(bundle))
    _draw_start_inset(ax_map, legs)
    _draw_profile(ax_prof, legs, bundle["course_bundle"]["attribution"])
    _annotate_climbs(ax_prof, bundle, legs)
    ax_map.legend(
        handles=[
            Line2D([0], [0], color=s["color"], lw=2, label=s["label"])
            for s in LEG_STYLE.values()
        ],
        loc="lower right", fontsize=8, frameon=True, framealpha=0.9,
    )
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    path = out_dir / f"{slug}.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return [path]


def render_contact_sheet(bundles_dir: str | Path, out_dir: str | Path) -> Path:
    bundles_dir = Path(bundles_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(bundles_dir.glob("*.bundle.json"))
    if not files:
        raise FileNotFoundError(f"no bundle fixtures in {bundles_dir}")

    cols = 3
    rows = math.ceil(len(files) / cols)
    fig, axes = plt.subplots(rows * 2, cols, figsize=(5.2 * cols, 4.9 * rows),
                             gridspec_kw={"height_ratios": [2.4, 1.0] * rows})
    fig.patch.set_facecolor("#ffffff")
    axes = axes.reshape(rows * 2, cols)

    for i, path in enumerate(files):
        bundle = json.loads(path.read_text(encoding="utf-8"))
        legs = _legs_from_fixture(bundle)
        r, c = divmod(i, cols)
        _draw_map(axes[r * 2][c], legs, _headline(bundle), _aid_positions(bundle))
        _draw_start_inset(axes[r * 2][c], legs)
        _draw_profile(axes[r * 2 + 1][c], legs)

    for i in range(len(files), rows * cols):
        r, c = divmod(i, cols)
        axes[r * 2][c].axis("off")
        axes[r * 2 + 1][c].axis("off")

    attribution = json.loads(files[0].read_text(encoding="utf-8"))["course_bundle"]["attribution"]
    fig.suptitle(
        f"RaceOS seeded courses -- visual check ({len(files)} generated)\n{attribution}",
        fontsize=12,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    path = out_dir / "contact-sheet.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path
