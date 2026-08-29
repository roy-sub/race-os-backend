"""Stage 10 -- validate and emit.

Three artefacts land per course:

    <slug>.bundle.json      seed fixture, shaped as the DB rows expect
    <slug>.bundle.bin       packed bundle, the object behind bundle_asset_key
    terrain/<slug>.pmtiles  clipped Terrarium extract for the course bbox

A bundle that fails validation is not written. That is the point of validating
here rather than after publish: a rejected bundle should leave no artefact
behind that someone could later mistake for a good one.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..bundle import fixture_bytes, pack_bundle
from ..config import Config
from ..validate import ValidationReport, validate_bundle


class BundleRejected(RuntimeError):
    def __init__(self, report: ValidationReport) -> None:
        super().__init__(f"{report.course_id}: bundle rejected\n{report.render()}")
        self.report = report


@dataclass(frozen=True)
class EmitResult:
    fixture_path: Path
    packed_path: Path
    packed_bytes: int
    report: ValidationReport


def emit(
    bundle: dict[str, Any],
    legs: dict[str, Any],
    cfg: Config,
    out_dir: str | Path,
    slug: str,
    dry_run: bool = False,
) -> EmitResult:
    out_dir = Path(out_dir)
    packed = pack_bundle(bundle, legs)
    report = validate_bundle(bundle, cfg, packed_bytes=len(packed))
    if not report.ok:
        raise BundleRejected(report)

    fixture_path = out_dir / f"{slug}.bundle.json"
    packed_path = out_dir / f"{slug}.bundle.bin"
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        fixture_path.write_bytes(fixture_bytes(bundle))
        packed_path.write_bytes(packed)

    return EmitResult(fixture_path, packed_path, len(packed), report)
