"""One check, applied to every piece of XML an athlete uploads.

GPX and TCX are XML, and both arrive from outside. Python's stdlib parser —
which `racefile` uses directly and `gpxpy` uses underneath — does not resolve
external entities, so this is not about XXE. It *does* expand internal ones,
which is the billion-laughs shape: a few hundred bytes of nested entity
declarations expand to gigabytes during parsing and take the worker down with
them. The upload size cap does not help, because the whole point of the attack
is that the input is small.

The fix is not a parser setting, because there isn't one that turns internal
entity expansion off. It is to refuse the only construct that makes the attack
possible. A document type declaration has no legitimate place in a file
exported by a watch, a bike computer or a mapping site: neither the GPX 1.1
nor the TCX v2 schema uses one, and no exporter in the wild emits one. So a
file that carries one is not a file we failed to read — it is a file we
decline to read, and saying so plainly is more useful than a parser timeout.
"""

from __future__ import annotations

#: Only the prolog can carry a DTD, and it precedes the root element. Scanning
#: a bounded prefix keeps the check O(1) against a 20 MB upload rather than
#: O(n); the internal subset of a billion-laughs payload is tiny and sits at
#: the very front, because it has to be declared before it is referenced.
_PROLOG_BYTES = 64 * 1024

_FORBIDDEN = (b"<!doctype", b"<!entity")


class XmlNotAllowedError(ValueError):
    """The upload carried a construct we will not parse."""


def ensure_no_doctype(data: bytes, *, filename: str) -> None:
    """Raise when `data` declares a document type or an entity.

    Case-insensitive because XML keywords are case-sensitive but attackers are
    not obliged to be tidy, and a parser that accepted `<!DoCtYpE>` while this
    check looked for `<!DOCTYPE` would be worse than no check at all.
    """
    prolog = data[:_PROLOG_BYTES].lower()
    for marker in _FORBIDDEN:
        if marker in prolog:
            raise XmlNotAllowedError(
                f"{filename} contains a document type declaration, which this "
                f"importer does not accept. Re-export the file from your watch, "
                f"head unit or mapping tool and upload it again."
            )
