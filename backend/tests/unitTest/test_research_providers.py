"""Research providers against fakes: parsing, retries, fallbacks, and failures
that must surface as ``ProviderUnavailable`` rather than as empty results.

Nothing here touches Google Trends, DuckDuckGo, News API, PubMed or Europe PMC.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from app.research.contracts import NewsArticle
from app.research.providers.base import EvidenceQuery, NewsResults, ProviderUnavailable
from app.research.providers.google_trends import GoogleTrendsProvider
from app.research.providers.news import (
    DuckDuckGoNewsProvider,
    FallbackNewsProvider,
    NewsApiProvider,
    news_api_query,
)
from app.research.providers.scientific import (
    PubMedEvidenceProvider,
    europe_pmc_terms,
    pubmed_terms,
)
from app.research.providers.web_search import SimpleWebSearchProvider, ddgs_region
from app.retrieval.base import ProviderError
from app.retrieval.providers import EuropePMCProvider
from app.retrieval.pubmed import PubMedProvider


async def _no_sleep(_: float) -> None:
    return None


class _Frame:
    """The two bits of a pandas DataFrame the Trends provider reads."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self.empty = not records

    def to_dict(self, orient: str) -> list[dict[str, Any]]:
        assert orient == "records"
        return self._records


class _Throttled(Exception):
    def __init__(self) -> None:
        super().__init__("The request failed: Google returned a response with code 429")
        self.response = type("R", (), {"status_code": 429})()


class _FakeTrends:
    def __init__(self, *, throttle_first: int = 0) -> None:
        self.payloads: list[dict[str, Any]] = []
        self._throttle_first = throttle_first

    def build_payload(self, kw_list: list[str], **kwargs: Any) -> None:
        if self._throttle_first > 0:
            self._throttle_first -= 1
            raise _Throttled()
        self.payloads.append({"kw_list": kw_list, **kwargs})

    def related_queries(self) -> dict[str, Any]:
        seed = self.payloads[-1]["kw_list"][0]
        rising = _Frame(
            [
                {"query": f"{seed} before bed", "value": 900},
                {"query": f"{seed} for women", "value": 5100},
                {"query": "", "value": 10},
            ]
        )
        return {seed: {"rising": rising, "top": None}}

    def interest_over_time(self) -> _Frame:
        kws = self.payloads[-1]["kw_list"]
        rows = [{**{kw: day for kw in kws}, "isPartial": False} for day in range(14)]
        rows.append({**{kw: 0 for kw in kws}, "isPartial": True})
        return _Frame(rows)


# --- Google Trends ---------------------------------------------------------


async def test_related_queries_are_parsed_and_blank_rows_dropped() -> None:
    fake = _FakeTrends()
    trends = GoogleTrendsProvider(min_interval=0, client_factory=lambda: fake, sleep=_no_sleep)

    related = await trends.related_queries("creatine", window_days=7, geo="US")

    assert [(r.query, r.value) for r in related.rising] == [
        ("creatine before bed", 900),
        ("creatine for women", 5100),
    ]
    assert fake.payloads[0]["timeframe"] == "now 7-d"
    assert fake.payloads[0]["geo"] == "US"


async def test_a_429_is_retried_after_a_wait_then_succeeds() -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    fake = _FakeTrends(throttle_first=1)
    trends = GoogleTrendsProvider(
        min_interval=0, client_factory=lambda: fake, sleep=sleep, backoff=(15.0, 45.0)
    )

    related = await trends.related_queries("creatine", window_days=7, geo="US")

    assert related.rising
    assert 15.0 in waits


async def test_persistent_throttling_is_unavailable_not_empty() -> None:
    fake = _FakeTrends(throttle_first=10)
    trends = GoogleTrendsProvider(
        min_interval=0, client_factory=lambda: fake, sleep=_no_sleep, backoff=(1.0, 1.0)
    )

    with pytest.raises(ProviderUnavailable, match="429"):
        await trends.related_queries("creatine", window_days=7, geo="US")


async def test_interest_series_drops_the_partial_last_day() -> None:
    fake = _FakeTrends()
    trends = GoogleTrendsProvider(min_interval=0, client_factory=lambda: fake, sleep=_no_sleep)

    series = await trends.interest_over_time(["a", "vitamin d"], days=14, geo="US")

    assert series["a"] == [float(day) for day in range(14)]
    assert fake.payloads[0]["timeframe"] == "today 1-m"


async def test_more_than_five_queries_is_a_caller_error() -> None:
    trends = GoogleTrendsProvider(min_interval=0, client_factory=_FakeTrends, sleep=_no_sleep)
    with pytest.raises(ValueError):
        await trends.interest_over_time(list("abcdef"), days=14, geo="US")


# --- web search ------------------------------------------------------------


