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


# ---------------------------------------------------------------------------
# The provider, and the wall over a real wire format
#
# The adversary above drives an injected model. These drive the actual HTTP
# adapter against a stub transport, so the boundary is exercised through the
# code path a configured deployment runs — parsing, error handling and all.
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _StubClient:
    """Records what was sent and returns what it was told to."""

    def __init__(self, response: _StubResponse) -> None:
        self._response = response
        self.sent: dict[str, object] = {}
        self.closed = False

    def post(self, path: str, json: dict[str, object]) -> _StubResponse:
        self.sent = {"path": path, **json}
        return self._response

    def close(self) -> None:
        self.closed = True


def _reply(text: str) -> _StubResponse:
    return _StubResponse(200, {"choices": [{"message": {"content": text}}]})


@pytest.fixture
def configured() -> Settings:
    """Phrasing on, with a model id and a credential."""
    from pydantic import SecretStr

    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        phrasing_enabled=True,
        phrasing_model_id="test-model",
        phrasing_model_api_key=SecretStr("sk-not-a-real-key"),
    )


def _with_http(settings: Settings, response: _StubResponse) -> _StubClient:
    client = _StubClient(response)
    phrasing.set_model(phrasing.HttpPhrasingModel(settings, client=client))
    return client


def test_the_adapter_is_only_built_when_it_is_fully_configured(
    configured: Settings, enabled: Settings
) -> None:
    """Three conditions, all required. Any missing yields the disabled model,
    which returns the deterministic text — so a half-configured deployment is
    correct rather than broken."""
    phrasing.set_model(None)
    assert isinstance(phrasing.get_model(configured), phrasing.HttpPhrasingModel)

    # Enabled, with a model id, but no credential.
    phrasing.set_model(None)
    assert isinstance(phrasing.get_model(enabled), phrasing.DisabledPhrasingModel)

    # Off entirely.
    phrasing.set_model(None)
    assert isinstance(
        phrasing.get_model(Settings(_env_file=None)),  # type: ignore[call-arg]
        phrasing.DisabledPhrasingModel,
    )


def test_the_model_is_sent_the_sentence_and_nothing_it_could_compute_with(
    configured: Settings,
) -> None:
    """It is being asked to improve a reading. Handing it the inputs would be
    inviting it to check the arithmetic."""
    client = _with_http(configured, _reply("On the bike, hold 208 w. Then 5:12/km."))
    phrasing.phrase(
        phrasing.PhrasingRequest(deterministic_text=DETERMINISTIC, context="race_card"),
        configured,
    )

    messages = client.sent["messages"]
    assert isinstance(messages, list)
    user = next(m for m in messages if m["role"] == "user")
    assert DETERMINISTIC in user["content"]
    assert "race_card" in user["content"]
    # No athlete state anywhere in the payload.
    for forbidden in ("threshold", "ftp", "sweat_rate", "weight", "constraint"):
        assert forbidden not in str(client.sent).lower()


def test_sampling_is_off_so_a_plan_does_not_reword_itself_on_reload(
    configured: Settings,
) -> None:
    client = _with_http(configured, _reply("On the bike, hold 208 w. Then 5:12/km."))
    phrasing.phrase(phrasing.PhrasingRequest(deterministic_text=DETERMINISTIC), configured)
    assert client.sent["temperature"] == 0.0
    assert client.sent["model"] == "test-model"


def test_an_invented_number_over_http_is_still_rejected(configured: Settings) -> None:
    """The wall does not care which model returned the text."""
    _with_http(configured, _reply("Hold 214 w on the bike and 5:12/km on the run."))
    result = phrasing.phrase(phrasing.PhrasingRequest(deterministic_text=DETERMINISTIC), configured)
    assert result.text == DETERMINISTIC
    assert result.rewritten is False


def test_a_provider_error_falls_back_without_echoing_the_body(
    configured: Settings,
) -> None:
    """A provider error body can echo the request that caused it."""
    _with_http(configured, _StubResponse(500, {"error": {"message": DETERMINISTIC}}))
    result = phrasing.phrase(phrasing.PhrasingRequest(deterministic_text=DETERMINISTIC), configured)
    assert result.text == DETERMINISTIC
    assert result.rewritten is False
    assert "500" in result.reason or "PhrasingUnavailableError" in result.reason


def test_an_unusable_body_falls_back(configured: Settings) -> None:
    _with_http(configured, _StubResponse(200, {"not": "what we expected"}))
    result = phrasing.phrase(phrasing.PhrasingRequest(deterministic_text=DETERMINISTIC), configured)
    assert result.text == DETERMINISTIC
    assert result.rewritten is False


def test_the_provider_description_never_carries_the_key(configured: Settings) -> None:
    """It is served on the ops overview, which is a screen and a log line."""
    phrasing.set_model(None)
    described = phrasing.describe_provider(configured)

    assert described["enabled"] is True
    assert described["credential_present"] is True
    assert described["model_id"] == "test-model"
    assert "sk-not-a-real-key" not in str(described)


def test_the_prompt_says_it_is_not_the_guarantee() -> None:
    """A layer whose safety rested on a model following instructions would
    have no safety at all, and the description says so."""
    described = phrasing.describe_boundary()
    assert "validation runs on every reply" in described["prompt_is_not_the_guarantee"]
