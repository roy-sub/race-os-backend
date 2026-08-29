"""Bundle assembly.

Two artefacts come out of a build, and they are two views of one geometry:

* the **seed fixture** (`<slug>.bundle.json`), shaped exactly like the rows the
  backend's `course_bundles`, `course_bundle_legs` and `courses` tables expect,
  with leg geometry as EWKT `LINESTRING Z` so a seeder can insert it directly;
* the **packed bundle** (`<slug>.bundle.bin`), the artefact behind
  `course_bundles.bundle_asset_key`, under the 400 KB budget.

Both are generated from the same node series. "One geometry, three consumers"
(Part 10.3) means the map, the solver and the FIT export must never be able to
disagree, so nothing is rounded independently into one artefact and not the
other.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .codec import canonical_json, ewkt_linestring_z, pack
from .config import Config
from .geo import Point, cumulative_m, elevation_gain
from .spec import CourseSpec

SCHEMA_VERSION = 1
LEGS = ("SWIM", "BIKE", "RUN")


@dataclass
class LegData:
    leg: str
    nodes: list[Point]
    heights: list[float]
    gradients: list[float]
    surface_quality: str
    length_m: float
    #: Reported ascent, hysteresis-filtered (config: course.elevation.gain_threshold_m).
    gain_m: float
    #: Plain sum of positive node differences -- what the solver recomputes.
    gain_m_raw_nodes: float
    loss_m: float
    laps: int
    segments: list[Any] = field(default_factory=list)

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    def xyz(self) -> list[tuple[float, float, float]]:
        return [(p[0], p[1], h) for p, h in zip(self.nodes, self.heights)]


def build_attribution(
    datasets: Sequence[tuple[str, str]], dem_attribution: str, cfg: Config
) -> str:
    """Assemble the ODbL attribution from the licences the data actually carries.

    ODbL obliges attribution wherever the derived data is displayed, so the
    string is emitted as a bundle field rather than left to a UI constant, and
    it is built from `sources[].dataset` / `sources[].license` of the ways used
    rather than assumed.
    """
    lic_cfg = cfg["sources"]["licensing"]
    names = lic_cfg["dataset_attribution"]
    licences = lic_cfg["license_names"]

    by_licence: dict[str, list[str]] = {}
    for dataset, licence in sorted(set(datasets)):
        label = names.get(dataset, f"© {dataset}")
        by_licence.setdefault(licences.get(licence, licence), []).append(label)

    parts = []
    for licence in sorted(by_licence):
        credits = ", ".join(sorted(set(by_licence[licence])))
        parts.append(f"{credits}, {licence}")
    parts.append(dem_attribution)
    return " · ".join(parts)


def _display_profile(cum: Sequence[float], heights: Sequence[float], samples: int = 400):
    """A fixed-length profile for the elevation chart.

    Explicitly derived-for-display. The authoritative series is the Z ordinate
    of `course_bundle_legs.geometry`; this exists so the frontend chart does not
    have to parse 18 000 nodes to draw 900 pixels.
    """
    n = len(heights)
    if n <= samples:
        idx = list(range(n))
    else:
        idx = [round(i * (n - 1) / (samples - 1)) for i in range(samples)]
    return {
        "s_km": [round(cum[i] / 1000.0, 4) for i in idx],
        "h_m": [round(heights[i], 2) for i in idx],
    }


def elevation_profile(legs: dict[str, LegData]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "authority": "course_bundle_legs.geometry (Z ordinate)",
        "note": (
            "`display` is a downsample for charting only; never solve against it. "
            "`gain_m` is hysteresis-filtered surveyed ascent and is what the UI shows; "
            "`gain_m_raw_nodes` is the plain sum of positive node differences, which is "
            "what SOLVER_MODEL.md 1.1 recomputes per segment."
        ),
        "legs": {},
    }
    for leg in LEGS:
        data = legs.get(leg)
        if data is None:
            continue
        cum = cumulative_m(data.nodes)
        out["legs"][leg] = {
            "node_count": data.node_count,
            "distance_m": round(data.length_m, 2),
            "gain_m": round(data.gain_m, 1),
            "gain_m_raw_nodes": round(data.gain_m_raw_nodes, 1),
            "loss_m": round(data.loss_m, 1),
            "min_m": round(min(data.heights), 2),
            "max_m": round(max(data.heights), 2),
            "mean_m": round(sum(data.heights) / len(data.heights), 3),
            "laps": data.laps,
            "display": _display_profile(cum, data.heights),
        }
    return out


def assemble(
    spec: CourseSpec,
    cfg: Config,
    legs: dict[str, LegData],
    furniture,
    attribution: str,
    elevation_source: str,
    provenance_detail: dict[str, Any],
) -> dict[str, Any]:
    node_spacing = float(cfg["course"]["resample"]["node_spacing_m"])
    course_gain = sum(legs[leg].gain_m for leg in ("BIKE", "RUN") if leg in legs)

    asset_key = f"course-bundles/{spec.slug}/{spec.season_year}/{spec.version}/bundle.bin"
    terrain_key = f"terrain/{spec.slug}/{spec.season_year}/{spec.version}/terrain.pmtiles"

    segments = [
        {
            "ordinal": s.ordinal,
            "leg": s.leg,
            "name": s.name,
            "from_km": s.from_km,
            "to_km": s.to_km,
            "net_gradient": s.net_gradient,
            "elevation_gain_m": s.elevation_gain_m,
            "surface_quality": s.surface_quality,
            "name_source": s.name_source,
        }
        for leg in LEGS
        for s in legs[leg].segments
        if leg in legs
    ]
    for i, seg in enumerate(segments, start=1):
        seg["ordinal"] = i

    bundle = {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "raceos-course-ingest",
        "course": {
            "name": spec.name,
            "place": spec.place,
            "slug": spec.slug,
            "distance_type": spec.distance_type,
            "difficulty": spec.difficulty,
            "elevation_gain_m": int(round(course_gain)),
            "media_hero_path": spec.media_hero_path,
            "media_card_path": spec.media_card_path,
            "tone_color": spec.tone_color,
            "timezone": spec.timezone,
            "lat": round(spec.start_lat, 6),
            "lng": round(spec.start_lng, 6),
            "is_fictional": True,
        },
        "course_bundle": {
            "course_id": spec.course_id,
            "version": spec.version,
            "status": "draft",
            "provenance": spec.provenance,
            "verified_at": None,
            "published_at": None,
            "season_year": spec.season_year,
            "changelog": spec.changelog,
            "plans_affected_count": 0,
            "bundle_asset_key": asset_key,
            "terrain_pmtiles_key": terrain_key,
            "elevation_source": elevation_source,
            "attribution": attribution,
            "segments": segments,
            "waypoints": [
                {
                    "type": w.type,
                    "leg": w.leg,
                    "name": w.name,
                    "km": w.km,
                    "provenance": w.provenance,
                }
                for w in furniture.waypoints
            ],
            "aid_stations": [
                {
                    "leg": a.leg,
                    "name": a.name,
                    "km": a.km,
                    "contents": list(a.contents),
                    "provenance": a.provenance,
                }
                for a in furniture.aid_stations
            ],
            "barriers": [
                {
                    "name": b.name,
                    "leg": b.leg,
                    "limit_minutes_from_start": b.limit_minutes_from_start,
                    "km": b.km,
                }
                for b in furniture.barriers
            ],
            "elevation_profile": elevation_profile(legs),
        },
        "course_bundle_legs": [
            {
                "leg": leg,
                "geometry": ewkt_linestring_z(legs[leg].xyz()),
                "distance_m": round(legs[leg].length_m, 2),
                "elevation_gain_m": int(round(legs[leg].gain_m)),
                "node_count": legs[leg].node_count,
                "surface_quality": legs[leg].surface_quality,
            }
            for leg in LEGS
            if leg in legs
        ],
        "provenance_detail": {
            "node_spacing_m": node_spacing,
            "gain_threshold_m": float(cfg["course"]["elevation"]["gain_threshold_m"]),
            "cutoff_ratios": furniture.cutoff_ratios,
            **provenance_detail,
        },
    }
    return bundle


def pack_bundle(bundle: dict[str, Any], legs: dict[str, LegData]) -> bytes:
    header = {
        "schema_version": bundle["schema_version"],
        "course": bundle["course"],
        "course_bundle": {
            k: v
            for k, v in bundle["course_bundle"].items()
            if k != "elevation_profile"
        },
        "legs": [
            {
                "leg": leg["leg"],
                "distance_m": leg["distance_m"],
                "elevation_gain_m": leg["elevation_gain_m"],
                "node_count": leg["node_count"],
                "surface_quality": leg["surface_quality"],
            }
            for leg in bundle["course_bundle_legs"]
        ],
        "provenance_detail": bundle["provenance_detail"],
    }
    return pack(header, {leg: legs[leg].xyz() for leg in LEGS if leg in legs})


def fixture_bytes(bundle: dict[str, Any]) -> bytes:
    """Deterministic, human-diffable fixture JSON."""
    import json

    return (
        json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")


__all__ = [
    "LegData",
    "SCHEMA_VERSION",
    "assemble",
    "build_attribution",
    "canonical_json",
    "elevation_profile",
    "fixture_bytes",
    "pack_bundle",
]
