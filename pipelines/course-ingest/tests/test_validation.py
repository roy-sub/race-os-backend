"""Every validation rule gets a case that trips it.

A validator nobody has seen fail is a validator nobody knows works.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from conftest import needs_fixtures

from course_ingest.pipeline import generate
from course_ingest.validate import validate_bundle


@pytest.fixture(scope="module")
def good_bundle(request):
    """A real, passing bundle to mutate. Built once from the offline fixture."""
    cfg = request.getfixturevalue("cfg")
    sources = request.getfixturevalue("sources")
    spec = request.getfixturevalue("fixture_spec")
    tmp = Path(request.getfixturevalue("tmp_path_factory").mktemp("bundle"))
    result = generate(spec, tmp, cfg=cfg, roads=sources[0], dem=sources[1], dry_run=True)
    return result.bundle


def _rule(report, name):
    return next(f for f in report.findings if f.rule == name)


@needs_fixtures
def test_a_generated_bundle_passes(good_bundle, cfg):
    report = validate_bundle(good_bundle, cfg, packed_bytes=1000)
    assert report.ok, report.render()


@needs_fixtures
def test_missing_leg_is_rejected(good_bundle, cfg):
    b = copy.deepcopy(good_bundle)
    b["course_bundle_legs"] = [leg for leg in b["course_bundle_legs"] if leg["leg"] != "RUN"]
    report = validate_bundle(b, cfg)
    assert not report.ok
    assert not _rule(report, "legs_present").severity == "info"


@needs_fixtures
def test_leg_distance_outside_tolerance_is_rejected(good_bundle, cfg):
    b = copy.deepcopy(good_bundle)
    for leg in b["course_bundle_legs"]:
        if leg["leg"] == "BIKE":
            leg["distance_m"] *= 1.10
    report = validate_bundle(b, cfg)
    assert _rule(report, "distance_bike").severity == "error"


@needs_fixtures
def test_barrier_chronology_is_enforced(good_bundle, cfg):
    b = copy.deepcopy(good_bundle)
    barriers = b["course_bundle"]["barriers"]
    barriers[0]["limit_minutes_from_start"] = barriers[-1]["limit_minutes_from_start"] + 10
    report = validate_bundle(b, cfg)
    assert _rule(report, "barrier_order").severity == "error"


@needs_fixtures
def test_aid_station_beyond_leg_distance_is_rejected(good_bundle, cfg):
    b = copy.deepcopy(good_bundle)
    b["course_bundle"]["aid_stations"][0]["km"] = 9999.0
    report = validate_bundle(b, cfg)
    assert _rule(report, "aid_station_km").severity == "error"


@needs_fixtures
def test_elevation_series_length_must_match_node_count(good_bundle, cfg):
    b = copy.deepcopy(good_bundle)
    for leg in b["course_bundle_legs"]:
        if leg["leg"] == "RUN":
            leg["node_count"] += 1
    report = validate_bundle(b, cfg)
    assert _rule(report, "node_count_run").severity == "error"


@needs_fixtures
def test_implausible_gradient_is_rejected(good_bundle, cfg):
    """A route that jumped a valley: one node moved 400 m vertically."""
    b = copy.deepcopy(good_bundle)
    for leg in b["course_bundle_legs"]:
        if leg["leg"] != "BIKE":
            continue
        head, coords = leg["geometry"].split("(", 1)
        parts = coords.rstrip(")").split(",")
        for i in range(1, min(len(parts), 400), 2):
            x, y, _z = parts[i].split()
            parts[i] = f"{x} {y} 400.00"
        leg["geometry"] = head + "(" + ",".join(parts) + ")"
    report = validate_bundle(b, cfg)
    assert _rule(report, "gradient_bike").severity == "error"
    assert _rule(report, "hard_gradient_bike").severity == "error"


@needs_fixtures
def test_character_mismatch_is_rejected(good_bundle, cfg):
    """A course declared brutal whose bike leg comes out flat must not publish."""
    b = copy.deepcopy(good_bundle)
    b["provenance_detail"]["character"]["BIKE"] = {
        "character": "rugged_mountain",
        "min_gain_per_km": 12.0,
        "max_gain_per_km": 32.0,
    }
    for leg in b["course_bundle_legs"]:
        if leg["leg"] == "BIKE":
            leg["elevation_gain_m"] = 12
    report = validate_bundle(b, cfg)
    assert _rule(report, "character_bike").severity == "error"


@needs_fixtures
def test_oversize_bundle_is_rejected(good_bundle, cfg):
    report = validate_bundle(good_bundle, cfg, packed_bytes=500_000)
    assert _rule(report, "bundle_size").severity == "error"


@needs_fixtures
def test_non_terrain_elevation_source_is_rejected(good_bundle, cfg):
    """SOLVER_MODEL.md 1.2: anything but `terrain` raises BundleIncomplete
    downstream. Better to fail here than in a solve."""
    b = copy.deepcopy(good_bundle)
    b["course_bundle"]["elevation_source"] = "gps"
    report = validate_bundle(b, cfg)
    assert _rule(report, "elevation_source").severity == "error"


@needs_fixtures
def test_attribution_must_be_present(good_bundle, cfg):
    b = copy.deepcopy(good_bundle)
    b["course_bundle"]["attribution"] = ""
    report = validate_bundle(b, cfg)
    assert _rule(report, "attribution").severity == "error"


@needs_fixtures
def test_segments_must_tile_the_leg(good_bundle, cfg):
    b = copy.deepcopy(good_bundle)
    segs = [s for s in b["course_bundle"]["segments"] if s["leg"] == "BIKE"]
    assert len(segs) >= 2
    b["course_bundle"]["segments"].remove(segs[1])
    report = validate_bundle(b, cfg)
    assert _rule(report, "segments_bike").severity == "error"


@needs_fixtures
def test_aid_stations_carry_no_other_waypoint_types(good_bundle, cfg):
    b = copy.deepcopy(good_bundle)
    b["course_bundle"]["aid_stations"].append(
        {"leg": "BIKE", "name": "smuggled", "km": 1.0, "contents": [],
         "provenance": "ESTIMATED", "type": "special_needs"}
    )
    report = validate_bundle(b, cfg)
    assert _rule(report, "aid_stations_pure").severity == "error"
