"""Athlete-submitted courses: the ingest, offline and end to end.

The whole path runs here with no database and no network — a real
:class:`~raceos.ingest.elevation.ConstantElevation` rather than a mock, which
is the same discipline payments and storage already follow. The assertion that
matters most is the last one: the payload this module builds is handed to the
*same* validator the official catalogue's bundles go through, so an athlete's
course cannot be loaded under a weaker standard than ours.
"""

from __future__ import annotations

import math

import pytest

from raceos.domain.enums import DistanceType, Leg
from raceos.ingest import gpx_course
from raceos.ingest.bundle_loader import validate_bundle
from raceos.ingest.elevation import ConstantElevation, decode_terrarium


def gpx_of(points: list[tuple[float, float]]) -> bytes:
    body = "".join(
        f'<trkpt lat="{lat}" lon="{lng}"><ele>999</ele></trkpt>' for lng, lat in points
    )
    return (
        '<?xml version="1.0"?><gpx version="1.1" creator="test">'
        f"<trk><trkseg>{body}</trkseg></trk></gpx>"
    ).encode()


def straight(lat0: float, lng0: float, metres: float, bearing_deg: float, nodes: int = 400):
    """A straight line of a known length, for distances that can be asserted."""
    out: list[tuple[float, float]] = []
    bearing = math.radians(bearing_deg)
    for index in range(nodes + 1):
        distance = metres * index / nodes
        dlat = distance * math.cos(bearing) / 111_320.0
        dlng = distance * math.sin(bearing) / (111_320.0 * math.cos(math.radians(lat0)))
        out.append((lng0 + dlng, lat0 + dlat))
    return out


@pytest.fixture
def half_distance_files() -> dict[Leg, tuple[str, bytes]]:
    return {
        Leg.SWIM: ("swim.gpx", gpx_of(straight(44.26, 12.36, 1900, 90))),
        Leg.BIKE: ("bike.gpx", gpx_of(straight(44.26, 12.36, 90_000, 45, 2000))),
        Leg.RUN: ("run.gpx", gpx_of(straight(44.26, 12.36, 21_100, 200, 800))),
    }


@pytest.fixture
def request_() -> gpx_course.BuildRequest:
    return gpx_course.BuildRequest(
        slug="test-race",
        name="Test Race",
        place="Cervia, Italy",
        timezone="Europe/Rome",
        distance_type=DistanceType.HALF,
        lat=44.26,
        lng=12.36,
        season_year=2026,
    )


# ---------------------------------------------------------------------------
# Parsing and cleaning
# ---------------------------------------------------------------------------


def test_a_file_that_is_not_gpx_names_what_is_wrong() -> None:
    with pytest.raises(gpx_course.GpxError) as caught:
        gpx_course.parse_gpx(b"this is a .fit file, renamed", filename="bike.gpx")
    assert "bike.gpx" in str(caught.value)


def test_a_file_with_one_point_is_not_a_route() -> None:
    with pytest.raises(gpx_course.GpxError):
        gpx_course.parse_gpx(gpx_of([(12.36, 44.26)]), filename="run.gpx")


def test_a_lost_gps_fix_is_dropped_rather_than_ridden() -> None:
    """A kilometre-long jump between consecutive samples is not a road."""
    points = straight(44.26, 12.36, 500, 90, 50)
    points.insert(25, (13.5, 45.0))  # a lost fix, a long way away
    cleaned = gpx_course.clean(points)
    assert (13.5, 45.0) not in cleaned
    assert gpx_course.path_length_m(cleaned) == pytest.approx(500, abs=5)


def test_resampling_spaces_nodes_evenly_and_keeps_the_ends() -> None:
    points = straight(44.26, 12.36, 1000, 90, 7)
    resampled = gpx_course.resample(points, spacing_m=10.0)
    assert resampled[0] == points[0]
    assert resampled[-1] == points[-1]
    steps = [
        gpx_course.haversine_m(a, b)
        for a, b in zip(resampled, resampled[1:], strict=False)
    ][:-1]
    assert max(steps) - min(steps) < 0.5


# ---------------------------------------------------------------------------
# Elevation
# ---------------------------------------------------------------------------


def test_terrarium_decodes_sea_level_and_a_summit() -> None:
    assert decode_terrarium(128, 0, 0) == 0.0
    # 128*256 + 100 - 32768 = 100 m
    assert decode_terrarium(128, 100, 0) == 100.0


def test_the_uploaded_files_own_elevation_is_never_used(
    request_: gpx_course.BuildRequest, half_distance_files: dict[Leg, tuple[str, bytes]]
) -> None:
    """Every ``<ele>`` in the fixture says 999. Nothing in the bundle does.

    SOLVER_MODEL.md §1.2: elevation is terrain-sampled, never barometric and
    never the consumer GPS trace.
    """
    result = gpx_course.build_bundle(
        request_, half_distance_files, elevation=ConstantElevation(12.0)
    )
    assert result.ok, result.problems
    heights = {
        round(node[2], 1)
        for leg in result.legs.values()
        for node in leg.nodes
    }
    assert 999.0 not in heights
    assert heights <= {0.0, 12.0}  # the swim is level water; the rest is the DEM


