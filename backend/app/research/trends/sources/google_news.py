"""Google News RSS search: ``news.google.com/rss/search``. No key — and read the terms.

**Off by default.** The feed itself states (checked 2026-10-12): "This XML feed
is made available solely for the purpose of rendering Google News results
within a personal feed reader for personal, non-commercial use. Any other use
of the feed is expressly prohibited." The adapter is here because it was asked
for and because it is a good signal; whether a deployment's use fits those
terms is a decision for whoever enables it (``RESEARCH_ENABLED_SOURCES``), not
something this code can settle.

What it returns, and what it cannot:

* Titles end in " - Publisher"; that suffix is stripped and the publisher is
  taken from the ``<source url=...>`` element, so the publisher domain is the
  outlet's, not Google's.
* Links are Google redirect URLs (``news.google.com/rss/articles/CBMi…``).
  Since 2024 they no longer encode the target URL, and resolving them needs a
  request to Google per article, so they are **never followed**. They are
  unique per article, which is enough for exact-URL deduplication; matching
  the same article from another source falls to the title layer.
* At most ~100 items per request, so a full page leaves the earlier window's
  count unknown rather than zero.
* A consent or error page arrives as HTML with status 200: that is a failure
  to answer, never an empty result.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime

import feedparser  # type: ignore[import-untyped]

from app.research.providers.base import ProviderUnavailable
from app.research.trends.contracts import (
    Capability,
    SourceBatch,
    SourceQuery,
    SourceType,
)
from app.research.trends.documents import make_item
from app.research.trends.sources.base import DiscoveryPlan, fetch
from app.retrieval.throttle import HttpClient

GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
#: Google News answers at most this many items per request.
PAGE_LIMIT = 100


def google_news_query(terms: tuple[str, ...], days: int) -> str:
    """``("vitamin d", "sleep"), 14`` -> ``"vitamin d" sleep when:14d``."""
    parts = []
    for term in terms:
        term = term.replace('"', "").strip()
        if term:
            parts.append(f'"{term}"' if " " in term else term)
    return f"{' '.join(parts)} when:{max(1, days)}d"


class GoogleNewsSource:
    name = "google_news"
    source_type = SourceType.NEWS
    capabilities = frozenset({Capability.DISCOVER, Capability.SEARCH})

    def __init__(
        self, http: HttpClient, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.http = http
        self._now = now

    def discovery(self, plan: DiscoveryPlan) -> list[SourceQuery]:
        queries = [
            SourceQuery(
                since=plan.since,
                until=plan.until,
                geo=plan.geo,
                language=plan.language,
                terms=(seed,),
                seed=seed,
                category=category,
                max_results=PAGE_LIMIT,
            )
            for seed, category in plan.seeds
        ]
        queries += [
            SourceQuery(
                since=plan.since,
                until=plan.until,
                geo=plan.geo,
                language=plan.language,
                terms=(text,),
                seed=text,
                max_results=PAGE_LIMIT,
            )
            for text in plan.manual
        ]
        return queries

    async def search(self, query: SourceQuery) -> SourceBatch:
        days = math.ceil((query.until - query.since).total_seconds() / 86400)
        geo, language = query.geo.upper(), query.language.lower()
        params = {
            "q": google_news_query(query.terms, days),
            "hl": f"{language}-{geo}",
            "gl": geo,
            "ceid": f"{geo}:{language}",
        }
        what = f"google news search for {query.text!r}"
        response = await fetch(self.http, GOOGLE_NEWS_URL, what, params=params)
        content_type = response.headers.get("content-type", "")
        if "xml" not in content_type and not response.content.lstrip().startswith(b"<?xml"):
            raise ProviderUnavailable(
                f"{what}: answered {content_type or 'non-XML'}, not a feed"
            )
        parsed = feedparser.parse(response.content)
        if not parsed.get("version") and not parsed.get("entries"):
            raise ProviderUnavailable(f"{what}: unparseable feed")

        collected = self._now()
        entries = list(parsed.get("entries") or [])
        items = []
        for entry in entries:
            source = entry.get("source") or {}
            publisher = source.get("title") if hasattr(source, "get") else None
            item = make_item(
                source_name=self.name,
                source_type=self.source_type,
                title=entry.get("title"),
                url=entry.get("link"),
                publisher=publisher,
                publisher_url=source.get("href") if hasattr(source, "get") else None,
                published_at=entry.get("published_parsed") or entry.get("published"),
                collected_at=collected,
                raw_metadata={"google_news": True},
                query=query.text,
                seed=query.seed,
                category=query.category,
                strip_publisher=True,
            )
            if item is not None:
                items.append(item)
        return SourceBatch(
            items=items, total=len(entries), truncated=len(entries) >= PAGE_LIMIT
        )
