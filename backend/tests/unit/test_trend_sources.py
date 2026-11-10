"""Trend source adapters against recorded and documented responses. No network.

RSS, Atom and Google News fixtures are trimmed live responses (2026-10-12,
article bodies removed). GDELT and YouTube payloads follow each API's
documented shape: GDELT refused every request from the development network
that day, and YouTube needs a key (exercised live separately, 2026-10-13).

HTTP is faked by ``_Scripted``, which plays back responses (or raises) in
order and records every request — the same constructor-injection style as
``test_research_providers.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.research.categories import Category
from app.research.providers.base import ProviderUnavailable
from app.research.trends.contracts import SourceQuery, SourceType
from app.research.trends.feeds import Feed
from app.research.trends.metrics import volume_windows
from app.research.trends.sources.base import (
    DiscoveryPlan,
    RequestBudget,
    TransientRetry,
    fetch,
)
from app.research.trends.sources.gdelt import (
    THROTTLE_WAIT,
    GdeltSource,
    detect_gdelt_throttle,
    gdelt_any_query,
    gdelt_query,
)
from app.research.trends.sources.google_news import GoogleNewsSource, google_news_query
from app.research.trends.sources.rss import FeedState, InMemoryFeedStore, RssSource
from app.research.trends.sources.youtube import YouTubeSource, detect_youtube_quota
from app.retrieval.base import RateLimited
from app.retrieval.throttle import RateLimiter, ThrottledClient

FIXTURES = Path(__file__).parent / "fixtures" / "trends"
NOW = datetime(2026, 10, 12, 18, 30, tzinfo=UTC)
WEEK = SourceQuery(since=NOW - timedelta(days=7), until=NOW, terms=("creatine",))


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _response(
    status: int = 200,
    *,
    content: bytes = b"",
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    request = httpx.Request("GET", "https://example.test")
    if json_body is not None:
        return httpx.Response(status, json=json_body, headers=headers, request=request)
    return httpx.Response(status, content=content, headers=headers, request=request)


class _Scripted:
    """Plays back responses or exceptions in order; records each request."""

    def __init__(self, *steps: httpx.Response | BaseException) -> None:
        self._steps = list(steps)
        self.calls: list[dict[str, Any]] = []
        self.retries = 0

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        self.calls.append(
            {"url": url, "params": dict(params or {}), "headers": dict(headers or {})}
        )
        if not self._steps:
            raise AssertionError(f"unexpected request to {url}")
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


async def _no_sleep(_: float) -> None:
    return None


def _feed() -> Feed:
    return Feed("bbc_health", "https://feeds.example/bbc.xml", (Category.GENERAL_HEALTH,))


def _plan() -> DiscoveryPlan:
    return DiscoveryPlan(
        seeds=(("creatine", Category.SUPPLEMENTS), ("protein", Category.NUTRITION)),
        categories=(Category.SUPPLEMENTS, Category.NUTRITION),
        manual=("mouth taping",),
        since=NOW - timedelta(days=7),
        until=NOW,
        geo="US",
        language="en",
    )


# --- the shared wrappers --------------------------------------------------------


async def test_transient_failures_are_retried_with_backoff_and_counted() -> None:
    inner = _Scripted(_response(503), httpx.ReadTimeout("slow"), _response(200, content=b"ok"))
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    client = TransientRetry(inner, attempts=3, base_delay=1.0, sleep=sleep)
    response = await client.get("https://x.test")
    assert response.status_code == 200
    assert client.retries == 2
    assert len(waits) == 2 and waits[1] > waits[0]  # exponential


async def test_retries_are_bounded_and_the_last_failure_surfaces() -> None:
    client = TransientRetry(_Scripted(*[_response(503)] * 3), attempts=3, sleep=_no_sleep)
    assert (await client.get("https://x.test")).status_code == 503
    timeouts = TransientRetry(
        _Scripted(*[httpx.ConnectTimeout("down")] * 2), attempts=2, sleep=_no_sleep
    )
    with pytest.raises(httpx.ConnectTimeout):
        await timeouts.get("https://x.test")


async def test_a_throttle_waits_at_least_the_source_minimum() -> None:
    inner = _Scripted(RateLimited("429", retry_after=0.0), _response(200, content=b"ok"))
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    client = TransientRetry(inner, min_throttle_wait=10.0, sleep=sleep)
    await client.get("https://x.test")
    assert waits and waits[0] >= 10.0
    assert client.retries == 1


async def test_a_spent_quota_is_never_retried() -> None:
    inner = _Scripted(RateLimited("quota exhausted (Retry-After 86400s)", retry_after=86400.0))
    client = TransientRetry(inner, sleep=_no_sleep)
    with pytest.raises(RateLimited):
        await client.get("https://x.test")
    assert client.retries == 0


async def test_rejected_keys_are_permanent_and_other_errors_are_not() -> None:
    with pytest.raises(ProviderUnavailable) as forbidden:
        await fetch(_Scripted(_response(403)), "https://x.test", "src")
    assert forbidden.value.permanent
    with pytest.raises(ProviderUnavailable) as server:
        await fetch(_Scripted(_response(500)), "https://x.test", "src")
    assert not server.value.permanent


def test_the_request_budget_counts_per_source() -> None:
    budget = RequestBudget(2, {"youtube": 1})
    assert budget.take("rss") and budget.take("rss")
    assert not budget.take("rss")
    assert budget.take("youtube") and not budget.take("youtube")
    assert budget.used("rss") == 2


# --- RSS and Atom ---------------------------------------------------------------


async def test_rss_items_are_parsed_and_normalised() -> None:
    http = _Scripted(
        _response(
            200,
            content=_fixture("rss_bbc_health.xml"),
            headers={"ETag": '"abc"', "Last-Modified": "Sun, 11 Oct 2026"},
        )
    )
    store = InMemoryFeedStore()
    source = RssSource(http, (_feed(),), store, now=lambda: NOW)
    batch = await source.search(
        SourceQuery(
            since=NOW - timedelta(days=7),
            until=NOW,
            target=_feed().url,
            category=Category.GENERAL_HEALTH,
        )
    )
    assert len(batch.items) == 3
    first = batch.items[0]
    assert first.source_type is SourceType.FEED
    assert first.url and first.url.startswith("https://www.bbc.co.uk/news/articles/")
    assert first.publisher_domain == "bbc.co.uk"
    # BBC's own tracking parameters go from the canonical URL, not the original.
    assert "at_medium" in first.url
    assert first.canonical_url and "at_medium" not in first.canonical_url
    assert first.published_at == datetime(2026, 10, 11, 23, 3, 45, tzinfo=UTC)
    assert first.collected_at == NOW
    assert first.description and "<" not in first.description
    assert store.states[_feed().url].etag == '"abc"'


async def test_atom_feeds_parse_too() -> None:
    feed = Feed("conversation", "https://feeds.example/conv.atom", (Category.GENERAL_HEALTH,))
    http = _Scripted(_response(200, content=_fixture("atom_conversation_health.xml")))
    source = RssSource(http, (feed,), InMemoryFeedStore(), now=lambda: NOW)
    batch = await source.search(
        SourceQuery(since=NOW - timedelta(days=7), until=NOW, target=feed.url)
    )
    assert len(batch.items) == 2
    assert all(item.published_at is not None for item in batch.items)


async def test_a_304_replays_the_stored_items_instead_of_reading_as_empty() -> None:
    store = InMemoryFeedStore()
    feed = _feed()
    first = RssSource(
        _Scripted(
            _response(200, content=_fixture("rss_bbc_health.xml"), headers={"ETag": '"v1"'})
        ),
        (feed,),
        store,
        now=lambda: NOW - timedelta(hours=3),
    )
    query = SourceQuery(since=NOW - timedelta(days=7), until=NOW, target=feed.url)
    await first.search(query)

    http = _Scripted(_response(304))
    second = RssSource(http, (feed,), store, now=lambda: NOW)
    batch = await second.search(query)
    assert http.calls[0]["headers"]["If-None-Match"] == '"v1"'
    assert len(batch.items) == 3


async def test_a_304_with_nothing_stored_is_unavailable_not_empty() -> None:
    store = InMemoryFeedStore()
    await store.save(FeedState(feed_url=_feed().url, etag='"v1"'))
    source = RssSource(_Scripted(_response(304)), (_feed(),), store, now=lambda: NOW)
    with pytest.raises(ProviderUnavailable, match="304"):
        await source.search(
            SourceQuery(since=NOW - timedelta(days=7), until=NOW, target=_feed().url)
        )


async def test_a_feed_fetched_within_its_poll_interval_is_not_fetched_again() -> None:
    store = InMemoryFeedStore()
    feed = _feed()
    query = SourceQuery(since=NOW - timedelta(days=7), until=NOW, target=feed.url)
    await RssSource(
        _Scripted(_response(200, content=_fixture("rss_bbc_health.xml"))),
        (feed,),
        store,
        now=lambda: NOW - timedelta(minutes=5),
    ).search(query)
    untouched = _Scripted()
    batch = await RssSource(untouched, (feed,), store, now=lambda: NOW).search(query)
    assert untouched.calls == []
    assert len(batch.items) == 3


async def test_a_page_that_is_not_a_feed_is_a_failure_and_is_recorded() -> None:
    store = InMemoryFeedStore()
    source = RssSource(
        _Scripted(_response(200, content=b"<html><body>Consent</body></html>")),
        (_feed(),),
        store,
        now=lambda: NOW,
    )
    with pytest.raises(ProviderUnavailable, match="unparseable"):
        await source.search(SourceQuery(since=NOW, until=NOW, target=_feed().url))
    assert store.states[_feed().url].consecutive_failures == 1


async def test_rss_discovery_sweeps_only_feeds_in_the_requested_categories() -> None:
    feeds = (
        Feed("a", "https://a.example/rss", (Category.SUPPLEMENTS,)),
        Feed("b", "https://b.example/rss", (Category.SLEEP_RECOVERY,)),
    )
    source = RssSource(_Scripted(), feeds, InMemoryFeedStore())
    targets = [q.target for q in source.discovery(_plan())]
    assert targets == ["https://a.example/rss"]


async def test_entries_without_title_or_link_are_skipped_not_invented() -> None:
    body = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
      <item><title>Has both</title><link>https://ok.example/a</link></item>
      <item><title>No link</title></item>
      <item><link>https://ok.example/b</link></item>
      <item><title>Bad scheme</title><link>ftp://ok.example/c</link></item>
    </channel></rss>"""
    source = RssSource(
        _Scripted(_response(200, content=body)),
        (_feed(),),
        InMemoryFeedStore(),
        now=lambda: NOW,
    )
    batch = await source.search(SourceQuery(since=NOW, until=NOW, target=_feed().url))
    assert [item.title for item in batch.items] == ["Has both"]


