"""Builds the research agent's sources and providers from settings.

The only place that knows which implementation stands behind each source —
swapping DuckDuckGo for Brave, or adding a source, is a change here and
nowhere else. A source that cannot be built (a key it needs is missing) is
listed as ``unavailable`` with the reason and never fails the build; a source
not enabled for the run is not built at all.

**Request rates are constants, not settings**: they are facts about each API
(GDELT asks for one request per five seconds, YouTube counts quota units),
and they are per process while every ceiling is per IP — so they sit below
the published numbers, as ``retrieval/factory.py``'s do.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

import httpx

from app.config import Settings
from app.llm.base import LLMError
from app.llm.factory import build_generative_clients, llamacpp_chat
from app.llm.ollama_client import build_ollama_client
from app.research.contracts import PastTopic, ResearchConfig
from app.research.evidence import EvidenceDeps
from app.research.providers.base import NewsProvider, WebSearchProvider
from app.research.providers.brave import BraveSearchProvider
from app.research.providers.google_trends import GoogleTrendsProvider
from app.research.providers.news import (
    DuckDuckGoNewsProvider,
    FallbackNewsProvider,
    NewsApiProvider,
)
from app.research.providers.scientific import (
    EuropePMCEvidenceProvider,
    PubMedEvidenceProvider,
)
from app.research.providers.web_search import SimpleWebSearchProvider, ddgs_region
from app.research.trends.feeds import load_sources_file
from app.research.trends.graph import TrendDeps
from app.research.trends.history import TopicStore
from app.research.trends.sources.adapters import (
    GoogleTrendsSource,
    NewsSearchSource,
    WebSearchSource,
)
from app.research.trends.sources.base import TransientRetry, TrendSource
from app.research.trends.sources.gdelt import (
    THROTTLE_WAIT,
    GdeltSource,
    detect_gdelt_throttle,
)
from app.research.trends.sources.google_news import GoogleNewsSource
from app.research.trends.sources.rss import FeedStateStore, RssSource
from app.research.trends.sources.youtube import YouTubeSource, detect_youtube_quota
from app.research.triage import LlamaCppTriageClient, OllamaTriageClient
from app.retrieval.factory import (
    build_europe_pmc_provider,
    build_pubmed_provider,
    throttled_client,
)
from app.retrieval.throttle import RateLimiter, ThrottledClient, ThrottleDetector

logger = logging.getLogger(__name__)

#: News API's free plan allows 100 requests a day; one a second is plenty.
NEWS_API_RPS = 1.0
#: Publisher feeds: one request each per run, from many hosts.
RSS_RPS = 2.0
#: Google News RSS has no published limit; it is a feed reader's endpoint.
GOOGLE_NEWS_RPS = 0.5
#: GDELT asks for at most one request every five seconds per IP.
GDELT_RPS = 1 / 6
#: Brave's free plan: 1 request a second.
BRAVE_RPS = 1.0
#: YouTube is bounded by daily quota units, not rate; one a second is polite.
YOUTUBE_RPS = 1.0


def source_client(
    settings: Settings,
    http: httpx.AsyncClient,
    name: str,
    per_second: float,
    *,
    detect: ThrottleDetector | None = None,
    min_throttle_wait: float = 0.0,
    attempts: int | None = None,
) -> TransientRetry:
    """A trend source's HTTP client: paced, then retried on transient failures.

    ``ThrottledClient`` runs with ``max_retries=0`` so a 429 surfaces at once
    and ``TransientRetry`` retries it on this source's own minimum wait, and
    counts the retry for the run report.
    """
    throttled = ThrottledClient(
        http,
        RateLimiter(per_second),
        provider=name,
        max_retries=0,
        max_wait_seconds=settings.provider_max_retry_wait_seconds,
        detect_throttle=detect,
    )
    return TransientRetry(
        throttled,
        attempts=attempts or settings.provider_max_retries + 1,
        max_delay=max(settings.provider_max_retry_wait_seconds, min_throttle_wait * 2),
        min_throttle_wait=min_throttle_wait,
    )


def build_news_provider(
    settings: Settings, http: httpx.AsyncClient, region: str
) -> NewsProvider:
    """News API with DuckDuckGo news behind it, or DuckDuckGo news alone."""
    ddg = DuckDuckGoNewsProvider(region=region)
    if not settings.news_api_enabled or not settings.news_api_key:
        return ddg
    news_api = NewsApiProvider(
        throttled_client(settings, http, "news_api", NEWS_API_RPS), settings.news_api_key
    )
    return FallbackNewsProvider(news_api, ddg)


def build_web_provider(settings: Settings, http: httpx.AsyncClient) -> WebSearchProvider:
    """Brave's documented API when keyed, keyless DuckDuckGo (ddgs) otherwise."""
    if settings.brave_search_api_key:
        return BraveSearchProvider(
            source_client(settings, http, "brave", BRAVE_RPS), settings.brave_search_api_key
        )
    return SimpleWebSearchProvider()


