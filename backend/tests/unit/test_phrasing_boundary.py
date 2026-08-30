"""STRUCTURAL GUARANTEE 3 — the language model cannot influence a number.

Law 1 says the solver decides and the model phrases. This file tries to break
that, by driving a deliberately adversarial model through every entry point
the phrasing layer has and asserting that nothing it returns changes a value
an athlete would see.

The model here is not a mock that returns a fixed string: it returns exactly
the kind of output a real model produces when it decides to be helpful —
plausible numbers, adjusted numbers, rounded numbers, extra numbers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import raceos
from raceos.config import Settings
from raceos.services import phrasing_service as phrasing


@pytest.fixture
def enabled() -> Settings:
    """Phrasing on, with no credential — the model is injected."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        phrasing_enabled=True,
        phrasing_model_id="test-model",
    )


@pytest.fixture(autouse=True)
def _reset_model():
    yield
    phrasing.set_model(None)


def _drive(replies: list[str]) -> phrasing.RecordingPhrasingModel:
    model = phrasing.RecordingPhrasingModel(replies=replies)
    phrasing.set_model(model)
    return model


DETERMINISTIC = "Hold 208 w on the bike and 5:12/km on the run."


# ---------------------------------------------------------------------------
# The adversary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attack", "why"),
    [
        (
            "Hold 214 w on the bike and 5:12/km on the run.",
            "a changed number",
        ),
        (
            "Hold 208 w on the bike and 5:10/km on the run.",
            "a rounded number",
        ),
        (
            "Hold 208 w on the bike, 5:12/km on the run, and 82 g/hr of carbs.",
            "an added number the solver never produced",
        ),
        (
            "Hold about 210 w on the bike and 5:12/km on the run.",
            "a hedged number",
        ),
        (
            "Hold 208.5 w on the bike and 5:12/km on the run.",
            "a number given false precision",
        ),
        (
            "Your bike target is 208 w — that is 2.8 W/kg for a 75 kg athlete.",
            "a derived number computed from athlete data",
        ),
    ],
)
def test_no_invented_or_altered_number_ever_reaches_an_athlete(
    enabled: Settings, attack: str, why: str
) -> None:
    _drive([attack])
    result = phrasing.phrase(phrasing.PhrasingRequest(deterministic_text=DETERMINISTIC), enabled)

    assert result.text == DETERMINISTIC, f"{why} was shown to the athlete"
    assert result.rewritten is False
    assert result.reason


def test_a_rewrite_that_only_changes_wording_is_accepted(enabled: Settings) -> None:
    """The layer has to be *useful*, or it is theatre.

    Dropping a number is allowed — "your bike target" instead of "208 w" is a
    legitimate improvement — but no new one may appear.
    """
    _drive(["On the bike hold 208 w. On the run, 5:12/km."])
    result = phrasing.phrase(phrasing.PhrasingRequest(deterministic_text=DETERMINISTIC), enabled)

    assert result.rewritten is True
    assert result.text == "On the bike hold 208 w. On the run, 5:12/km."


def test_dropping_a_number_entirely_is_allowed(enabled: Settings) -> None:
    _drive(["Hold your bike target, then settle into your run pace."])
    result = phrasing.phrase(phrasing.PhrasingRequest(deterministic_text=DETERMINISTIC), enabled)
    assert result.rewritten is True
    assert "208" not in result.text


# ---------------------------------------------------------------------------
# Failure is always a fallback, never a wrong number
# ---------------------------------------------------------------------------


def test_an_empty_reply_falls_back(enabled: Settings) -> None:
    _drive([""])
    assert (
        phrasing.phrase(phrasing.PhrasingRequest(deterministic_text=DETERMINISTIC), enabled).text
        == DETERMINISTIC
    )


def test_a_rambling_reply_falls_back(enabled: Settings) -> None:
    """Most often this is a model that started explaining itself."""
    _drive(["Certainly! Here is a rewrite. " + ("Hold your target. " * 60)])
    assert (
        phrasing.phrase(phrasing.PhrasingRequest(deterministic_text=DETERMINISTIC), enabled).text
        == DETERMINISTIC
    )


def test_a_provider_failure_falls_back(enabled: Settings) -> None:
    class Exploding(phrasing.PhrasingModel):
        def rewrite(self, request, settings):  # type: ignore[no-untyped-def]
            raise TimeoutError("provider timed out")

    phrasing.set_model(Exploding())
    result = phrasing.phrase(phrasing.PhrasingRequest(deterministic_text=DETERMINISTIC), enabled)
    assert result.text == DETERMINISTIC
    assert result.reason == "TimeoutError"


def test_phrasing_is_off_by_default() -> None:
    """The product is complete with the language layer switched off."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.phrasing_enabled is False

    _drive(["Hold 999 w."])
    result = phrasing.phrase(phrasing.PhrasingRequest(deterministic_text=DETERMINISTIC), settings)
    assert result.text == DETERMINISTIC
    assert result.reason == "disabled"


# ---------------------------------------------------------------------------
# The token scan itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("208 w", {"208"}),
        ("5:12/km", {"5:12"}),
        ("11:45:03", {"11:45:03"}),
        ("70.3", {"70.3"}),
        ("T1 and T2", {"1", "2"}),
        ("no numbers here", set()),
        ("2.8 W/kg for 75 kg", {"2.8", "75"}),
    ],
)
def test_the_numeric_scan_is_deliberately_broad(text: str, expected: set[str]) -> None:
    """Better to reject a harmless rewrite than to let one number through."""
    assert phrasing.numeric_tokens(text) == expected


# ---------------------------------------------------------------------------
# There is exactly one way in
# ---------------------------------------------------------------------------


PACKAGE_ROOT = Path(raceos.__file__).parent


def _sources(subpath: str = "") -> list[Path]:
    """Read from disk rather than through ``inspect``.

    A package object has no source of its own, and importing every module to
    read it would make this test depend on import side effects.
    """
    return sorted((PACKAGE_ROOT / subpath).rglob("*.py"))


def test_the_solver_package_never_imports_the_phrasing_layer() -> None:
    """The wall, from the solver's side.

    A solver that could call the language layer would be a solver whose
    numbers a model could touch, whatever the validation said.
    """
    for path in _sources("solver"):
        source = path.read_text(encoding="utf-8")
        assert (
            "phrasing" not in source
        ), f"{path.relative_to(PACKAGE_ROOT)} references the phrasing layer"


def test_every_caller_goes_through_the_single_entry_point() -> None:
    """A second phrasing path would be a second place validation is skipped.

    Only ``phrase`` and ``phrase_text`` may reach a model; anything calling
    ``rewrite`` or ``get_model`` directly would bypass :func:`validate`.
    """
    offenders: list[str] = []
    for path in _sources():
        if path.name == "phrasing_service.py":
            continue
        source = path.read_text(encoding="utf-8")
        if ".rewrite(" in source or "get_model(" in source:
            offenders.append(str(path.relative_to(PACKAGE_ROOT)))

    assert not offenders, (
        f"these modules reach the phrasing model directly, bypassing " f"validation: {offenders}"
    )


def test_the_boundary_is_described_for_operators() -> None:
    """The claim is served, not left in a document nobody reads."""
    described = phrasing.describe_boundary()
    assert "solver decides" in described["law"].lower()
    assert any("constraint" in item for item in described["model_never_receives"])
    assert "deterministic" in described["on_failure"].lower()
