"""The determinism guarantee, proven rather than asserted.

The pipeline runs twice over identical inputs and the emitted bytes are
compared. This is the contract that lets a bundle be regenerated in a later
season and diffed meaningfully: if the output moved, the *input* moved.
"""
from __future__ import annotations

import hashlib
import json

from conftest import needs_fixtures

from course_ingest.bundle import fixture_bytes, pack_bundle
from course_ingest.pipeline import generate


def _run(spec, cfg, sources, tmp_path, tag):
    roads, dem = sources
    return generate(spec, tmp_path / tag, cfg=cfg, roads=roads, dem=dem, dry_run=True)


@needs_fixtures
def test_two_runs_are_byte_identical(fixture_spec, cfg, sources, tmp_path):
    first = _run(fixture_spec, cfg, sources, tmp_path, "a")
    second = _run(fixture_spec, cfg, sources, tmp_path, "b")

    a_json = fixture_bytes(first.bundle)
    b_json = fixture_bytes(second.bundle)
    assert hashlib.sha256(a_json).hexdigest() == hashlib.sha256(b_json).hexdigest(), (
        "seed fixture JSON differs between runs"
    )

    a_bin = pack_bundle(first.bundle, first.legs)
    b_bin = pack_bundle(second.bundle, second.legs)
    assert a_bin == b_bin, "packed bundle differs between runs"


@needs_fixtures
def test_geometry_is_identical_node_for_node(fixture_spec, cfg, sources, tmp_path):
    first = _run(fixture_spec, cfg, sources, tmp_path, "a")
    second = _run(fixture_spec, cfg, sources, tmp_path, "b")
    for leg in ("SWIM", "BIKE", "RUN"):
        assert first.legs[leg].nodes == second.legs[leg].nodes
        assert first.legs[leg].heights == second.legs[leg].heights


@needs_fixtures
def test_geometry_uses_fixed_precision_formatting(fixture_spec, cfg, sources, tmp_path):
    """Coordinates are formatted, never repr'd.

    An unformatted float is the classic way a bundle stops being byte-identical
    across platforms, and the geometry strings are where almost all the floats
    in a bundle live.
    """
    result = _run(fixture_spec, cfg, sources, tmp_path, "a")
    for leg in result.bundle["course_bundle_legs"]:
        coords = leg["geometry"].split("(", 1)[1].rstrip(")").split(",")
        assert coords
        for triple in coords:
            x, y, z = triple.split()
            assert len(x.split(".")[1]) == 6, x
            assert len(y.split(".")[1]) == 6, y
            assert len(z.split(".")[1]) == 2, z
            assert "e" not in triple.lower(), triple


@needs_fixtures
def test_the_fixture_reserialises_identically(fixture_spec, cfg, sources, tmp_path):
    """Serialisation is itself stable: writing the same bundle twice, and
    round-tripping it through JSON, produces the same bytes."""
    result = _run(fixture_spec, cfg, sources, tmp_path, "a")
    once = fixture_bytes(result.bundle)
    assert once == fixture_bytes(result.bundle)
    assert once == fixture_bytes(json.loads(once.decode("utf-8")))
