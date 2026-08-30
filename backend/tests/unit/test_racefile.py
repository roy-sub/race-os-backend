"""The race-file decoder, offline and byte-exact.

The FIT reader is checked against the FIT *writer* in this same codebase: a
course written and read back must round-trip, which is the strongest
statement available without a device.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from raceos.domain.enums import RaceFileFormat
from raceos.exports import files as writer
from raceos.ingest import racefile


def _route(count: int = 300, spacing_m: float = 20.0) -> list[writer.RoutePoint]:
    return [
        writer.RoutePoint(
            lat=39.70 + index * 0.0002,
            lng=2.60 + index * 0.0002,
            elevation_m=10.0 + index * 0.4,
            distance_m=index * spacing_m,
        )
        for index in range(count)
    ]


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def test_the_content_decides_the_format_not_the_extension() -> None:
    """A `.fit` that is actually XML is a mislabelled export, and saying so is
    more useful than reporting a corrupt FIT file."""
    fit = writer.render_fit_course(course_name="T", points=_route(), waypoints=[])
    gpx = writer.render_gpx(course_name="T", points=_route(), attribution="x")

    assert racefile.detect_format(fit, "activity.gpx") is RaceFileFormat.FIT
    assert racefile.detect_format(gpx, "activity.fit") is RaceFileFormat.GPX


def test_an_unrecognisable_file_says_what_to_do() -> None:
    with pytest.raises(racefile.RaceFileError) as error:
        racefile.detect_format(b"not a race file at all", "notes.txt")
    assert "Export the activity again" in str(error.value)


# ---------------------------------------------------------------------------
# FIT
# ---------------------------------------------------------------------------


def test_a_fit_file_round_trips_through_the_writer() -> None:
    points = _route()
    encoded = writer.render_fit_course(
        course_name="Tramuntana",
        points=points,
        waypoints=[writer.Waypoint("Aid 1", 39.71, 2.61, 4000.0, "aid_station")],
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    parsed = racefile.parse_fit(encoded)

    assert parsed.format is RaceFileFormat.FIT
    assert len(parsed.points) == len(points)
    assert parsed.points[0].distance_m == pytest.approx(0.0, abs=0.01)
    assert parsed.points[-1].distance_m == pytest.approx(points[-1].distance_m, abs=0.01)
    # Altitude is stored as (m + 500) * 5 in an unsigned 16-bit field, so it
    # round-trips to the nearest 0.2 m.
    assert parsed.points[0].elevation_m == pytest.approx(10.0, abs=0.2)


def test_a_fit_file_with_no_records_says_it_is_the_wrong_kind_of_file() -> None:
    empty = writer.render_fit_course(course_name="T", points=[], waypoints=[])
    with pytest.raises(racefile.RaceFileError) as error:
        racefile.parse_fit(empty)
    assert "course or a settings file" in str(error.value)


def test_a_truncated_fit_file_is_refused_rather_than_half_read() -> None:
    encoded = writer.render_fit_course(course_name="T", points=_route(), waypoints=[])
    with pytest.raises(racefile.RaceFileError):
        racefile.parse_fit(encoded[:8])


# ---------------------------------------------------------------------------
# GPX and TCX
# ---------------------------------------------------------------------------


GPX_WITH_TIME = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
 <trk><trkseg>
  <trkpt lat="39.7000" lon="2.6000"><ele>10.0</ele><time>2026-06-21T05:00:00Z</time></trkpt>
  <trkpt lat="39.7090" lon="2.6000"><ele>12.0</ele><time>2026-06-21T05:05:00Z</time></trkpt>
  <trkpt lat="39.7180" lon="2.6000"><ele>14.0</ele><time>2026-06-21T05:10:00Z</time></trkpt>
 </trkseg></trk>
</gpx>"""


def test_gpx_distance_is_integrated_from_the_coordinates() -> None:
    """Most GPX exports carry no distance channel, so deriving it is what
    makes a plain GPX comparable at all."""
    parsed = racefile.parse_gpx(GPX_WITH_TIME.encode())

    assert len(parsed.points) == 3
    assert parsed.points[0].distance_m == 0.0
    # ~0.009° of latitude is almost exactly 1 km.
    assert parsed.points[1].distance_m == pytest.approx(1000.0, rel=0.01)
    assert parsed.points[2].distance_m == pytest.approx(2000.0, rel=0.01)
    assert parsed.total_elapsed_s == pytest.approx(600.0)


