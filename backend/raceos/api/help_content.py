"""Help and FAQ content, one entry per article.

Copy, kept in code for the same two reasons ``constraint_copy`` is: it is
words rather than model behaviour, and having one flat table means changing a
sentence is a one-line edit by whoever owns the words.

It is served rather than shipped in the frontend because a help library that
lives in the client cannot be searched by the command palette, cannot be
linked to from an error, and has to be redeployed with the app to fix a typo.
The frontend previously held a single hardcoded article whose "related"
links pointed at three guides that did not exist.

**Every claim here is about behaviour this system actually has**, and each
article names the part of the code or the endpoint it describes. Nothing
below quotes a statistic, a study, or a number the product cannot produce —
an article that invented one would be indistinguishable, to a reader, from
the solver's own output.
"""

from __future__ import annotations

from typing import NamedTuple


class HelpArticle(NamedTuple):
    """One article. ``body`` is Markdown, rendered by the client."""

    slug: str
    category: str
    title: str
    #: One sentence. This is what a search result and a card show.
    summary: str
    body: str
    #: Reading time in whole minutes, from the body's own length rather than
    #: asserted: see :func:`read_minutes`.
    #:
    #: Declared as a field so a caller never has to recompute it, and
    #: computed once at import.
    read_minutes: int = 0


#: Roughly the rate an adult reads prose on screen. Used only to turn a body
#: length into "3 min", so precision past the nearest minute is meaningless.
WORDS_PER_MINUTE = 220


def read_minutes(body: str) -> int:
    return max(1, round(len(body.split()) / WORDS_PER_MINUTE))


