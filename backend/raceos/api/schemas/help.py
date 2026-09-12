"""Help article shapes."""

from __future__ import annotations

from pydantic import BaseModel


class HelpArticleSummary(BaseModel):
    """A card or a search hit. Deliberately without the body.

    The list endpoint returns every article, and sending eight full bodies to
    render eight cards would be most of a page of prose nobody asked for.
    """

    slug: str
    category: str
    title: str
    summary: str
    read_minutes: int


class HelpArticleOut(HelpArticleSummary):
    #: Markdown. Rendered by the client, which already owns the typography.
    body: str