# --- Google News RSS ------------------------------------------------------------


def test_google_news_queries_quote_phrases_and_bound_the_window() -> None:
    assert google_news_query(("vitamin d", "sleep"), 14) == '"vitamin d" sleep when:14d'


async def test_google_news_items_name_the_outlet_not_google() -> None:
    http = _Scripted(
        _response(
            200,
            content=_fixture("google_news_creatine.xml"),
            headers={"content-type": "application/xml; charset=utf-8"},
        )
    )
    batch = await GoogleNewsSource(http, now=lambda: NOW).search(WEEK)
    assert len(batch.items) == 4 and not batch.truncated
    first = batch.items[0]
    assert not first.title.endswith("Women's Health")  # the " - Publisher" suffix is gone
    assert first.publisher == "Women's Health"
    assert first.publisher_domain == "womenshealthmag.com"
    assert first.url and first.url.startswith("https://news.google.com/rss/articles/")
    params = http.calls[0]["params"]
    assert (params["hl"], params["gl"], params["ceid"]) == ("en-US", "US", "US:en")
    assert params["q"] == "creatine when:7d"


async def test_a_consent_page_is_not_an_empty_result() -> None:
    http = _Scripted(
        _response(
            200,
            content=b"<!doctype html><html>Before you continue",
            headers={"content-type": "text/html"},
        )
    )
    with pytest.raises(ProviderUnavailable, match="not a feed"):
        await GoogleNewsSource(http, now=lambda: NOW).search(WEEK)


