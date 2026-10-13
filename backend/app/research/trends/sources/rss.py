"""Publisher RSS and Atom feeds, parsed by feedparser. No key; the feeds are public.

Feeds are the cheapest discovery source and the politest: one request per feed
per run, conditional on what the last fetch saw. ``research_feed_state`` keeps
each feed's ``ETag`` / ``Last-Modified`` validators, when it was last fetched
and last succeeded, its last error, and **the last parsed items**:

* A **304 Not Modified replays the stored items.** Without them a 304 would
  read as "this feed has nothing", which is a zero standing in for "unchanged"
  — exactly the substitution the trend agent refuses everywhere else.
* A feed fetched within its ``poll_minutes`` is not fetched again; its stored
  items are replayed without a request.

What is stored is a slim copy (title, link, date, a 500-character summary),
never an article body: feeds often carry only a teaser, and the agent needs a
headline and a date, not the publisher's text.

Never-empty rule: feedparser flags malformed XML as ``bozo`` and still recovers
what it can. A bozo feed *with* entries is used (most "malformed" feeds are a
stray character); a bozo feed with none is a failure, not an empty feed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import feedparser  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from app.research.providers.base import ProviderUnavailable
from app.research.trends.contracts import (
    Capability,
    SourceBatch,
    SourceItem,
    SourceQuery,
    SourceType,
)
from app.research.trends.documents import make_item, parse_timestamp
from app.research.trends.feeds import Feed, feeds_for
from app.research.trends.sources.base import DiscoveryPlan, fetch
from app.retrieval.throttle import HttpClient

logger = logging.getLogger(__name__)

#: Entries kept per feed for a 304 replay. Feeds publish 10-60.
STORED_ITEMS = 100
_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"


class FeedState(BaseModel):
    """One row of ``research_feed_state``."""

    feed_url: str
    etag: str | None = None
    last_modified: str | None = None
    last_fetched_at: datetime | None = None
    last_success_at: datetime | None = None
    last_status: int | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    items: list[dict[str, Any]] = Field(default_factory=list)


class FeedStateStore(Protocol):
    async def load(self, urls: list[str]) -> dict[str, FeedState]: ...

    async def save(self, state: FeedState) -> None: ...


class InMemoryFeedStore:
    """For tests and the dry-run script: state lives as long as the object."""

    def __init__(self) -> None:
        self.states: dict[str, FeedState] = {}

    async def load(self, urls: list[str]) -> dict[str, FeedState]:
        return {url: self.states[url] for url in urls if url in self.states}

    async def save(self, state: FeedState) -> None:
        self.states[state.feed_url] = state


class RssSource:
    name = "rss"
    source_type = SourceType.FEED
    capabilities = frozenset({Capability.DISCOVER})

    def __init__(
        self,
        http: HttpClient,
        feeds: tuple[Feed, ...],
        store: FeedStateStore,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._http = http
        self._feeds = {feed.url: feed for feed in feeds}
        self._store = store
        self._now = now
        self._states: dict[str, FeedState] | None = None
        self._lock = asyncio.Lock()
        self.http = http

    def discovery(self, plan: DiscoveryPlan) -> list[SourceQuery]:
        return [
            SourceQuery(
                since=plan.since,
                until=plan.until,
                geo=plan.geo,
                language=plan.language,
                target=feed.url,
                seed=feed.name,
                category=feed.categories[0],
            )
            for feed in feeds_for(tuple(self._feeds.values()), list(plan.categories))
        ]

    async def search(self, query: SourceQuery) -> SourceBatch:
        if query.target is None or query.target not in self._feeds:
            raise ProviderUnavailable(f"rss: unknown feed {query.target!r}", permanent=True)
        feed = self._feeds[query.target]
        state = await self._state(feed.url)
        now = self._now()

        if (
            state.last_success_at is not None
            and state.items
            and now - state.last_success_at < timedelta(minutes=feed.poll_minutes)
        ):
            return SourceBatch(items=self._replay(state, feed, query), total=len(state.items))

        headers = {"Accept": _ACCEPT}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        state.last_fetched_at = now
        try:
            response = await fetch(self._http, feed.url, f"rss {feed.name}", headers=headers)
        except ProviderUnavailable as exc:
            await self._record_failure(state, str(exc))
            raise

        state.last_status = response.status_code
        if response.status_code == 304:
            if not state.items:
                # Unchanged, and we kept nothing from last time: unknown, not empty.
                await self._record_failure(state, "304 with no stored items to replay")
                raise ProviderUnavailable(f"rss {feed.name}: 304 but nothing stored to replay")
            state.last_success_at = now
            state.consecutive_failures = 0
            state.last_error = None
            await self._store.save(state)
            return SourceBatch(items=self._replay(state, feed, query), total=len(state.items))

        parsed = feedparser.parse(response.content)
        entries = list(parsed.get("entries") or [])
        # feedparser names the format it recognised in ``version`` ("rss20",
        # "atom10", ...) and leaves it empty for anything else — an HTML
        # consent page parses without a bozo flag, so that is the reliable test.
        if not entries and (parsed.get("bozo") or not parsed.get("version")):
            reason = parsed.get("bozo_exception") or "not a feed"
            await self._record_failure(state, f"unparseable: {reason}")
            raise ProviderUnavailable(f"rss {feed.name}: unparseable feed ({reason})")

        stored = [_slim(entry) for entry in entries[:STORED_ITEMS]]
        state.items = [entry for entry in stored if entry is not None]
        state.etag = response.headers.get("etag") or None
        state.last_modified = response.headers.get("last-modified") or None
        state.last_success_at = now
        state.last_error = None
        state.consecutive_failures = 0
        await self._store.save(state)
        items = self._replay(state, feed, query)
        return SourceBatch(items=items, total=len(entries))

    async def _state(self, url: str) -> FeedState:
        async with self._lock:
            if self._states is None:
                self._states = await self._store.load(list(self._feeds))
            return self._states.setdefault(url, FeedState(feed_url=url))

    async def _record_failure(self, state: FeedState, error: str) -> None:
        state.last_error = error[:500]
        state.consecutive_failures += 1
        try:
            await self._store.save(state)
        except Exception:  # bookkeeping must not mask the real failure
            logger.exception("could not record the failure of feed %s", state.feed_url)

    def _replay(self, state: FeedState, feed: Feed, query: SourceQuery) -> list[SourceItem]:
        collected = self._now()
        items: list[SourceItem] = []
        for entry in state.items:
            item = make_item(
                source_name=self.name,
                source_type=self.source_type,
                title=entry.get("title"),
                url=entry.get("link"),
                publisher=entry.get("publisher") or feed.name,
                published_at=entry.get("published"),
                collected_at=collected,
                description=entry.get("summary"),
                author=entry.get("author"),
                raw_metadata={"feed": feed.name},
                query=None,
                seed=feed.name,
                category=query.category,
            )
            if item is not None:
                items.append(item)
        return items


def _slim(entry: Any) -> dict[str, Any] | None:
    """What a 304 replay needs from one entry, JSON-safe."""
    title = entry.get("title")
    link = entry.get("link")
    if not title or not link:
        return None
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    parsed = parse_timestamp(published) if published is not None else None
    when = parsed.isoformat() if parsed is not None else None
    source = entry.get("source") or {}
    return {
        "title": str(title)[:400],
        "link": str(link),
        "published": when,
        "summary": str(entry.get("summary") or "")[:600] or None,
        "author": entry.get("author"),
        "publisher": source.get("title") if isinstance(source, dict) else None,
    }
