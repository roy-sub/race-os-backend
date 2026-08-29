"""Seed specification: the human-editable input to the pipeline.

One YAML file per course, in `specs/`. Everything the pipeline needs to build a
bundle is here; nothing about a course is hidden in code. Adding a tenth course
is a new file, not a new branch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DISTANCE_TYPES = ("Sprint", "Olympic", "70.3", "Full")
DIFFICULTIES = ("APPROACHABLE", "MODERATE", "HARD", "BRUTAL")
PROVENANCES = ("OFFICIAL", "CROWD", "ESTIMATED")
WATER_KINDS = ("sea", "lake", "canal", "harbour")


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class LegSpec:
    laps: int
    bearing_offset_deg: float
    character: str | None = None


@dataclass(frozen=True)
class CourseSpec:
    course_id: str
    slug: str
    name: str
    place: str
    country: str
    timezone: str
    distance_type: str
    difficulty: str
    character: str
    tone_color: str
    start_lat: float
    start_lng: float
    water_kind: str
    water_name: str | None
    swim_bearing_deg: float
    bike: LegSpec
    run: LegSpec
    season_year: int
    version: str
    provenance: str
    verified: bool
    cutoff_generosity: float
    changelog: str
    media_hero_path: str | None = None
    media_card_path: str | None = None
    notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def bundle_key(self) -> str:
        """course_id + season_year + version, per Part 10.1."""
        return f"{self.course_id}:{self.season_year}:{self.version}"


def _require(data: dict, key: str, path: Path):
    if key not in data:
        raise SpecError(f"{path.name}: missing required key `{key}`")
    return data[key]


def load_spec(path: str | Path) -> CourseSpec:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SpecError(f"{path.name}: spec must be a mapping")

    distance_type = _require(data, "distance_type", path)
    if distance_type not in DISTANCE_TYPES:
        raise SpecError(f"{path.name}: distance_type must be one of {DISTANCE_TYPES}")
    difficulty = _require(data, "difficulty", path)
    if difficulty not in DIFFICULTIES:
        raise SpecError(f"{path.name}: difficulty must be one of {DIFFICULTIES}")
    provenance = data.get("provenance", "ESTIMATED")
    if provenance not in PROVENANCES:
        raise SpecError(f"{path.name}: provenance must be one of {PROVENANCES}")

    start = _require(data, "start", path)
    swim = _require(data, "swim", path)
    if swim["water_kind"] not in WATER_KINDS:
        raise SpecError(f"{path.name}: swim.water_kind must be one of {WATER_KINDS}")

    def leg(name: str) -> LegSpec:
        raw = _require(data, name, path)
        return LegSpec(
            laps=int(raw.get("laps", 1)),
            bearing_offset_deg=float(raw.get("bearing_offset_deg", 0.0)),
            character=raw.get("character"),
        )

    return CourseSpec(
        course_id=_require(data, "course_id", path),
        slug=data.get("slug", data["course_id"]),
        name=_require(data, "name", path),
        place=_require(data, "place", path),
        country=_require(data, "country", path),
        timezone=_require(data, "timezone", path),
        distance_type=distance_type,
        difficulty=difficulty,
        character=_require(data, "character", path),
        tone_color=data.get("tone_color", "#1f6f6b"),
        start_lat=float(start["lat"]),
        start_lng=float(start["lng"]),
        water_kind=swim["water_kind"],
        water_name=swim.get("water_name"),
        swim_bearing_deg=float(swim.get("bearing_deg", 0.0)),
        bike=leg("bike"),
        run=leg("run"),
        season_year=int(data.get("season_year", 2026)),
        version=str(data.get("version", "v2026.1")),
        provenance=provenance,
        verified=bool(data.get("verified", False)),
        cutoff_generosity=float(data.get("cutoff_generosity", 1.0)),
        changelog=data.get("changelog", ""),
        media_hero_path=data.get("media_hero_path"),
        media_card_path=data.get("media_card_path"),
        notes=data.get("notes", ""),
        raw=data,
    )


def load_all_specs(directory: str | Path) -> list[CourseSpec]:
    directory = Path(directory)
    return [load_spec(p) for p in sorted(directory.glob("*.yaml"))]