def test_google_news_discovery_asks_each_seed_and_each_manual_query() -> None:
    queries = GoogleNewsSource(_Scripted()).discovery(_plan())
    assert [q.text for q in queries] == ["creatine", "protein", "mouth taping"]


# --- GDELT ----------------------------------------------------------------------


def test_gdelt_query_syntax() -> None:
    assert gdelt_query(("vitamin d", "sleep"), "en") == '"vitamin d" sleep sourcelang:english'
    assert gdelt_any_query(["creatine", "protein powder"], "en") == (
        '(creatine OR "protein powder") sourcelang:english'
    )


async def test_gdelt_articles_are_parsed_and_unusable_records_skipped() -> None:
    http = _Scripted(_response(200, json_body=json.loads(_fixture("gdelt_artlist.json"))))
    batch = await GdeltSource(http, now=lambda: NOW).search(WEEK)
    assert [item.title for item in batch.items] == [
        "Creatine may improve sleep after a hard night, study finds",
        "Why more women are taking creatine",
    ]
    second = batch.items[1]
    assert second.published_at == datetime(2026, 10, 10, 12, tzinfo=UTC)
    assert second.canonical_url == "https://news.example.co.uk/health/creatine-women"
    assert second.publisher_domain == "example.co.uk"
    assert second.raw_metadata["sourcecountry"] == "United Kingdom"
    params = http.calls[0]["params"]
    assert params["mode"] == "artlist" and params["format"] == "json"