_ARTICLES: list[HelpArticle] = [
    HelpArticle(
        slug="i-do-not-know-my-ftp",
        category="Getting started",
        title="I do not know my FTP. Can I still use this?",
        summary=(
            "Yes. Every constraint has an estimator behind two plain questions, "
            "and the value it produces is marked as estimated wherever it appears."
        ),
        body="""
Yes, and you will not be nagged about it.

Each of the eight constraints the solver reads has an estimator: two
plain-language questions instead of a number. Answer them and you get a value
you can race on.

An estimated value carries **full numeric weight**. The solver does not
quietly pad it or plan more conservatively around it — the estimator's job is
to produce your real number as well as two questions can, not a cautious one.

What does change is that the value is stamped `estimated` for the rest of its
life. That stamp travels everywhere the number appears: the plan screen, the
"Why this?" drawer, the printed race card's provenance footer. When you later
measure the thing properly, you replace the estimate and the stamp changes
with it.

You can see which of your values are estimates, and which the solve actually
depended on, in **Settings → Constraints**.
""".strip(),
    ),
    HelpArticle(
        slug="where-the-numbers-come-from",
        category="How it works",
        title="Where do the numbers come from?",
        summary=(
            "A deterministic solver produces every figure; a language model may "
            "only rephrase sentences, and is rejected if it introduces a number."
        ),
        body="""
Every number you see comes from a deterministic constraint solver. Given
identical inputs it returns an identical plan — no sampling, no temperature,
no creativity. That property is not a claim: a golden-file suite records the
solver's output byte for byte, and a difference blocks a deploy.

A language model is involved in exactly one place, and it is walled off. It is
handed already-correct rendered sentences and asked to improve how they read.
Its output is then checked: **every numeric token in the rewrite must already
appear in the input**. A model that alters a power target by a single watt is
rejected and the original sentence is used. Dropping a number is allowed;
inventing or altering one is not.

If the phrasing layer is unavailable, times out, or returns something that
fails that check, you get the deterministic sentence. Phrasing failing is a
cosmetic degradation, never a correctness one.

Any value on a plan can show you the constraint that produced it. Open the
"Why this?" drawer beside it.
""".strip(),
    ),
    HelpArticle(
        slug="what-if-the-forecast-changes",
        category="Race week",
        title="What if the forecast changes in race week?",
        summary=(
            "You get a drift alert naming what moved and what it costs, with the "
            "old value beside the new one. Your existing plan does not change "
            "underneath you."
        ),
        body="""
Your solved plan does not change on its own. That is deliberate and it is one
of the three rules this product is built on: a plan never changes under the
person holding it.

What happens instead is that a sweep re-solves your plan in the background and
compares the result with the one you have. If the difference is large enough
to matter, you get a **drift alert** naming what moved — the previous value
beside the new one — and what it costs you in minutes.

Applying it produces a new version. The old version stays readable forever,
because after the race you want to compare what happened against *the version
that was live on the day*, not against whatever it later became.

On the plan's **Conditions** tab you can also see the current forecast for
your start hour beside the one the plan was solved on, so you can judge the
gap yourself before any alert arrives.

Inside race week, structural edits to a plan are closed. Reading it, exporting
it and Race Mode all keep working.
""".strip(),
    ),
    HelpArticle(
        slug="my-race-is-not-in-the-directory",
        category="Getting started",
        title="My race is not in the directory.",
        summary=(
            "You can add it by uploading a route file for each leg. Until a real "
            "course exists we will not solve against a guess."
        ),
        body="""
A plan is built against a real course bundle: a surveyed route, elevation
sampled from terrain rather than from a watch barometer, and cut-offs from the
published athlete guide. Without one there is nothing honest to solve against,
and we would rather say no than produce a plan resting on an invented climb.

You can add the race yourself. **Races → Add a race** takes a GPX for each of
the three legs, validates each file while the picker is still in front of you,
and then builds a course from them. A course built this way is yours to plan
against, and it is marked with where its data came from.

The one thing to get right is the route file. A watch altimeter drifts tens of
metres over a long ride and swings with the weather, so the build reads
gradient from terrain rather than from the elevation recorded in your file — a
noisy profile does not make a slightly wrong plan, it makes one that invents
climbs.
""".strip(),
    ),
    HelpArticle(
        slug="what-you-keep-if-you-cancel",
        category="Billing",
        title="What happens to my plans if I cancel?",
        summary=(
            "You keep every plan you have already paid for, at full function and "
            "permanently. Only new solves and analysis lapse."
        ),
        body="""
Cancelling never takes away something you have already been charged for.

A race plan you paid for stays yours permanently: the race card, the exports,
the PDF, Race Mode. That is not a policy that could be changed by a setting —
the entitlement is attached to the captured payment for that race, not to a
subscription being live, so it keeps working after the subscription ends.

What lapses is the ability to start something *new* on a subscription tier:
another solve without paying per race, a post-race analysis, a constraint
calibration, the coach board.

Cancelling a subscription takes effect at the end of the period you have
already paid for, not immediately. Until that date nothing changes, and you
can undo it from **Settings → Billing**.

If a race is cancelled by its organiser, the plan stays in your account. Defer
to the next edition and re-solving costs nothing.
""".strip(),
    ),
    HelpArticle(
        slug="why-a-number-is-what-it-is",
        category="How it works",
        title="Why is this number what it is?",
        summary=(
            "Every value on a plan traces to the constraint that produced it, "
            "including the one that bound."
        ),
        body="""
Beside every number on a plan there is a "Why this?" drawer. It names the
constraint the value came from, your own figure for it, the unit, and where
that figure came from — measured, tested, estimated or typed in.

One of those constraints will be marked as **binding**. That is the one the
plan is actually limited by. It is usually not the one people expect: a bike
power target is frequently held below your fitness because a cut-off further
down the course is what the day is really constrained by, and riding to your
threshold would miss it.

The drawer is a snapshot taken at solve time, not a live lookup. If you change
a constraint afterwards, the plan you are looking at still shows the value it
was solved with — otherwise the numbers on the page and the reasons under them
would stop agreeing.

Where a plan rested on something you did not supply, the affected numbers are
marked as assumed, and the plan lists what was assumed.
""".strip(),
    ),
    HelpArticle(
        slug="overriding-a-ceiling",
        category="Fuelling",
        title="Can I override a ceiling the solver is holding?",
        summary=(
            "Yes, and it is recorded. An override is written before the solve "
            "that uses it and shown wherever the number appears."
        ),
        body="""
Yes. Your gut carbohydrate ceiling in particular is a wall the solver will not
cross on its own — it wants to give you more, because more is faster, and it
is not allowed to.

You can raise it for a solve. What happens then is that the override is
written down **before** the solve that consumes it, not after: an override is a
decision you made, and the record of it has to outlive the plan it produced.
It is then shown wherever the affected number appears, so a race card built on
an override says so.

There is a hard maximum that cannot be overridden, because past it the model
has no data at all and a number there would be a guess wearing the same
typeface as a result.
""".strip(),
    ),
    HelpArticle(
        slug="racing-with-no-signal",
        category="Race week",
        title="Will my plan work with no phone signal?",
        summary=(
            "Race Mode is one payload, cached whole. On race day it makes no " "network requests."
        ),
        body="""
Race Mode is built for a phone in a transition bag on a course with no
coverage. Everything it renders — your splits, the cut-off ladder, the
fuelling schedule, the aid-station actions, the bag manifests and the "Why
this?" reasons — arrives in a single payload and is cached.

It is fetched once and deliberately never refetched. A phone that woke up
mid-race and tried to re-download a plan would be worse than useless.

Take it further and print the race card, or export the plan to your head unit
and your calendar, from the plan's **Exports** tab. A printed card needs no
battery.
""".strip(),
    ),
]


#: Slug to article, with reading time filled in from each body.
HELP_ARTICLES: dict[str, HelpArticle] = {
    article.slug: article._replace(read_minutes=read_minutes(article.body)) for article in _ARTICLES
}

#: Categories in the order a reader should meet them, rather than
#: alphabetically — "Getting started" before "Billing" is the whole point.
CATEGORY_ORDER: tuple[str, ...] = (
    "Getting started",
    "How it works",
    "Fuelling",
    "Race week",
    "Billing",
)


def ordered_articles() -> list[HelpArticle]:
    """Every article, grouped by :data:`CATEGORY_ORDER`."""
    position = {name: index for index, name in enumerate(CATEGORY_ORDER)}
    return sorted(
        HELP_ARTICLES.values(),
        key=lambda a: (position.get(a.category, len(position)), a.title),
    )


def search_articles(query: str, limit: int = 10) -> list[HelpArticle]:
    """Articles matching *query*, title matches first.

    A plain substring scan over title, summary and body. There are a handful
    of articles, so anything cleverer would be machinery with nothing to do —
    and a title match ranking above a body match is the whole of what a reader
    notices.
    """
    needle = query.strip().lower()
    if not needle:
        return []

    scored: list[tuple[int, HelpArticle]] = []
    for article in ordered_articles():
        if needle in article.title.lower():
            scored.append((0, article))
        elif needle in article.summary.lower():
            scored.append((1, article))
        elif needle in article.body.lower():
            scored.append((2, article))
    scored.sort(key=lambda pair: pair[0])
    return [article for _, article in scored[:limit]]
