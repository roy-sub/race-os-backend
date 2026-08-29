"""Stage 9 -- attach aid stations, transitions, special needs, distance markers
and cut-off barriers.

All of this is invented, because the races are. It is therefore generated from
documented rules in `config/furniture.yaml` rather than placed by hand, and
every item is stamped `provenance: ESTIMATED`. Nothing here is presented as
official.

Two structural decisions worth stating:

* Aid stations and everything else live in separate arrays. "One action per aid
  station" is a solver correctness property (SOLVER_MODEL.md 5.5); keeping
  `aid_stations` pure means that property holds by construction rather than by
  every future reader remembering to filter on a type field.
* Cut-offs are minutes from the athlete's start, monotonic by construction, and
  scaled from one reference ladder per distance by a single per-course dial. A
  course is made harder by moving one number in its spec, not by hand-editing
  four barriers into an order that may not be chronologically sane.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..config import Config
from ..spec import CourseSpec


class FurnitureError(RuntimeError):
    pass


@dataclass(frozen=True)
class AidStation:
    leg: str
    name: str
    km: float
    contents: tuple[str, ...]
    provenance: str


@dataclass(frozen=True)
class Waypoint:
    type: str
    leg: str
    name: str
    km: float
    provenance: str


@dataclass(frozen=True)
class Barrier:
    name: str
    leg: str
    limit_minutes_from_start: float
    km: float


@dataclass(frozen=True)
class Furniture:
    aid_stations: tuple[AidStation, ...]
    waypoints: tuple[Waypoint, ...]
    barriers: tuple[Barrier, ...]
    cutoff_ratios: dict[str, float]


def _stations_for(leg: str, leg_km: float, spec: CourseSpec, cfg: Config) -> list[AidStation]:
    aid = cfg["furniture"]["aid_stations"]
    rule = aid["spacing"][spec.distance_type].get(leg)
    if rule is None:
        return []
    first = float(rule["first_km"])
    spacing = float(rule["spacing_km"])
    min_tail = float(aid["min_tail_km"])
    every_n = int(aid["full_service_every_n"][leg])
    contents = aid["contents"][leg]
    provenance = cfg["furniture"]["provenance"]

    out: list[AidStation] = []
    km = first
    index = 0
    while km <= leg_km - min_tail:
        index += 1
        kind = "full_service" if index % every_n == 0 else "standard"
        out.append(
            AidStation(
                leg=leg,
                name=f"{leg.title()} aid {index}",
                km=round(km, 3),
                contents=tuple(contents[kind]),
                provenance=provenance,
            )
        )
        km += spacing
    return out


def _markers_for(leg: str, leg_km: float, spec: CourseSpec, cfg: Config) -> list[Waypoint]:
    table = cfg["furniture"]["waypoints"]["distance_markers"].get(leg)
    if not table:
        return []
    step = float(table[spec.distance_type])
    provenance = cfg["furniture"]["provenance"]
    out: list[Waypoint] = []
    km = step
    while km < leg_km - 1e-6:
        out.append(
            Waypoint(
                type="distance_marker",
                leg=leg,
                name=f"{km:g} km",
                km=round(km, 3),
                provenance=provenance,
            )
        )
        km += step
    return out


def build_furniture(
    spec: CourseSpec,
    cfg: Config,
    leg_km: dict[str, float],
) -> Furniture:
    fcfg = cfg["furniture"]
    provenance = fcfg["provenance"]

    aid_stations: list[AidStation] = []
    for leg in ("BIKE", "RUN"):
        aid_stations.extend(_stations_for(leg, leg_km[leg], spec, cfg))

    waypoints: list[Waypoint] = []
    for key in ("t1", "t2"):
        t = fcfg["waypoints"]["transitions"][key]
        waypoints.append(
            Waypoint(
                type="transition",
                leg=t["leg"],
                name=t["name"],
                km=float(t["km"]),
                provenance=provenance,
            )
        )

    sn = fcfg["waypoints"]["special_needs"]
    snap = float(sn["snap_to_aid_within_km"])
    for leg in sn["legs_by_distance"][spec.distance_type]:
        target = leg_km[leg] * float(sn["at_fraction"])
        candidates = [a for a in aid_stations if a.leg == leg and abs(a.km - target) <= snap]
        km = min(candidates, key=lambda a: (abs(a.km - target), a.km)).km if candidates else round(target, 3)
        waypoints.append(
            Waypoint(
                type="special_needs",
                leg=leg,
                name=f"{leg.title()} special needs",
                km=km,
                provenance=provenance,
            )
        )

    for leg in ("SWIM", "BIKE", "RUN"):
        waypoints.extend(_markers_for(leg, leg_km[leg], spec, cfg))

    barriers, ratios = _build_barriers(spec, cfg, leg_km)

    aid_stations.sort(key=lambda a: (("BIKE", "RUN").index(a.leg), a.km, a.name))
    waypoints.sort(key=lambda w: (("SWIM", "BIKE", "RUN").index(w.leg), w.km, w.type, w.name))
    return Furniture(tuple(aid_stations), tuple(waypoints), tuple(barriers), ratios)


def _build_barriers(spec: CourseSpec, cfg: Config, leg_km: dict[str, float]):
    bcfg = cfg["furniture"]["barriers"]
    lo, hi = (float(x) for x in bcfg["generosity_bounds"])
    generosity = spec.cutoff_generosity
    if not lo <= generosity <= hi:
        raise FurnitureError(
            f"{spec.course_id}: cutoff_generosity {generosity} outside configured bounds [{lo}, {hi}]"
        )
    reference = bcfg["reference_minutes"][spec.distance_type]
    dp = int(bcfg["round_minutes_dp"])

    swim_exit = reference["swim_exit"] * generosity
    bike_cutoff = reference["bike_cutoff"] * generosity
    finish = reference["finish"] * generosity

    out: list[Barrier] = [
        Barrier("swim_exit", "SWIM", round(swim_exit, dp), round(leg_km["SWIM"], 3)),
    ]

    inter = bcfg["intermediate_bike_checkpoint"]
    if spec.distance_type in inter["enabled_for"]:
        km = leg_km["BIKE"] * float(inter["distance_fraction"])
        minutes = swim_exit + float(inter["time_fraction"]) * (bike_cutoff - swim_exit)
        out.append(
            Barrier(
                inter["name_template"].format(km=km),
                "BIKE",
                round(minutes, dp),
                round(km, 3),
            )
        )

    out.append(Barrier("bike_cutoff", "BIKE", round(bike_cutoff, dp), round(leg_km["BIKE"], 3)))
    out.append(Barrier("finish", "RUN", round(finish, dp), round(leg_km["RUN"], 3)))

    ratios = {
        "generosity": generosity,
        "swim_exit_min": out[0].limit_minutes_from_start,
        "bike_cutoff_min": round(bike_cutoff, dp),
        "finish_min": round(finish, dp),
        "reference_swim_exit_min": float(reference["swim_exit"]),
        "reference_bike_cutoff_min": float(reference["bike_cutoff"]),
        "reference_finish_min": float(reference["finish"]),
    }
    return out, ratios
