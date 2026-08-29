"""Configuration loading.

Every tunable in the pipeline lives in one of the YAML files beside this module.
Nothing in the pipeline may inline a threshold, a spacing, a cost or a ratio.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Mapping

import yaml

CONFIG_DIR = Path(__file__).resolve().parent
_FILES = ("sources", "routing", "course", "furniture")


class ConfigError(RuntimeError):
    """Raised when configuration is missing or internally inconsistent."""


class Config(Mapping[str, Any]):
    """Read-only view over the merged configuration tree."""

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def get_path(self, dotted: str, default: Any = ...) -> Any:
        """Fetch a nested value by dotted path, failing loudly by default."""
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is ...:
                    raise ConfigError(f"missing config key: {dotted}")
                return default
            node = node[part]
        return node


@functools.lru_cache(maxsize=1)
def load_config(config_dir: str | None = None) -> Config:
    base = Path(config_dir) if config_dir else CONFIG_DIR
    merged: dict[str, Any] = {}
    for name in _FILES:
        path = base / f"{name}.yaml"
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        with path.open("r", encoding="utf-8") as fh:
            merged[name] = yaml.safe_load(fh)
    _validate(merged)
    return Config(merged)


def _validate(cfg: dict[str, Any]) -> None:
    """Fail at load time on the inconsistencies that would otherwise surface
    halfway through a two-minute route build."""
    distances = cfg["course"]["distances"]
    for section, keys in (
        ("furniture.barriers.reference_minutes", cfg["furniture"]["barriers"]["reference_minutes"]),
        ("furniture.aid_stations.spacing", cfg["furniture"]["aid_stations"]["spacing"]),
        ("routing.loop.waypoint_count_by_distance", cfg["routing"]["loop"]["waypoint_count_by_distance"]),
    ):
        missing = sorted(set(distances) - set(keys))
        if missing:
            raise ConfigError(f"{section} is missing distance types: {missing}")

    surfaces = set(cfg["course"]["surface_map"].values())
    allowed = {"smooth_asphalt", "typical_road", "rough_chipseal"}
    if not surfaces <= allowed:
        raise ConfigError(f"surface_map has values outside {sorted(allowed)}: {sorted(surfaces - allowed)}")
    if cfg["course"]["surface_absent_default"] not in allowed:
        raise ConfigError("surface_absent_default is not a valid surface_quality")

    bands = cfg["course"]["segmentation"]["bands"]
    maxima = [b["max"] for b in bands]
    if maxima != sorted(maxima):
        raise ConfigError("segmentation.bands must be ordered by ascending `max`")


__all__ = ["Config", "ConfigError", "load_config", "CONFIG_DIR"]
