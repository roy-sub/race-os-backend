"""Source interfaces.

The pipeline never talks to a concrete data source. It talks to `RoadSource` and
`DemSource`. That is what makes swapping the Overture snapshot for a locally-run
OSRM instance, or a licensed course file, an implementation rather than a
rewrite -- and what lets the tests run offline against fixtures.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Sequence

from ..geo import Point


class SourceError(RuntimeError):
    """A data source could not satisfy a request."""


class MissingDemTile(SourceError):
    """A DEM tile is absent.

    This is always fatal. Part of the brief's hard rule: if a DEM tile is
    missing for a region, fail loudly rather than interpolating across the gap.
    """


@dataclass(frozen=True)
class RoadWay:
    """One routable way, as delivered by a road source.

    `connectors` are the topology handles: two ways sharing a connector id meet
    there. `at` is the fractional position of that connector along `geometry`.
    """

    way_id: str
    geometry: tuple[Point, ...]
    road_class: str
    name: str | None
    surface: str | None
    connectors: tuple[tuple[str, float], ...]
    access_denied: bool
    sources: tuple[tuple[str, str], ...] = field(default=())  # (dataset, license)
    #: Overture road_flags, e.g. is_bridge / is_tunnel / is_covered. The DEM
    #: cannot see under a tunnel or over a viaduct, so these decide routability.
    flags: tuple[str, ...] = field(default=())


class RoadSource(abc.ABC):
    """Supplies real road geometry and topology for a bounding box."""

    #: Opaque string identifying the exact data snapshot, carried into bundle
    #: provenance so a regenerated bundle is traceable.
    snapshot_id: str

    @abc.abstractmethod
    def ways_in_bbox(self, bbox: tuple[float, float, float, float]) -> list[RoadWay]:
        """Return every routable way intersecting `bbox` (minx, miny, maxx, maxy)."""

    @abc.abstractmethod
    def water_rings_in_bbox(
        self, bbox: tuple[float, float, float, float]
    ) -> list[tuple[str, str | None, tuple[Point, ...]]]:
        """Return (subtype, name, exterior ring) for water bodies in `bbox`."""


class DemSource(abc.ABC):
    """Supplies terrain-sampled elevation. Never GPS, never barometric."""

    snapshot_id: str
    #: Value written to the bundle's `elevation_source` column. SOLVER_MODEL.md
    #: 1.2 raises BundleIncomplete for anything other than "terrain".
    elevation_source: str = "terrain"

    @abc.abstractmethod
    def sample(self, points: Sequence[Point]) -> list[float]:
        """Elevation in metres for each point, in order. Raises MissingDemTile."""

    @abc.abstractmethod
    def attribution(self) -> str:
        """Human-readable attribution for the elevation data."""
