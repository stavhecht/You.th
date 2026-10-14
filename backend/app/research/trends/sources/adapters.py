"""``TrendSource`` faces for the research agent's existing providers.

Google Trends, News API / DuckDuckGo news and web search were built against
the per-signal protocols in ``research/providers/base.py``, and those classes
— their pacing, retries and fallbacks, and the measurements behind them — are
kept exactly as they are. These adapters only translate their answers into
``SourceItem``s, so the trend graph can treat every source alike.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from app.research.providers.base import (
    NewsProvider,
    ProviderUnavailable,
    TrendProvider,
    WebSearchProvider,
)
from app.research.providers.google_trends import BREAKOUT_PERCENT
from app.research.providers.web_search import ddgs_region
from app.research.trends.contracts import (
    Capability,
    SourceBatch,
    SourceItem,
    SourceQuery,
    SourceType,
)
from app.research.trends.documents import make_item
from app.research.trends.sources.base import DiscoveryPlan


class GoogleTrendsSource:
    """Rising related queries (discovery) and the anchored interest series.

    Trends values are **relative indices** (0-100 within one request), never
    search counts; the items carry Google's "rising" percent as a metric and
    nothing is converted into a volume.
    """

    name = "google_trends"
    source_type = SourceType.SEARCH
    capabilities = frozenset({Capability.DISCOVER, Capability.INTEREST})

    def __init__(
        self,
        provider: TrendProvider,
        *,
        window_days: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.provider = provider
        self._window_days = window_days
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
            )
            for text in plan.manual
        ]
        return queries

    async def search(self, query: SourceQuery) -> SourceBatch:
        related = await self.provider.related_queries(
            query.text, window_days=self._window_days, geo=query.geo
        )
        collected = self._now()
        items: list[SourceItem] = []
        for row in related.rising:
            item = make_item(
                source_name=self.name,
                source_type=self.source_type,
                title=row.query,
                collected_at=collected,
                metrics={
                    "rising_percent": float(row.value),
                    "breakout": 1.0 if row.value >= BREAKOUT_PERCENT else 0.0,
                },
                query=query.text,
                seed=query.seed,
                category=query.category,
            )
            if item is not None:
                items.append(item)
        return SourceBatch(items=items, total=len(items))

    async def interest(
        self, queries: list[str], *, days: int, geo: str
    ) -> dict[str, list[float]]:
        return await self.provider.interest_over_time(queries, days=days, geo=geo)


class NewsSearchSource:
    """News API with DuckDuckGo news behind it (``FallbackNewsProvider``)."""

    source_type = SourceType.NEWS
    capabilities = frozenset({Capability.SEARCH})

    def __init__(
        self, provider: NewsProvider, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.provider = provider
        self.name = "news"
        self._now = now

    async def search(self, query: SourceQuery) -> SourceBatch:
        results = await self.provider.search(
            list(query.terms),
            since=query.since.date(),
            until=query.until.date(),
            language=query.language,
        )
        collected = self._now()
        items = []
        for article in results.articles:
            item = make_item(
                source_name=results.source,
                source_type=self.source_type,
                title=article.title,
                url=article.url,
                publisher=article.source,
                published_at=article.published_at,
                collected_at=collected,
                query=query.text,
                seed=query.seed,
                category=query.category,
            )
            if item is not None:
                items.append(item)
        return SourceBatch(
            items=items, total=results.total, truncated=results.total > len(results.articles)
        )


class WebSearchSource:
    """A web search provider (Brave when keyed, DuckDuckGo otherwise).

    Web pages are evergreen and mostly undated: they show a topic is written
    about, and count toward source *diversity*, never toward independent
    reporting this week.
    """

    source_type = SourceType.WEB
    capabilities = frozenset({Capability.SEARCH})

    def __init__(
        self,
        provider: WebSearchProvider,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.provider = provider
        self.name = "web"
        self._now = now

    async def search(self, query: SourceQuery) -> SourceBatch:
        try:
            results = await self.provider.search(
                query.text,
                region=ddgs_region(query.geo, query.language),
                max_results=query.max_results,
            )
        except ProviderUnavailable:
            raise
        collected = self._now()
        items = []
        for result in results:
            item = make_item(
                source_name=self.provider.name,
                source_type=self.source_type,
                title=result.title,
                url=result.url,
                description=result.snippet,
                collected_at=collected,
                query=query.text,
                seed=query.seed,
                category=query.category,
            )
            if item is not None:
                items.append(item)
        return SourceBatch(items=items, total=len(items))