class _FakeDdgs:
    def __init__(self, *, fail: int = 0) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail

    def text(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
        self.calls.append((query, kwargs))
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("202 Ratelimit")
        return [
            {"title": "Creatine and sleep", "href": "https://www.nih.gov/x", "body": "..."},
            {"title": "", "href": "https://example.com/blank"},
        ]

    def news(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
        self.calls.append((query, kwargs))
        return [
            {"title": "New", "url": "https://n/1", "date": "2026-09-28T08:00:00+00:00"},
            {"title": "Older", "url": "https://n/2", "date": "2026-09-18T08:00:00+00:00"},
            {"title": "Too old", "url": "https://n/3", "date": "2026-08-01T08:00:00+00:00"},
        ]


async def test_web_search_parses_results_and_skips_untitled_ones() -> None:
    fake = _FakeDdgs()
    web = SimpleWebSearchProvider(client_factory=lambda: fake, sleep=_no_sleep)

    results = await web.search("creatine before bed", region="us-en", max_results=20)

    assert [r.url for r in results] == ["https://www.nih.gov/x"]
    assert fake.calls[0][1] == {"region": "us-en", "max_results": 20}


async def test_web_search_retries_once_then_reports_unavailable() -> None:
    recovered = _FakeDdgs(fail=1)
    web = SimpleWebSearchProvider(client_factory=lambda: recovered, sleep=_no_sleep, retries=1)
    assert await web.search("q", region="us-en", max_results=5)

    broken = _FakeDdgs(fail=5)
    web = SimpleWebSearchProvider(client_factory=lambda: broken, sleep=_no_sleep, retries=1)
    with pytest.raises(ProviderUnavailable, match="Ratelimit"):
        await web.search("q", region="us-en", max_results=5)
    assert len(broken.calls) == 2


def test_ddgs_region_codes() -> None:
    assert ddgs_region("US", "en") == "us-en"
    assert ddgs_region("GB", "en") == "uk-en"


# --- news ------------------------------------------------------------------


class _FakeHttp:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self.response


def test_news_api_query_phrases_multiword_terms() -> None:
    assert news_api_query(["vitamin d", "sleep"]) == '"vitamin d" AND sleep'


async def test_news_api_parses_articles_and_total() -> None:
    http = _FakeHttp(
        httpx.Response(
            200,
            json={
                "status": "ok",
                "totalResults": 250,
                "articles": [
                    {
                        "title": "Creatine at night",
                        "url": "https://news/1",
                        "source": {"name": "Health Daily"},
                        "publishedAt": "2026-09-28T10:00:00Z",
                    },
                    {"title": None, "url": "https://news/2"},
                ],
            },
        )
    )
    news = NewsApiProvider(http, "k" * 32)

    result = await news.search(
        ["creatine", "sleep"], since=date(2026, 9, 16), until=date(2026, 9, 30), language="en"
    )

    assert result.total == 250
    assert [a.title for a in result.articles] == ["Creatine at night"]
    assert http.calls[0]["headers"] == {"X-Api-Key": "k" * 32}
    assert http.calls[0]["params"]["q"] == "creatine AND sleep"


async def test_news_api_quota_errors_are_unavailable() -> None:
    http = _FakeHttp(
        httpx.Response(429, json={"status": "error", "code": "rateLimited", "message": "100"})
    )
    with pytest.raises(ProviderUnavailable, match="rateLimited"):
        await NewsApiProvider(http, "k").search(
            ["x"], since=date(2026, 9, 1), until=date(2026, 9, 30), language="en"
        )


async def test_duckduckgo_news_keeps_only_the_requested_dates() -> None:
    fake = _FakeDdgs()
    news = DuckDuckGoNewsProvider(region="us-en", client_factory=lambda: fake, sleep=_no_sleep)

    result = await news.search(
        ["creatine"], since=date(2026, 9, 16), until=date(2026, 9, 30), language="en"
    )

    assert [a.title for a in result.articles] == ["New", "Older"]
    assert result.total == 2


class _StubNews:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls = 0

    async def search(
        self, terms: list[str], *, since: date, until: date, language: str
    ) -> NewsResults:
        self.calls += 1
        if self.fail:
            raise ProviderUnavailable(f"{self.name} is out of quota")
        return NewsResults(
            source=self.name, total=1, articles=[NewsArticle(title="t", url="https://u")]
        )


async def test_news_falls_back_and_stops_asking_a_dead_primary() -> None:
    primary, fallback = _StubNews("news_api", fail=True), _StubNews("duckduckgo_news")
    news = FallbackNewsProvider(primary, fallback)
    kwargs = {"since": date(2026, 9, 16), "until": date(2026, 9, 30), "language": "en"}

    first = await news.search(["a"], **kwargs)
    second = await news.search(["b"], **kwargs)

    assert first.source == second.source == "duckduckgo_news"
    assert primary.calls == 1
    assert fallback.calls == 2


# --- scientific evidence ---------------------------------------------------


def test_pubmed_terms_phrase_the_subject_and_and_the_outcome() -> None:
    query = EvidenceQuery(
        subject="Vitamin D", outcome="sleep quality", recent_since=date(2025, 9, 30)
    )
    assert pubmed_terms(query) == '"vitamin d"[tiab] AND (sleep[tiab] AND quality[tiab])'
    assert europe_pmc_terms(query) == '"vitamin d" AND (sleep AND quality)'
    bare = EvidenceQuery(subject="creatine", outcome="", recent_since=date(2025, 9, 30))
    assert pubmed_terms(bare) == '"creatine"[tiab]'


class _FakePubMed:
    def __init__(self, counts: dict[str, int] | None = None, *, fail: bool = False) -> None:
        self.asked: list[tuple[str, str | None]] = []
        self._counts = counts or {}
        self._fail = fail

    async def count(self, terms: str, *, min_date: str | None = None) -> int:
        self.asked.append((terms, min_date))
        if self._fail:
            raise ProviderError("pubmed esearch returned 500")
        if min_date:
            return self._counts.get("recent", 0)
        if "systematic[sb]" in terms:
            return self._counts.get("reviews", 0)
        if "randomized controlled trial" in terms:
            return self._counts.get("rcts", 0)
        return self._counts.get("total", 0)


async def test_pubmed_profile_makes_the_four_counts() -> None:
    fake = _FakePubMed({"total": 40, "recent": 6, "reviews": 3, "rcts": 5})
    provider = PubMedEvidenceProvider(fake)  # type: ignore[arg-type]

    counts = await provider.profile(
        EvidenceQuery(subject="creatine", outcome="sleep", recent_since=date(2025, 9, 30))
    )

    assert (counts.total, counts.recent, counts.reviews_or_meta, counts.rcts) == (40, 6, 3, 5)
    assert fake.asked[1][1] == "2025/09/30"


async def test_zero_papers_skips_the_follow_up_counts() -> None:
    fake = _FakePubMed({"total": 0})
    counts = await PubMedEvidenceProvider(fake).profile(  # type: ignore[arg-type]
        EvidenceQuery(subject="miracle powder", outcome="", recent_since=date(2025, 9, 30))
    )
    assert counts.total == 0
    assert len(fake.asked) == 1


async def test_a_failed_count_is_unavailable_never_zero() -> None:
    provider = PubMedEvidenceProvider(_FakePubMed(fail=True))  # type: ignore[arg-type]
    with pytest.raises(ProviderUnavailable):
        await provider.profile(
            EvidenceQuery(subject="creatine", outcome="", recent_since=date(2025, 9, 30))
        )


async def test_pubmed_count_reads_esearch_count() -> None:
    http = _FakeHttp(
        httpx.Response(200, json={"esearchresult": {"count": "123", "idlist": []}})
    )
    assert await PubMedProvider(http).count('"creatine"[tiab]', min_date="2025/09/30") == 123
    assert http.calls[0]["params"]["retmax"] == "0"
    assert http.calls[0]["params"]["mindate"] == "2025/09/30"

    missing = _FakeHttp(httpx.Response(200, json={"esearchresult": {}}))
    with pytest.raises(ProviderError):
        await PubMedProvider(missing).count("x")


async def test_europe_pmc_count_reads_hit_count() -> None:
    http = _FakeHttp(httpx.Response(200, json={"hitCount": 57, "resultList": {"result": []}}))
    assert await EuropePMCProvider(http).count('"creatine" AND (sleep)') == 57

    broken = _FakeHttp(httpx.Response(503, text="unavailable"))
    with pytest.raises(ProviderError):
        await EuropePMCProvider(broken).count("x")


async def test_ddgs_no_results_is_an_empty_answer_not_a_failure() -> None:
    """ddgs raises "No results found." only when every engine answered empty."""
    from app.research.providers.web_search import SimpleWebSearchProvider

    class DDGSException(Exception):  # same name ddgs uses; matched by name
        pass

    class _Empty:
        def text(self, *args: object, **kwargs: object) -> list[dict[str, str]]:
            raise DDGSException("No results found.")

    class _Broken:
        def text(self, *args: object, **kwargs: object) -> list[dict[str, str]]:
            raise DDGSException("https://html.duckduckgo.com 403 Ratelimit")

    async def no_sleep(_: float) -> None:
        return None

    empty = SimpleWebSearchProvider(client_factory=_Empty, sleep=no_sleep)
    assert await empty.search("magnesium spray sleep", region="us-en", max_results=5) == []
    broken = SimpleWebSearchProvider(client_factory=_Broken, retries=0, sleep=no_sleep)
    with pytest.raises(ProviderUnavailable):
        await broken.search("magnesium spray sleep", region="us-en", max_results=5)