def test_filtered_gain_ignores_noise_below_the_threshold() -> None:
    flat_with_noise = [100.0 + (1.5 if index % 2 else 0.0) for index in range(200)]
    assert gpx_course.filtered_gain_m(flat_with_noise) == 0.0
    real_climb = [100.0, 110.0, 120.0, 130.0]
    assert gpx_course.filtered_gain_m(real_climb) == pytest.approx(30.0, abs=0.1)
    # Up, down, up again: both rises count and the descent does not.
    assert gpx_course.filtered_gain_m([100.0, 120.0, 110.0, 125.0]) == pytest.approx(35.0, abs=0.1)


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


def test_a_complete_submission_builds_a_bundle_the_loader_accepts(
    request_: gpx_course.BuildRequest, half_distance_files: dict[Leg, tuple[str, bytes]]
) -> None:
    result = gpx_course.build_bundle(
        request_, half_distance_files, elevation=ConstantElevation(12.0)
    )
    assert result.ok, result.problems
    # The same validator the official bundles go through.
    assert validate_bundle(result.payload, "submission") == []

    bundle = result.payload["course_bundle"]
    assert [b["name"] for b in bundle["barriers"]] == [
        "swim_exit",
        "bike_km_60",
        "bike_cutoff",
        "finish",
    ]
    limits = [b["limit_minutes_from_start"] for b in bundle["barriers"]]
    assert limits == sorted(limits), "a cut-off ladder out of order is not solvable"
    assert bundle["elevation_source"] == "terrain"
    assert bundle["provenance"] == "ESTIMATED"
    assert "athlete" in bundle["attribution"].lower()


def test_a_missing_leg_is_reported_rather_than_guessed(
    request_: gpx_course.BuildRequest, half_distance_files: dict[Leg, tuple[str, bytes]]
) -> None:
    del half_distance_files[Leg.RUN]
    result = gpx_course.build_bundle(
        request_, half_distance_files, elevation=ConstantElevation(12.0)
    )
    assert not result.ok
    assert any("run" in problem.lower() for problem in result.problems)


def test_every_wrong_leg_is_reported_at_once(
    request_: gpx_course.BuildRequest, half_distance_files: dict[Leg, tuple[str, bytes]]
) -> None:
    """Two wrong files produce two problems, not the first one twice.

    An athlete who fixes one file and is then told about the second has had
    two round trips where one would do.
    """
    half_distance_files[Leg.BIKE] = ("bike.gpx", gpx_of(straight(44.26, 12.36, 40_000, 45)))
    half_distance_files[Leg.RUN] = ("run.gpx", gpx_of(straight(44.26, 12.36, 10_000, 200)))
    result = gpx_course.build_bundle(
        request_, half_distance_files, elevation=ConstantElevation(12.0)
    )
    assert not result.ok
    assert len(result.problems) == 2
    assert any("bike" in problem.lower() for problem in result.problems)
    assert any("run" in problem.lower() for problem in result.problems)


def test_a_leg_slightly_off_nominal_is_accepted(
    request_: gpx_course.BuildRequest, half_distance_files: dict[Leg, tuple[str, bytes]]
) -> None:
    """A traced course is never exact, and rejecting it for 500 m would be wrong."""
    half_distance_files[Leg.BIKE] = ("bike.gpx", gpx_of(straight(44.26, 12.36, 89_400, 45, 2000)))
    result = gpx_course.build_bundle(
        request_, half_distance_files, elevation=ConstantElevation(12.0)
    )
    assert result.ok, result.problems


def test_segments_tile_the_leg_with_no_gap(
    request_: gpx_course.BuildRequest, half_distance_files: dict[Leg, tuple[str, bytes]]
) -> None:
    """A gap between segments is a stretch of road the plan never paces."""
    result = gpx_course.build_bundle(
        request_, half_distance_files, elevation=ConstantElevation(12.0)
    )
    assert result.ok, result.problems
    for leg in (Leg.BIKE, Leg.RUN):
        rows = sorted(
            (s for s in result.payload["course_bundle"]["segments"] if s["leg"] == leg.value),
            key=lambda s: s["from_km"],
        )
        assert rows
        assert rows[0]["from_km"] == 0.0
        for previous, current in zip(rows, rows[1:], strict=False):
            assert current["from_km"] == pytest.approx(previous["to_km"], abs=1e-6)
        assert rows[-1]["to_km"] == pytest.approx(result.legs[leg].distance_km, abs=1e-3)


def test_difficulty_is_derived_from_climbing_per_kilometre() -> None:
    # Kalmar's 515 m over a 70.3 is the flat, forgiving end.
    assert gpx_course._difficulty_for(515, DistanceType.HALF) == "APPROACHABLE"
    assert gpx_course._difficulty_for(2400, DistanceType.HALF) == "BRUTAL"
    # The same absolute climb is a far easier day over a Full: 1,200 m is
    # HARD across 113 km and only MODERATE across 226 km.
    assert gpx_course._difficulty_for(1200, DistanceType.HALF) == "HARD"
    assert gpx_course._difficulty_for(1200, DistanceType.FULL) == "MODERATE"
