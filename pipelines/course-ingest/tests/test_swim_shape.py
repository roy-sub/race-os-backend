"""The swim rectangle's proportions, and which water a canal course accepts.

Both of these exist for one course — IRONMAN 70.3 Versailles, swum in the
Grand Canal of the château — and both are the kind of change that is easy to
undo by accident later, because everything else in the catalogue swims in the
sea and would not notice.

The risk being guarded against is not that Versailles breaks loudly. It is
that a tidy-up removes `reservoir` from the canal preference, or drops the
per-course aspect, and the only symptom is one race quietly failing to
generate on a machine nobody is watching.
"""
from __future__ import annotations

import math

import pytest

from course_ingest.spec import SpecError, load_spec
from course_ingest.stages.s03_swim import _SUBTYPE_PREFERENCE, _shape_vertices

SPECS = __import__("pathlib").Path(__file__).resolve().parents[1] / "specs"


def _metres(a, b, lat):
    return math.hypot(
        (b[0] - a[0]) * 111320 * math.cos(math.radians(lat)),
        (b[1] - a[1]) * 110540,
    )


def test_a_canal_course_accepts_reservoir_water() -> None:
    """Overture classifies the Grand Canal as a reservoir, not a canal.

    It is water with no current, so the classification is right and the
    preference list was wrong: the one course whose swim is famously in a
    canal could not find its canal.
    """
    assert "reservoir" in _SUBTYPE_PREFERENCE["canal"]
    # Still a canal first, and still not the sea.
    assert _SUBTYPE_PREFERENCE["canal"][0] == "canal"
    assert "ocean" not in _SUBTYPE_PREFERENCE["canal"]


def test_the_rectangle_narrows_as_the_aspect_rises() -> None:
    """The override's whole purpose: same distance, a shape water can hold."""
    anchor = (2.10079, 48.81001)
    bearing = math.radians(291.2)
    perimeter = 1900.0

    wide = _shape_vertices(anchor, bearing, "rectangle_one_lap", perimeter, 3.0)
    narrow = _shape_vertices(anchor, bearing, "rectangle_one_lap", perimeter, 40.0)

    def long_and_short(v):
        return _metres(v[1], v[2], anchor[1]), _metres(v[2], v[3], anchor[1])

    wide_long, wide_short = long_and_short(wide)
    narrow_long, narrow_short = long_and_short(narrow)

    # 3:1 is 710 x 240 m — a bay shape, and far wider than any canal.
    assert wide_long == pytest.approx(712, abs=8)
    assert wide_short == pytest.approx(238, abs=8)
    # 40:1 is 927 x 23 m — the out-and-back a canal race is actually swum as.
    assert narrow_long == pytest.approx(927, abs=8)
    assert narrow_short == pytest.approx(23, abs=3)

    # The distance swum is the same either way; only the shape moved.
    for verts in (wide, narrow):
        perim = sum(
            _metres(verts[i], verts[i + 1], anchor[1]) for i in range(len(verts) - 1)
        )
        assert perim == pytest.approx(perimeter, rel=0.01)


def test_versailles_declares_the_override_and_everything_else_does_not() -> None:
    """A per-course setting that quietly became global would move every bundle."""
    versailles = load_spec(SPECS / "12-versailles-703.yaml")
    assert versailles.swim_rectangle_aspect == 40.0
    assert versailles.water_kind == "canal"

    for path in sorted(SPECS.glob("*.yaml")):
        spec = load_spec(path)
        if spec.slug == "versailles-703":
            continue
        assert spec.swim_rectangle_aspect is None, (
            f"{path.name} overrides the swim aspect; only Versailles should"
        )


def test_an_inverted_aspect_is_refused(tmp_path) -> None:
    """`rectangle_aspect` is long:short. Below 1.0 it is short:long, and the
    swim would silently come out across the canal rather than along it."""
    source = (SPECS / "12-versailles-703.yaml").read_text(encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text(source.replace("rectangle_aspect: 40.0", "rectangle_aspect: 0.5"))
    with pytest.raises(SpecError, match="long:short"):
        load_spec(bad)