async def test_gdelt_empty_object_is_unmeasured_not_zero() -> None:
    with pytest.raises(ProviderUnavailable, match="not counted as zero"):
        await GdeltSource(_Scripted(_response(200, content=b"{}"))).search(WEEK)


async def test_gdelt_plain_text_errors_are_failures_to_answer() -> None:
    http = _Scripted(_response(200, content=b"The specified phrase is too short."))
    with pytest.raises(ProviderUnavailable, match="too short"):
        await GdeltSource(http).search(WEEK)


async def test_gdelt_throttling_is_retried_after_its_minimum_wait() -> None:
    slow_down = b"Please limit requests to one every 5 seconds or contact ..."
    inner = _Scripted(
        _response(429, content=slow_down),
        _response(200, content=slow_down),
        _response(200, json_body=json.loads(_fixture("gdelt_artlist.json"))),
    )
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    throttled = ThrottledClient(
        inner,
        RateLimiter(1000),
        provider="gdelt",
        max_retries=0,
        detect_throttle=detect_gdelt_throttle,
    )
    client = TransientRetry(throttled, attempts=3, min_throttle_wait=THROTTLE_WAIT, sleep=sleep)
    batch = await GdeltSource(client, now=lambda: NOW).search(WEEK)
    assert len(batch.items) == 2
    assert client.retries == 2
    assert all(wait >= THROTTLE_WAIT for wait in waits)


async def test_gdelt_volume_fills_days_it_omitted_and_feeds_a_baseline() -> None:
    http = _Scripted(_response(200, json_body=json.loads(_fixture("gdelt_timeline.json"))))
    query = SourceQuery(
        since=datetime(2026, 9, 1, tzinfo=UTC),
        until=datetime(2026, 10, 12, 6, tzinfo=UTC),
        terms=("creatine",),
    )
    series = await GdeltSource(http).volume(query)
    assert series.dates[0].isoformat() == "2026-09-01"
    assert series.dates[-1].isoformat() == "2026-10-12"
    by_day = dict(zip(series.dates, series.values, strict=True))
    assert by_day[datetime(2026, 10, 11).date()] == 15.0
    assert by_day[datetime(2026, 10, 3).date()] == 0.0  # omitted inside an answer
    windows = volume_windows(series, window_days=7, until=query.until)
    assert windows.recent == 36.0  # Oct 5-11: 9 + 12 + 15; today (partial) left out
    assert windows.baseline_windows == 4  # Sep 7 - Oct 4, four full weeks
    assert windows.baseline == 1.75  # (4 + 3) over four windows
    assert windows.baseline is not None and windows.baseline < windows.recent


