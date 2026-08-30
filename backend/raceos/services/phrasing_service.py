"""The language layer, and the wall around it.

**Law 1: the solver decides, the model phrases.** Every number an athlete sees
comes from the deterministic solver. A language model may only rewrite already-
correct sentences into better ones. It cannot compute, adjust, round, or
introduce a value.

That is not a matter of prompt discipline here. It is enforced structurally:

* The model is handed **rendered text and a whitelist of allowed tokens**, not
  the numbers themselves as data to reason about.
* Its output is **validated before use**: every numeric token in the returned
  text must already appear in the input. A model that invents "6 w" where the
  input said "8 w" is rejected and the deterministic text is used instead.
* The fallback is always the deterministic string. Phrasing failing is a
  cosmetic degradation, never a correctness one — which is why
  ``PHRASING_ENABLED`` defaults to off and the product is complete without it.

``tests/unit/test_phrasing_boundary.py`` is the third structural guarantee: it
drives a deliberately adversarial model through every entry point and asserts
that no number it returns ever reaches an athlete.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from raceos.config import Settings
from raceos.logging import get_logger

logger = get_logger(__name__)

#: Anything that reads as a quantity. Deliberately broad — it is better to
#: reject a harmless rewrite than to let one number through.
#:
#: Matches integers, decimals, clock times (``1:56``, ``11:45:03``), and
#: percentages. Ordinals inside words (``T1``, ``70.3``) are covered because
#: the scan is on the token, not on word boundaries.
NUMERIC_TOKEN = re.compile(r"\d+(?:[.:]\d+)*")


class PhrasingRejectedError(ValueError):
    """The candidate text failed validation. The caller uses the original."""


@dataclass(frozen=True)
class PhrasingRequest:
    """What the model is allowed to see.

    ``deterministic_text`` is already correct and already shippable. The model
    is being asked to improve its *reading*, nothing else.
    """

    deterministic_text: str
    #: A short description of the surface, so tone can differ between a race
    #: card and a drift alert. Never contains athlete data.
    context: str = "general"
    #: Extra strings the rewrite may legitimately contain — a course name, a
    #: leg name. Never numbers.
    allowed_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhrasingResult:
    text: str
    #: True when the model's text was used; False when the deterministic
    #: string was kept. Recorded so the proportion is observable.
    rewritten: bool
    reason: str = ""


def numeric_tokens(text: str) -> set[str]:
    """Every quantity-shaped token in *text*."""
    return set(NUMERIC_TOKEN.findall(text))


def validate(candidate: str, request: PhrasingRequest) -> str:
    """Return *candidate* if it is safe to show. Raise otherwise.

    The rule that matters: **the candidate may not contain a number the
    deterministic text did not already contain.** Not "a plausible number",
    not "a number within tolerance" — the same tokens, or fewer.

    Dropping a number is allowed (a rewrite may legitimately say "your bike
    target" instead of "208 w"); inventing or altering one is not.
    """
    text = (candidate or "").strip()
    if not text:
        raise PhrasingRejectedError("the model returned nothing")
    if len(text) > len(request.deterministic_text) * 3 + 200:
        # A rewrite three times the length is not a rewrite. Most often this
        # is a model that started explaining itself.
        raise PhrasingRejectedError("the rewrite is disproportionately long")

    invented = numeric_tokens(text) - numeric_tokens(request.deterministic_text)
    if invented:
        raise PhrasingRejectedError(
            f"the rewrite introduced numbers that were not in the input: " f"{sorted(invented)}"
        )
    return text


# ---------------------------------------------------------------------------
# The provider boundary
# ---------------------------------------------------------------------------


class PhrasingModel(ABC):
    """A language model, behind an interface that hands it text only."""

    @abstractmethod
    def rewrite(self, request: PhrasingRequest, settings: Settings) -> str:
        """Return a candidate rewrite. May raise; the caller falls back."""


class DisabledPhrasingModel(PhrasingModel):
    """The default. Returns the deterministic text unchanged.

    A real implementation, not a stub: with phrasing off the product is
    complete, every string is already correct, and nothing degrades.
    """

    def rewrite(self, request: PhrasingRequest, settings: Settings) -> str:
        return request.deterministic_text


@dataclass
class RecordingPhrasingModel(PhrasingModel):
    """A model whose replies are supplied by the caller.

    Used by the boundary test to drive adversarial output through the real
    validation path, and by a local run to preview copy without a key.
    """

    replies: list[str] = field(default_factory=list)
    seen: list[PhrasingRequest] = field(default_factory=list)

    def rewrite(self, request: PhrasingRequest, settings: Settings) -> str:
        self.seen.append(request)
        if not self.replies:
            return request.deterministic_text
        return self.replies.pop(0)


_model: PhrasingModel | None = None


def get_model(settings: Settings | None = None) -> PhrasingModel:
    global _model
    if _model is not None:
        return _model
    _model = DisabledPhrasingModel()
    return _model


def set_model(model: PhrasingModel | None) -> None:
    """Override the model. For tests and for a local copy preview."""
    global _model
    _model = model


# ---------------------------------------------------------------------------
# The one entry point
# ---------------------------------------------------------------------------


def phrase(request: PhrasingRequest, settings: Settings) -> PhrasingResult:
    """Improve the reading of an already-correct string.

    Every caller goes through here, so the validation cannot be bypassed by
    adding a second phrasing path later. On any failure — disabled, timeout,
    provider error, failed validation — the deterministic text is returned and
    the athlete sees a correct sentence.
    """
    if not settings.phrasing_enabled:
        return PhrasingResult(text=request.deterministic_text, rewritten=False, reason="disabled")

    try:
        candidate = get_model(settings).rewrite(request, settings)
        text = validate(candidate, request)
    except PhrasingRejectedError as rejection:
        logger.warning(
            "phrasing.rejected",
            extra={"context": request.context, "reason": str(rejection)},
        )
        return PhrasingResult(
            text=request.deterministic_text, rewritten=False, reason=str(rejection)
        )
    except Exception as error:  # provider failure, timeout, anything
        logger.warning(
            "phrasing.unavailable",
            extra={"context": request.context, "error_type": type(error).__name__},
        )
        return PhrasingResult(
            text=request.deterministic_text,
            rewritten=False,
            reason=f"{type(error).__name__}",
        )

    return PhrasingResult(text=text, rewritten=text != request.deterministic_text)


def phrase_text(
    deterministic_text: str,
    settings: Settings,
    *,
    context: str = "general",
    allowed_terms: tuple[str, ...] = (),
) -> str:
    """Convenience wrapper returning just the text."""
    return phrase(
        PhrasingRequest(
            deterministic_text=deterministic_text,
            context=context,
            allowed_terms=allowed_terms,
        ),
        settings,
    ).text


def describe_boundary() -> dict[str, Any]:
    """What the layer is allowed to do. Surfaced on the ops health page.

    Written down and served rather than left in a document, because "the LLM
    cannot touch a number" is a claim the product makes to users and an
    operator should be able to check it without reading the source.
    """
    return {
        "law": "The solver decides every number. The model may only rephrase.",
        "model_receives": [
            "the already-correct rendered sentence",
            "a context label naming the surface",
        ],
        "model_never_receives": [
            "athlete constraints",
            "raw solver state",
            "any value it could compute with",
        ],
        "validation": (
            "Every numeric token in the rewrite must already appear in the "
            "input. Dropping a number is allowed; inventing or altering one "
            "is rejected."
        ),
        "on_failure": "The deterministic text is used. Phrasing is cosmetic.",
    }
