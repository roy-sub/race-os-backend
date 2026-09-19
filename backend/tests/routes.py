"""Walking the live route table, which is not a flat list any more.

FastAPI 0.141 stopped putting an included router's routes directly on
`app.routes`. What sits there now is an `_IncludedRouter` wrapper holding the
original router and the prefix it was mounted under, and the wrapper has no
`path` of its own.

Three tests read the route table, and each broke differently. Two raised
`AttributeError` on the wrapper, which is the harmless failure. The third —
`test_no_endpoint_accepts_an_athlete_id_for_a_constraint_write`, which exists
to prove no constraint route lets a caller name another athlete — used
`getattr(route, "path", "")` and so found nothing at all, asserted over an
empty list and passed. A security test that silently stops covering anything
is worse than one that fails, so the traversal lives here, once, and returns
the full mounted path rather than the router-relative one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteInfo:
    """One route, at the path it is actually reachable on."""

    path: str
    methods: frozenset[str]


def iter_routes(app: object) -> list[RouteInfo]:
    """Every route on `app`, however deeply it is mounted."""
    found: list[RouteInfo] = []

    def walk(node: object, prefix: str) -> None:
        for route in getattr(node, "routes", ()) or ():
            original = getattr(route, "original_router", None)
            if original is not None:
                context = getattr(route, "include_context", None)
                walk(original, prefix + str(getattr(context, "prefix", "") or ""))
                continue

            path = getattr(route, "path", None)
            if path is not None:
                found.append(
                    RouteInfo(
                        path=prefix + str(path),
                        methods=frozenset(getattr(route, "methods", ()) or ()),
                    )
                )

    walk(app, "")
    return found
