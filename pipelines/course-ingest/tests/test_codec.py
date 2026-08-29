from __future__ import annotations

import pytest

from course_ingest.codec import ewkt_linestring_z, pack, unpack
from course_ingest.geo import elevation_gain


def test_pack_roundtrip_is_lossless_at_stored_precision():
    nodes = [(2.5 + i * 1e-4, 39.5 + i * 5e-5, 10.0 + i * 0.37) for i in range(5000)]
    header = {"course": {"slug": "x"}, "n": 1}
    blob = pack(header, {"BIKE": nodes})
    back_header, back_legs = unpack(blob)
    assert back_header == header
    for (x1, y1, z1), (x2, y2, z2) in zip(nodes, back_legs["BIKE"]):
        assert abs(x1 - x2) <= 5e-7
        assert abs(y1 - y2) <= 5e-7
        assert abs(z1 - z2) <= 5e-3


def test_pack_is_deterministic():
    nodes = [(1.0 + i * 1e-4, 2.0 + i * 1e-4, float(i)) for i in range(500)]
    assert pack({"a": 1, "b": 2}, {"RUN": nodes}) == pack({"b": 2, "a": 1}, {"RUN": nodes})


def test_pack_stays_well_inside_the_size_budget():
    """A full-distance course is roughly 22 600 nodes at 10 m spacing."""
    nodes = [(2.5 + i * 9e-5, 39.5 + i * 4e-5, 100.0 + (i % 200) * 0.4) for i in range(22600)]
    blob = pack({"slug": "full"}, {"BIKE": nodes[:18000], "RUN": nodes[18000:]})
    assert len(blob) < 409600


def test_ewkt_is_fixed_precision():
    assert ewkt_linestring_z([(1.0, 2.0, 3.0)]) == "SRID=4326;LINESTRING Z (1.000000 2.000000 3.00)"


@pytest.mark.parametrize(
    "heights,threshold,expected",
    [
        ([0.0, 5.0, 0.0, 5.0], 0.0, 10.0),
        ([0.0, 1.0, 0.0, 1.0, 0.0], 3.0, 0.0),      # noise below the threshold
        ([0.0, 10.0, 9.0, 20.0], 3.0, 20.0),        # one real climb with a dip
        ([100.0, 90.0], 3.0, 0.0),                  # pure descent
    ],
)
def test_elevation_gain_hysteresis(heights, threshold, expected):
    assert elevation_gain(heights, threshold) == pytest.approx(expected, abs=1e-6)