def test_gdelt_discovery_ors_each_categorys_seeds() -> None:
    queries = GdeltSource(_Scripted()).discovery(_plan())
    assert [(q.target, q.terms) for q in queries] == [
        ("any", ("creatine",)),
        ("any", ("protein",)),
        (None, ("mouth taping",)),
    ]


# --- YouTube --------------------------------------------------------------------


def _youtube_search() -> dict[str, Any]:
    return {
        "pageInfo": {"totalResults": 2},
        "items": [
            {
                "id": {"videoId": "abc123"},
                "snippet": {
                    "title": "I took creatine for 30 days",
                    "channelTitle": "Lift Lab",
                    "publishedAt": "2026-10-10T12:00:00Z",
                    "description": "What changed",
                },
            },
            {
                "id": {"videoId": "def456"},
                "snippet": {
                    "title": "Creatine and sleep?",
                    "channelTitle": "Sleep Doc",
                    "publishedAt": "2026-10-01T09:00:00Z",
                    "description": "",
                },
            },
        ],
    }


async def test_youtube_searches_then_reads_statistics_with_the_key_in_a_header() -> None:
    http = _Scripted(
        _response(200, json_body=_youtube_search()),
        _response(
            200,
            json_body={
                "items": [
                    {
                        "id": "abc123",
                        "statistics": {
                            "viewCount": "20000",
                            "likeCount": "900",
                            "commentCount": "120",
                        },
                    },
                    {"id": "def456", "statistics": {"viewCount": "500"}},
                ]
            },
        ),
    )
    source = YouTubeSource(http, "secret-key", now=lambda: NOW)
    batch = await source.search(
        SourceQuery(since=NOW - timedelta(days=14), until=NOW, terms=("creatine",))
    )
    assert [c["url"] for c in http.calls] == [
        "https://www.googleapis.com/youtube/v3/search",
        "https://www.googleapis.com/youtube/v3/videos",
    ]
    for call in http.calls:
        assert call["headers"]["X-Goog-Api-Key"] == "secret-key"
        assert "secret-key" not in json.dumps(call["params"])
    assert http.calls[1]["params"]["id"] == "abc123,def456"
    first = batch.items[0]
    assert first.url == "https://www.youtube.com/watch?v=abc123"
    assert first.engagement == {"views": 20000.0, "likes": 900.0, "comments": 120.0}
    assert first.publisher_domain == "youtube.com"


async def test_youtube_with_no_results_spends_no_statistics_call() -> None:
    http = _Scripted(_response(200, json_body={"items": []}))
    batch = await YouTubeSource(http, "k").search(WEEK)
    assert batch.items == [] and len(http.calls) == 1


async def test_a_spent_youtube_quota_is_permanent_for_the_run() -> None:
    quota = _response(403, json_body={"error": {"errors": [{"reason": "quotaExceeded"}]}})
    throttled = ThrottledClient(
        _Scripted(quota),
        RateLimiter(1000),
        provider="youtube",
        max_retries=0,
        detect_throttle=detect_youtube_quota,
    )
    source = YouTubeSource(TransientRetry(throttled, sleep=_no_sleep), "k")
    with pytest.raises(ProviderUnavailable) as raised:
        await source.search(WEEK)
    assert raised.value.permanent
    with pytest.raises(ProviderUnavailable):  # and fails fast after
        await source.search(WEEK)


async def test_a_rejected_youtube_key_is_permanent() -> None:
    bad_key = _response(400, json_body={"error": {"errors": [{"reason": "keyInvalid"}]}})
    with pytest.raises(ProviderUnavailable) as raised:
        await YouTubeSource(_Scripted(bad_key), "k").search(WEEK)
    assert raised.value.permanent


def test_api_text_is_unescaped() -> None:
    """Measured live: YouTube titles arrive HTML-escaped ("What&#39;s")."""
    from app.research.trends.documents import clean_text

    assert clean_text("Costco Supplements: What&#39;s Worth Buying? &amp; more") == (
        "Costco Supplements: What's Worth Buying? & more"
    )
