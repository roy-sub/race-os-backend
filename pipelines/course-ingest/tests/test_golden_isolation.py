"""The golden solver fixtures and the pipeline's output must stay separate.

`SOLVER_MODEL.md` §B.1 defines `C-TRAM`, `C-FLAT`, `C-ALTA`, `C-HALF`, `C-OLY`
and `C-SPR` with synthetic node series generated to reproduce exact net
gradients. They are frozen inputs to the solver's golden-file regression suite.

`C-TRAM` shares a name and coordinates with the seeded Tramuntana Full course,
which is exactly why this guard exists. If a golden case ever resolved to a
pipeline-generated bundle, regenerating a course would silently move the golden
expectations, and the solver's determinism guarantee would stop being a property
of the solver and become a property of the routing engine.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PIPELINE_ROOT.parent.parent
GOLDEN_DIR = REPO_ROOT / "backend" / "tests" / "golden" / "courses"
GOLDEN_BUILDER = REPO_ROOT / "backend" / "tests" / "golden" / "build_golden_courses.py"

#: Any path the pipeline is capable of writing to.
PIPELINE_OUTPUT_ROOTS = (
    PIPELINE_ROOT / "out",
    PIPELINE_ROOT / "specs",
    PIPELINE_ROOT / ".cache",
)

needs_golden = pytest.mark.skipif(
    not GOLDEN_DIR.exists(), reason="golden fixtures not present in this checkout"
)


def golden_files():
    return sorted(GOLDEN_DIR.glob("*.json"))


@needs_golden
def test_all_six_golden_courses_are_checked_in_as_static_files():
    ids = {p.stem for p in golden_files()}
    assert ids == {"C-TRAM", "C-FLAT", "C-ALTA", "C-HALF", "C-OLY", "C-SPR"}, ids


@needs_golden
def test_no_golden_course_resolves_to_a_pipeline_output_path():
    for path in golden_files():
        resolved = path.resolve()
        for root in PIPELINE_OUTPUT_ROOTS:
            assert root.resolve() not in resolved.parents, (
                f"{path.name} resolves inside the pipeline output tree {root}"
            )


@needs_golden
def test_every_golden_course_declares_itself_synthetic():
    for path in golden_files():
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["synthetic"] is True, path.name
        assert data["source"] == "SOLVER_MODEL.md §B.1", path.name
        assert "course_bundle" not in data, (
            f"{path.name} carries a course_bundle block: it looks like pipeline output"
        )


@needs_golden
def test_golden_courses_are_not_pipeline_bundles():
    """Structural separation: a golden file has none of the pipeline's markers."""
    emitted = PIPELINE_ROOT / "out" / "bundles"
    generated = {p.name for p in emitted.glob("*.bundle.json")} if emitted.exists() else set()
    for path in golden_files():
        assert path.name not in generated
        text = path.read_text(encoding="utf-8")
        assert "raceos-course-ingest" not in text, path.name
        assert "bundle_asset_key" not in text, path.name
        assert "SRID=4326" not in text, path.name


@needs_golden
def test_the_golden_builder_does_not_import_the_pipeline():
    source = GOLDEN_BUILDER.read_text(encoding="utf-8")
    code_lines = [
        line for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "course_ingest" in line
    ]
    assert not code_lines, f"golden builder imports the pipeline: {code_lines}"


def test_the_pipeline_never_writes_into_the_golden_directory():
    """Belt and braces: no pipeline module names the golden path."""
    for module in (PIPELINE_ROOT / "course_ingest").rglob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert "tests/golden" not in text, f"{module} references the golden directory"
        assert "C-TRAM" not in text, f"{module} references a golden course id"
