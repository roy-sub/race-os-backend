"""Help and FAQ content.

Public: help that needs a session is help nobody can reach when they are
locked out, and "I cannot sign in" is exactly when someone reads it.

The content lives in :mod:`raceos.api.help_content`, one flat table, so
changing a sentence is a one-line edit. Serving it rather than shipping it in
the client is what lets the command palette search it and an error message
link into it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from raceos.api.errors import NotFound
from raceos.api.help_content import (
    CATEGORY_ORDER,
    HELP_ARTICLES,
    HelpArticle,
    ordered_articles,
    search_articles,
)
from raceos.api.schemas.help import HelpArticleOut, HelpArticleSummary

router = APIRouter(prefix="/api/v1/help", tags=["help"])


def _summary(article: HelpArticle) -> HelpArticleSummary:
    return HelpArticleSummary(
        slug=article.slug,
        category=article.category,
        title=article.title,
        summary=article.summary,
        read_minutes=article.read_minutes,
    )


@router.get("", summary="Every help article")
def list_articles(
    q: Annotated[str | None, Query(description="Match title, summary and body")] = None,
    category: Annotated[str | None, Query(description="One of the published categories")] = None,
) -> list[HelpArticleSummary]:
    """Grouped by category in reading order, not alphabetically.

    "Getting started" before "Billing" is the whole point of having an order.
    """
    if q:
        return [_summary(article) for article in search_articles(q, limit=50)]
    rows = ordered_articles()
    if category:
        rows = [article for article in rows if article.category == category]
    return [_summary(article) for article in rows]


@router.get("/categories", summary="The published categories, in reading order")
def list_categories() -> list[str]:
    return list(CATEGORY_ORDER)


@router.get("/{slug}", summary="One article in full")
def get_article(slug: str) -> HelpArticleOut:
    article = HELP_ARTICLES.get(slug)
    if article is None:
        raise NotFound("There is no help article at that address.")
    return HelpArticleOut(
        slug=article.slug,
        category=article.category,
        title=article.title,
        summary=article.summary,
        read_minutes=article.read_minutes,
        body=article.body,
    )