def test_a_gpx_route_with_no_track_points_says_so() -> None:
    route_only = b"""<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
    <wpt lat="1" lon="2"><name>x</name></wpt></gpx>"""
    with pytest.raises(racefile.RaceFileError) as error:
        racefile.parse_gpx(route_only)
    assert "route or waypoint file" in str(error.value)


TCX = """<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
 <Activities><Activity Sport="Biking"><Lap><Track>
  <Trackpoint><Time>2026-06-21T05:00:00Z</Time><DistanceMeters>0</DistanceMeters>
   <AltitudeMeters>10</AltitudeMeters><HeartRateBpm><Value>120</Value></HeartRateBpm>
   <Extensions><Watts>200</Watts></Extensions></Trackpoint>
  <Trackpoint><Time>2026-06-21T05:10:00Z</Time><DistanceMeters>5000</DistanceMeters>
   <AltitudeMeters>40</AltitudeMeters><HeartRateBpm><Value>140</Value></HeartRateBpm>
   <Extensions><Watts>220</Watts></Extensions></Trackpoint>
 </Track></Lap></Activity></Activities>
</TrainingCenterDatabase>"""


def test_tcx_prefers_its_declared_distance_channel() -> None:
    parsed = racefile.parse_tcx(TCX.encode())

    assert parsed.total_distance_m == 5000.0
    assert parsed.total_elapsed_s == 600.0
    assert parsed.has_power
    assert parsed.has_heart_rate
    assert parsed.points[1].power_w == 220.0


def test_an_absent_channel_is_none_not_zero() -> None:
    """Zero is a real power reading. Conflating the two is how an analysis
    reports that an athlete coasted a climb."""
    parsed = racefile.parse_gpx(GPX_WITH_TIME.encode())
    assert all(point.power_w is None for point in parsed.points)
    assert not parsed.has_power


def test_invalid_xml_names_the_problem() -> None:
    with pytest.raises(racefile.RaceFileError) as error:
        racefile.parse_gpx(b"<gpx><trk>")
    assert "not valid XML" in str(error.value)


# ---------------------------------------------------------------------------
# The qualifying effort — SOLVER_MODEL.md §2.5.3
# ---------------------------------------------------------------------------


def _steady(duration_s: int, pace_s_per_km: float, jitter: float = 0.0):
    """A synthetic run at a given pace, sampled every ten seconds."""
    points = []
    distance = 0.0
    for index in range(0, duration_s + 1, 10):
        wobble = jitter * (1 if (index // 10) % 2 else -1)
        points.append(
            racefile.TrackPoint(elapsed_s=float(index), distance_m=distance, elevation_m=0.0)
        )
        distance += 10.0 / (pace_s_per_km + wobble) * 1000.0
    return racefile.ParsedRaceFile(format=RaceFileFormat.GPX, points=tuple(points), started_at=None)


def test_a_steady_forty_minute_effort_qualifies() -> None:
    from raceos.services.postrace_service import find_qualifying_effort

    found = find_qualifying_effort(_steady(40 * 60, 300.0))
    assert found is not None
    assert 20 * 60 <= found.duration_s <= 90 * 60
    assert found.pace_cv < 0.05
    assert found.pace_s_per_km == pytest.approx(300.0, rel=0.02)


def test_a_twelve_minute_interval_does_not_qualify() -> None:
    """§2.5.3 step 4: a derived value from a short interval is worse than no
    value, because it will carry a `measured` stamp."""
    from raceos.services.postrace_service import find_qualifying_effort

    assert find_qualifying_effort(_steady(12 * 60, 300.0)) is None


def test_a_four_hour_ride_does_not_qualify_as_a_single_effort() -> None:
    from raceos.services.postrace_service import find_qualifying_effort

    found = find_qualifying_effort(_steady(4 * 3600, 300.0))
    # A window inside it can still qualify, but never the whole ride.
    assert found is None or found.duration_s <= 90 * 60


def test_a_variable_effort_is_rejected_on_pace_variation() -> None:
    """A session average is not a sustained effort."""
    from raceos.services.postrace_service import find_qualifying_effort

    assert find_qualifying_effort(_steady(40 * 60, 300.0, jitter=60.0)) is None