def build_trend_sources(
    settings: Settings,
    http: httpx.AsyncClient,
    config: ResearchConfig,
    *,
    feeds: FeedStateStore,
) -> tuple[list[TrendSource], dict[str, str]]:
    """The run's enabled sources, in collection order, and why any could not be built."""
    enabled = set(config.enabled_sources)
    feed_list = load_sources_file(settings.research_sources_file)
    region = ddgs_region(config.geo, config.language)
    sources: list[TrendSource] = []
    unavailable: dict[str, str] = {}

    if "google_trends" in enabled:
        sources.append(
            GoogleTrendsSource(
                GoogleTrendsProvider(
                    min_interval=settings.research_trends_request_interval_seconds
                ),
                window_days=config.trend_window_days,
            )
        )
    if "rss" in enabled:
        sources.append(
            RssSource(source_client(settings, http, "rss", RSS_RPS), feed_list, feeds)
        )
    if "google_news" in enabled:
        sources.append(
            GoogleNewsSource(source_client(settings, http, "google_news", GOOGLE_NEWS_RPS))
        )
    if "gdelt" in enabled:
        sources.append(
            GdeltSource(
                source_client(
                    settings,
                    http,
                    "gdelt",
                    GDELT_RPS,
                    detect=detect_gdelt_throttle,
                    min_throttle_wait=THROTTLE_WAIT,
                    # One retry: measured 2026-10-12, GDELT refused most requests
                    # for minutes at a time, and three attempts at >=10 s each
                    # spent the whole enrichment window on it.
                    attempts=2,
                )
            )
        )
    if "news" in enabled:
        sources.append(NewsSearchSource(build_news_provider(settings, http, region)))
    if "web" in enabled:
        sources.append(WebSearchSource(build_web_provider(settings, http)))
    if "youtube" in enabled:
        if settings.youtube_api_key:
            sources.append(
                YouTubeSource(
                    source_client(
                        settings, http, "youtube", YOUTUBE_RPS, detect=detect_youtube_quota
                    ),
                    settings.youtube_api_key,
                )
            )
        else:
            unavailable["youtube"] = "no YOUTUBE_API_KEY configured"
    return sources, unavailable


def build_research_deps(
    settings: Settings,
    http: httpx.AsyncClient,
    config: ResearchConfig,
    *,
    history: Callable[[datetime], Awaitable[list[PastTopic]]],
    topics: TopicStore | None,
    feeds: FeedStateStore,
) -> tuple[TrendDeps, EvidenceDeps]:
    sources, unavailable = build_trend_sources(settings, http, config, feeds=feeds)

    # Triage runs on the local synthesis model (qwen2.5 by default, or Bonsai on
    # llama-server). On the hosted path it is skipped and the raw phrases are
    # used — one small call is not worth a second hosted integration.
    triage: OllamaTriageClient | None = None
    provider = settings.llm_provider.lower()
    if provider == "ollama":
        triage = OllamaTriageClient(
            build_ollama_client(settings.ollama_base_url, settings.ollama_timeout_seconds),
            settings.ollama_synthesis_model,
        )
    elif provider == "llamacpp":
        triage = LlamaCppTriageClient(llamacpp_chat(settings))

    embedder = None
    try:
        from app.llm.embeddings.factory import build_embedding_provider

        embedder = build_embedding_provider(settings)
    except Exception as exc:
        logger.warning(
            "research: no embedding provider; clustering and novelty skipped: %s", exc
        )

    trend = TrendDeps(
        sources=sources,
        unavailable=unavailable,
        triage=triage,
        embedder=embedder,
        history=history,
        topics=topics,
    )

    pubmed = build_pubmed_provider(settings, http)
    pubmed_evidence = PubMedEvidenceProvider(pubmed)
    extraction = None
    try:
        extraction, _ = build_generative_clients(settings)
    except LLMError as exc:
        logger.warning("research: no extraction client, deep research is skipped: %s", exc)
    evidence = EvidenceDeps(
        science=pubmed_evidence,
        cross_check=EuropePMCEvidenceProvider(build_europe_pmc_provider(settings, http)),
        deep_counts=pubmed_evidence,
        papers=pubmed,
        extraction=extraction,
    )
    return trend, evidence
