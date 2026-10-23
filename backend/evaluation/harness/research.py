"""Failure scenarios for the research agent — the part with web, news and trend tools.

The research agent is where the tool fallbacks live: News API falls back to
DuckDuckGo news, PubMed and Europe PMC stand in for each other, a triage-model
failure falls back to the raw queries. Each scenario breaks one thing and runs
the **production graph** (``research/graph.py::ResearchAgent``) end to end.

What is real and what is scripted:

* Real: the graph, the scorer, ``FallbackNewsProvider``, ``NewsApiProvider``,
  ``DuckDuckGoNewsProvider`` and ``SimpleWebSearchProvider`` (with a scripted
  ``ddgs`` client), ``PubMedEvidenceProvider`` and ``EuropePMCEvidenceProvider``
  over the real ``PubMedProvider`` / ``EuropePMCProvider`` and the real
  ``ThrottledClient``, and ``TemplateQueryStrategy``.
* Scripted: Google Trends (a scraper with no HTTP seam to script), the triage
  and extraction models, the embedder, and the HTTP answers themselves —
  canned counts from ``ScriptedHttp``, with faults injected through the same
  ``harness/faults.py`` the pipeline cases use.

So the scenario runs no model and touches no network, and what it measures is
whether the agent detects a failure, uses its fallback, never scores a missing
signal as zero, and ends the run cleanly.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

from app.config import get_settings
from app.domain.contracts import ExtractionInput, ExtractionOutput
from app.domain.enums import ResearchCandidateStatus, ResearchRunMode
from app.llm.base import LLMError, LLMResult, TokenUsage
from app.research.contracts import (
    Category,
    PastTopic,
    ResearchConfig,
    ResearchParams,
    ResearchState,
)
from app.research.evidence import EvidenceDeps
from app.research.graph import ResearchAgent
from app.research.normalize import QueryCluster
from app.research.providers.base import ProviderUnavailable, RelatedQueries, RelatedQuery
from app.research.providers.news import (
    DuckDuckGoNewsProvider,
    FallbackNewsProvider,
    NewsApiProvider,
)
from app.research.providers.scientific import (
    EuropePMCEvidenceProvider,
    PubMedEvidenceProvider,
)
from app.research.providers.web_search import SimpleWebSearchProvider
from app.research.trends.graph import TrendDeps
from app.research.trends.sources.adapters import (
    GoogleTrendsSource,
    NewsSearchSource,
    WebSearchSource,
)
from app.research.triage import Examples, TriageOutput, TriageTopic
from app.retrieval.providers import EuropePMCProvider
from app.retrieval.pubmed import PubMedProvider
from app.retrieval.throttle import RateLimiter, ThrottledClient
from evaluation.harness.faults import FaultInjector, fault_response
from evaluation.harness.http import classify, sanitize
from evaluation.harness.trace import Trace, TraceRecorder
from evaluation.schema import FaultSpec, Outcome

TODAY = date(2026, 9, 30)
NOW = datetime(2026, 9, 30, 8, tzinfo=UTC)

#: (query, subject, outcome, literature size) — the scripted internet.
TOPICS = [
    ("creatine before bed", "creatine", "sleep", 60),
    ("creatine for women", "creatine", "women", 60),
    ("magnesium glycinate sleep", "magnesium glycinate", "sleep", 60),
    ("ashwagandha cortisol", "ashwagandha", "cortisol", 60),
    ("sauna recovery", "sauna", "recovery", 60),
    ("miracle mushroom gummies", "miracle mushroom", "", 1),
]


#: Headlines from different outlets. The trend agent counts a topic only when
#: two or more independent outlets carry it, and folds headlines that differ in
#: little but a number into one story, so one outlet with "<q> news 1..6"
#: proposed nothing.
_HEADLINES = (
    "Why {q} is suddenly everywhere",
    "Researchers take a closer look at {q}",
    "{q}: what the experts say",
    "Readers keep asking about {q}",
    "The rise of {q} this autumn",
    "Is {q} worth the hype?",
)


async def _no_sleep(_: float) -> None:
    return None


class ScriptedHttp:
    """Canned PubMed / Europe PMC / News API answers, with faults on top."""

    def __init__(self, recorder: TraceRecorder, faults: FaultInjector) -> None:
        self._recorder = recorder
        self._faults = faults

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        tool, operation = classify(url, params)
        request = {"url": url, "params": sanitize(params)}
        started = time.monotonic()
        fault = self._faults.match(tool, operation, params)
        if fault is not None:
            try:
                response = fault_response(fault, url, dict(params or {}), tool, operation)
            except httpx.HTTPError as exc:
                self._recorder.record(
                    tool=tool,
                    operation=operation,
                    request=request,
                    status="fault",
                    fault=fault.kind,
                    error=str(exc),
                )
                raise
            self._recorder.record(
                tool=tool,
                operation=operation,
                request=request,
                status="fault",
                fault=fault.kind,
                http_status=response.status_code,
            )
            return response
        body = self._answer(tool, operation, dict(params or {}))
        self._recorder.record(
            tool=tool,
            operation=operation,
            request=request,
            http_status=200,
            latency_ms=round((time.monotonic() - started) * 1000, 1),
        )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            text=json.dumps(body),
            request=httpx.Request("GET", url),
        )

    @staticmethod
    def _size(text: str) -> int:
        return 1 if "miracle" in text.lower() else 60

    def _answer(self, tool: str, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        if tool == "pubmed":
            terms = str(params.get("term", ""))
            total = self._size(terms)
            if "mindate" in params:
                total = 0 if total == 1 else 8
            elif "systematic[sb]" in terms:
                total = 0 if total == 1 else 3
            elif "randomized controlled trial" in terms:
                total = 0 if total == 1 else 6
            return {"esearchresult": {"count": str(total), "idlist": []}}
        if tool == "europe_pmc":
            query = str(params.get("query", ""))
            total = self._size(query)
            if "FIRST_PDATE" in query:
                total = 0 if total == 1 else 7
            elif "PUB_TYPE" in query:
                total = 0 if total == 1 else 4
            return {"hitCount": total, "resultList": {"result": []}}
        if tool == "news_api":
            q = str(params.get("q", "")).strip('"')
            return {
                "status": "ok",
                "totalResults": 6,
                "articles": [
                    {
                        "title": headline.format(q=q),
                        "url": f"https://outlet{i}.example/{i}",
                        "source": {"name": f"Outlet {i}"},
                        "publishedAt": "2026-09-28T09:00:00Z",
                    }
                    for i, headline in enumerate(_HEADLINES)
                ],
            }
        return {}


class _FakeDDGS:
    """``ddgs.DDGS`` with canned answers; raises when told to be down."""

    def __init__(self, *, web_down: bool, news_down: bool) -> None:
        self._web_down = web_down
        self._news_down = news_down

    def text(self, query: str, *, region: str, max_results: int) -> list[dict[str, str]]:
        if self._web_down:
            raise RuntimeError("ddgs: Ratelimit (scripted)")
        word = query.split()[0].lower()
        return [
            {
                "title": f"{word} study {i}",
                "href": f"https://www.nih.gov/{word}/{i}",
                "body": word,
            }
            for i in range(4)
        ] + [
            {"title": f"{word} blog {i}", "href": f"https://blog{i}.example.com", "body": word}
            for i in range(4)
        ]

    def news(
        self, query: str, *, region: str, timelimit: str, max_results: int
    ) -> list[dict[str, str]]:
        if self._news_down:
            raise RuntimeError("ddgs: news timed out (scripted)")
        return [
            {
                "title": headline.format(q=query),
                "url": f"https://news{i}.example/{i}",
                "source": f"Paper {i}",
                "date": "2026-09-28T10:00:00+00:00",
            }
            for i, headline in enumerate(_HEADLINES)
        ]


class _Trends:
    name = "google_trends"

    def __init__(self, *, down: bool) -> None:
        self._down = down

    async def related_queries(self, seed: str, *, window_days: int, geo: str) -> RelatedQueries:
        if self._down:
            raise ProviderUnavailable("google trends answered 429 (scripted)")
        if seed != "creatine":
            return RelatedQueries()
        return RelatedQueries(rising=[RelatedQuery(query=q, value=700) for q, *_ in TOPICS])

    async def interest_over_time(
        self, queries: list[str], *, days: int, geo: str
    ) -> dict[str, list[float]]:
        half = days // 2
        return {query: [20.0] * half + [35.0] * half for query in queries}


class _Triage:
    def __init__(self, *, down: bool) -> None:
        self._down = down
        self._by_query = {q: (subject, outcome) for q, subject, outcome, _ in TOPICS}

    async def triage(
        self, clusters: list[QueryCluster], examples: Examples | None = None
    ) -> TriageOutput:
        if self._down:
            raise LLMError("research triage call failed: model unavailable (scripted)")
        topics = []
        for cluster in clusters:
            subject, outcome = self._by_query.get(
                cluster.representative.query, (cluster.representative.query, "")
            )
            topics.append(
                TriageTopic(
                    ids=[cluster.cluster_id],
                    canonical_topic=cluster.representative.query.capitalize(),
                    category=Category.SUPPLEMENTS,
                    is_wellness_topic=True,
                    subject=subject,
                    outcome=outcome,
                    reader_question=f"Does {subject} work?",
                )
            )
        return TriageOutput(topics=topics)


class _Extraction:
    async def extract(self, payload: ExtractionInput) -> LLMResult[ExtractionOutput]:
        product = payload.topic.lower()
        return LLMResult(
            output=ExtractionOutput(
                product=product,
                target_claims=[f"{product} improves sleep"],
                ingredients=[product.split()[0]],
            ),
            usage=TokenUsage(),
            model="eval/scripted",
        )


class _Embedder:
    model_id = "eval/one-hot"
    dimension = 64

    def __init__(self, *, down: bool) -> None:
        self._down = down
        self._index: dict[str, int] = {}

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._down:
            raise RuntimeError("embedding provider unreachable (scripted)")
        vectors = []
        for text in texts:
            slot = self._index.setdefault(text.lower(), len(self._index) % self.dimension)
            vectors.append([1.0 if i == slot else 0.0 for i in range(self.dimension)])
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]


@dataclass
class Scenario:
    name: str
    faults: list[FaultSpec]
    web_down: bool = False
    news_ddg_down: bool = False
    trends_down: bool = False
    triage_down: bool = False
    embeddings_down: bool = False
    news_api: bool = False
    #: Fallback success, read off the final state: (passed, detail).
    fallback: Callable[[ResearchState], tuple[bool, str]] | None = None


def _primary_source(state: ResearchState, source: str) -> tuple[bool, str]:
    checked = [c for c in state.candidates if c.science.primary is not None]
    using = [c for c in checked if c.science.primary and c.science.primary.source == source]
    return bool(checked) and len(using) == len(checked), (
        f"{len(using)} of {len(checked)} checked candidates measured by {source}"
    )


def _news_source(state: ResearchState, source: str) -> tuple[bool, str]:
    with_news = [c for c in state.candidates if c.news is not None]
    using = [c for c in with_news if c.news and c.news.source == source]
    return bool(with_news) and len(using) == len(with_news), (
        f"{len(using)} of {len(with_news)} candidates' news came from {source}"
    )


def _triage_fallback(state: ResearchState) -> tuple[bool, str]:
    noted = any(note.startswith("triage failed") for note in state.notes)
    return noted and bool(state.candidates), (
        f"triage fallback note={'yes' if noted else 'no'}, candidates={len(state.candidates)}"
    )


SCENARIOS: dict[str, Scenario] = {
    "nominal": Scenario("nominal", []),
    "web_search_down": Scenario("web_search_down", [], web_down=True),
    "news_api_down": Scenario(
        "news_api_down",
        [FaultSpec(target="news_api", kind="status", status=500)],
        news_api=True,
        fallback=lambda s: _news_source(s, "duckduckgo_news"),
    ),
    "all_news_down": Scenario(
        "all_news_down",
        [FaultSpec(target="news_api", kind="timeout")],
        news_api=True,
        news_ddg_down=True,
    ),
    "pubmed_down": Scenario(
        "pubmed_down",
        [FaultSpec(target="pubmed", kind="status", status=503)],
        fallback=lambda s: _primary_source(s, "europe_pmc"),
    ),
    "europe_pmc_down": Scenario(
        "europe_pmc_down",
        [FaultSpec(target="europe_pmc", kind="malformed_json")],
        fallback=lambda s: _primary_source(s, "pubmed"),
    ),
    "all_science_down": Scenario(
        "all_science_down",
        [
            FaultSpec(target="pubmed", kind="rate_limit"),
            FaultSpec(target="europe_pmc", kind="status", status=503),
        ],
    ),
    "trends_down": Scenario("trends_down", [], trends_down=True),
    "triage_model_down": Scenario(
        "triage_model_down", [], triage_down=True, fallback=_triage_fallback
    ),
    "embeddings_down": Scenario("embeddings_down", [], embeddings_down=True),
}


def _fabrication_checks(state: ResearchState) -> list[dict[str, Any]]:
    """A signal that could not be measured must be absent, never zero."""
    checks = []
    for provider in ("web", "news"):
        status = state.provider_status.get(provider)
        if status is None or status.status != "failed":
            continue
        carried = [c.canonical_topic for c in state.live() if getattr(c, provider) is not None]
        checks.append(
            {
                "name": f"no_fabricated_{provider}_signal",
                "passed": not carried,
                "detail": f"{provider} failed: {len(carried)} candidates carry its signal",
            }
        )
    if state.failed and state.failed.startswith("scientific_evidence_unavailable"):
        selected = state.with_status(ResearchCandidateStatus.SELECTED)
        checks.append(
            {
                "name": "nothing_proposed_without_literature",
                "passed": not selected,
                "detail": f"{len(selected)} topics selected with no literature check",
            }
        )
    return checks


async def run_scenario(case_id: str, scenario_name: str) -> Trace:
    scenario = SCENARIOS[scenario_name]
    recorder = TraceRecorder.start(
        case_id, f"research scenario: {scenario_name}", "scripted", {}
    )
    faults = FaultInjector(scenario.faults)
    http = ScriptedHttp(recorder, faults)
    settings = get_settings().model_copy(
        update={"research_web_pool_size": 6, "research_shortlist_size": 5}
    )

    def throttled(provider: str) -> ThrottledClient:
        return ThrottledClient(
            http, RateLimiter(1000.0), provider=provider, max_retries=2, max_wait_seconds=0.01
        )

    pubmed = PubMedEvidenceProvider(PubMedProvider(throttled("pubmed"), None))
    ddgs = _FakeDDGS(web_down=scenario.web_down, news_down=scenario.news_ddg_down)
    ddg_news = DuckDuckGoNewsProvider(
        region="us-en", client_factory=lambda: ddgs, retries=1, retry_wait=0, sleep=_no_sleep
    )
    news: Any = (
        FallbackNewsProvider(NewsApiProvider(throttled("news_api"), "eval-key"), ddg_news)
        if scenario.news_api
        else ddg_news
    )

    async def history(_: datetime) -> list[PastTopic]:
        return []

    config = ResearchConfig.resolve(
        settings,
        ResearchParams(
            categories=[Category.SUPPLEMENTS],
            enabled_sources=["google_trends", "news", "web"],
        ),
    )
    web = SimpleWebSearchProvider(
        client_factory=lambda: ddgs, retries=1, retry_wait=0, sleep=_no_sleep
    )
    trend = TrendDeps(
        sources=[
            GoogleTrendsSource(
                _Trends(down=scenario.trends_down),
                window_days=config.trend_window_days,
                now=lambda: NOW,
            ),
            NewsSearchSource(news, now=lambda: NOW),
            WebSearchSource(web, now=lambda: NOW),
        ],
        unavailable={},
        triage=_Triage(down=scenario.triage_down),
        embedder=_Embedder(down=scenario.embeddings_down),
        history=history,
        now=lambda: NOW,
    )
    evidence = EvidenceDeps(
        science=pubmed,
        cross_check=EuropePMCEvidenceProvider(EuropePMCProvider(throttled("europe_pmc"))),
        deep_counts=pubmed,
        papers=None,
        extraction=_Extraction(),
        now=lambda: NOW,
    )
    state = ResearchState(
        run_id="00000000-0000-0000-0000-00000000e7a1",
        label=f"eval_{scenario_name}",
        mode=ResearchRunMode.MANUAL,
        config=config,
        today=TODAY,
    )
    try:
        final = await ResearchAgent(trend, evidence=evidence).run(state)
    except Exception as exc:  # the graph records node failures; anything here escaped it
        recorder.trace.error = {"type": type(exc).__name__, "message": str(exc)}
        return recorder.finish(Outcome.CRASHED)

    checks = _fabrication_checks(final)
    fallback = None
    if scenario.fallback is not None:
        passed, detail = scenario.fallback(final)
        fallback = {"passed": passed, "detail": detail}
    recorder.trace.research = {
        "scenario": scenario_name,
        "failed": final.failed,
        "stage": str(final.stage),
        "provider_status": {
            name: status.model_dump() for name, status in final.provider_status.items()
        },
        "notes": final.notes,
        "selected": [
            c.canonical_topic for c in final.with_status(ResearchCandidateStatus.SELECTED)
        ],
        "candidates": [
            {
                "topic": c.canonical_topic,
                "status": str(c.status),
                "news_source": c.news.source if c.news else None,
                "primary_source": c.science.primary.source if c.science.primary else None,
                "unavailable": c.scores.unavailable,
                "discard_reason": c.discard_reason,
            }
            for c in final.candidates
        ],
        "checks": checks,
        "fallback": fallback,
        "faults_fired": faults.fired(),
    }
    return recorder.finish(
        Outcome.RESEARCH_FAILED if final.failed else Outcome.RESEARCH_COMPLETED
    )
