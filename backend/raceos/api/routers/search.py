"""One query across everything a person can navigate to.

Backs the command palette. Until now the only search in the product was
``GET /courses?q=``, which searches courses alone, and the palette's input in
the header was wired to nothing at all.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from raceos.api.deps import Config, DbSession, OptionalUser
from raceos.services import search_service

router = APIRouter(prefix="/api/v1/search", tags=["search"])


class SearchHitOut(BaseModel):
    """One result.

    ``ref`` is what the client needs to build a link — a course slug, a race or
    plan id, a help slug — rather than a URL. The frontend owns its route
    table; a server emitting ``/plan?plan=`` would be a second copy of it, and
    silently wrong the day a path changes.
    """

    kind: str
    ref: str
    title: str
    subtitle: str


@router.get("", summary="Search races, plans, courses and help")
def search(
    session: DbSession,
    viewer: OptionalUser,
    settings: Config,
    q: Annotated[str, Query(description="At least two characters", max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[SearchHitOut]:
    """Public, and scoped to the asker.

    A signed-out visitor gets courses and help; a signed-in athlete also gets
    their own races and plans, filtered by owner in SQL rather than after the
    fact. Courses reuse the directory's own visibility filter, so a search
    cannot surface a row the directory would not list.

    A query under two characters returns nothing rather than most of the
    library: it is almost always a keystroke on the way to a real one.
    """
    return [
        SearchHitOut(kind=hit.kind, ref=hit.ref, title=hit.title, subtitle=hit.subtitle)
        for hit in search_service.search(
            session, query=q, viewer=viewer, limit=limit, settings=settings
        )
    ]
