"""Canonical JSON for ``SolveInput`` and ``SolveOutput``.

Two jobs, and both depend on the same property:

``solve_input_hash``
    ``sha256(canonical_json(SolveInput))``. Identical hash implies
    byte-identical output, which is what lets the plan service skip a re-solve.
    That only holds if the serialisation is genuinely canonical — stable key
    order, fixed float formatting, no dependence on dictionary insertion order.

golden files
    The frozen expectations are these bytes. A diff is a behaviour change, and
    CI blocks on it.

**Floats are formatted with ``repr``, which in Python 3 is the shortest string
that round-trips exactly.** That is deterministic across platforms for IEEE-754
doubles, unlike ``%.17g`` (which is exact but noisy) or ``%.6f`` (which is
stable but lossy, and would let two genuinely different inputs hash the same).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import date, time
from enum import Enum
from typing import Any


def canonical(value: Any) -> Any:
    """Recursively convert to JSON-safe primitives with a stable ordering."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        # `repr` is the shortest round-tripping representation; going through
        # it keeps the hash a function of the number rather than of its
        # formatting.
        return repr(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, dict):
        # Sorted keys: a dict's insertion order must not reach the hash.
        return {str(key): canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list | tuple):
        return [canonical(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: canonical(getattr(value, field.name))
            for field in sorted(fields(value), key=lambda f: f.name)
        }
    raise TypeError(f"cannot canonicalise {type(value).__name__}")  # pragma: no cover


def canonical_json(value: Any) -> str:
    """One canonical JSON document. Sorted keys, no incidental whitespace."""
    return json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def solve_input_hash(request: Any) -> str:
    """``sha256`` of the canonical input. The re-solve skip key."""
    return hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()


def golden_json(value: Any) -> str:
    """The frozen golden representation: canonical, but indented to be diffable.

    A golden file is read by a human when it changes, so the two extra
    kilobytes of indentation buy a reviewable diff. The *content* is identical
    to :func:`canonical_json`.
    """
    return json.dumps(canonical(value), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
