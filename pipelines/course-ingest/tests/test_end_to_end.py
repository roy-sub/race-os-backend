"""One end-to-end generation against real (checked-in) road and DEM data."""
from __future__ import annotations

import json

from conftest import needs_fixtures

from course_ingest.codec import unpack
from course_ingest.pipeline import generate
from course_ingest.validate import validate_file


@needs_fixtures
def test_generate_emits_a_valid_bundle(fixture_spec, cfg, sources, tmp_path):
    roads, dem = sources
    result = generate(fixture_spec, tmp_path, cfg=cfg, roads=roads, dem=dem)
    emitted = result.emit_result

    assert emitted.report.ok, emitted.report.render()
    assert emitted.fixture_path.exists()
    assert emitted.packed_path.exists()
    assert emitted.packed_bytes < int(cfg["course"]["validation"]["max_bundle_bytes"])

    bundle = json.loads(emitted.fixture_path.read_text(encoding="utf-8"))
    cb = bundle["course_bundle"]

    # The schema additions the backend depends on.
    for column in ("segments", "waypoints", "elevation_source", "attribution"):
        assert column in cb, f"course_bundles.{column} missing"
    for leg in bundle["course_bundle_legs"]:
        assert leg["surface_quality"] in ("smooth_asphalt", "typical_road", "rough_chipseal")

    assert cb["elevation_source"] == "terrain"
    assert "OpenStreetMap" in cb["attribution"]
    assert cb["provenance"] == "ESTIMATED"
    assert all(a["provenance"] == "ESTIMATED" for a in cb["aid_stations"])
    assert all(w["provenance"] == "ESTIMATED" for w in cb["waypoints"])

    # Waypoint types live outside aid_stations, by construction.
    types = {w["type"] for w in cb["waypoints"]}
    assert {"transition", "distance_marker"} <= types
    assert all("type" not in a for a in cb["aid_stations"])

    # The packed bundle carries the same node series as the fixture.
    _header, legs = unpack(emitted.packed_path.read_bytes())
    assert len(legs["BIKE"]) == result.legs["BIKE"].node_count

    # `validate` on the emitted file reaches the same verdict as `generate`.
    assert validate_file(emitted.fixture_path, cfg).ok


@needs_fixtures
def test_swim_is_the_only_drawn_leg(fixture_spec, cfg, sources, tmp_path):
    """Bike and run must lie on real ways: every node within snapping distance
    of the road network it was routed on."""
    roads, dem = sources
    result = generate(fixture_spec, tmp_path, cfg=cfg, roads=roads, dem=dem, dry_run=True)
    assert result.reports["map_match"]["BIKE"].unmatched == 0
    assert result.reports["map_match"]["RUN"].unmatched == 0
    assert result.reports["map_match"]["BIKE"].max_offset_m <= float(
        cfg["routing"]["map_match"]["max_snap_distance_m"]
    )
