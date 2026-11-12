"""The research graph end to end against fake sources: branches, bounds, guarantees.

No network, no model, no database. Each test builds a small fake internet —
which outlets ran which headlines, what Google Trends says is rising, how
GDELT's volume moved, what PubMed counts — and checks what the trend agent
ranks, what the evidence check lets through, and what neither may do.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.config import Settings
from app.domain.contracts import ExtractionInput, ExtractionOutput
from app.domain.enums import ResearchCandidateStatus, ResearchRunMode
from app.llm.base import LLMResult, TokenUsage
from app.research.contracts import (
    Category,
    EvidenceCounts,
    PastTopic,
    ResearchConfig,
    ResearchParams,
    ResearchStage,
    ResearchState,
)
from app.research.evidence import MAX_BACKFILL_ROUNDS, EvidenceDeps
from app.research.graph import ResearchAgent
from app.research.normalize import QueryCluster, normalize_query
from app.research.providers.base import (
    EvidenceQuery,
    ProviderUnavailable,
    RelatedQueries,
    RelatedQuery,
)
from app.research.trends.contracts import (
    Capability,
    DailySeries,
    SourceBatch,
    SourceItem,
    SourceQuery,
    SourceType,
)
from app.research.trends.documents import make_item
from app.research.trends.graph import TrendDeps
from app.research.trends.history import InMemoryTopicStore, Observation, topic_key
from app.research.trends.sources.adapters import GoogleTrendsSource
from app.research.trends.sources.base import DiscoveryPlan
from app.research.triage import TriageOutput, TriageTopic
from app.retrieval.base import SearchQuery
from app.retrieval.query_builder import UnanchoredQuery

NOW = datetime(2026, 10, 12, 8, tzinfo=UTC)
TODAY = NOW.date()
ANCHOR = "vitamin d"

STRONG = {"total": 60, "recent": 8, "reviews": 3, "rcts": 6}
MODERATE = {"total": 25, "recent": 3, "reviews": 1, "rcts": 2}
NOTHING = {"total": 1, "recent": 0, "reviews": 0, "rcts": 0}
THIN = {"total": 3, "recent": 0, "reviews": 0, "rcts": 0}

_ANGLES = (
    "{s} may improve {o}, study finds",
    "What the new research says about {s} and {o}",
    "{s} for {o}: experts weigh in",
    "Doctors explain {s} and {o}",
    "Is {s} worth it for {o}?",
    "A trial on {s} and {o} surprised researchers",
)


_NEWS_ANGLES = (
    "Sales of {s} jump as interest grows",
    "Readers ask whether {s} actually works",
    "New {s} results published this week",
    "Why everyone is talking about {s}",
    "A dietitian on the hype around {s}",
    "The {s} boom, explained",
    "Inside the {s} craze",
    "Should you try {s}? A guide",
)


@dataclass
class Topic:
    subject: str
    outcome: str
    query: str
    outlets: int = 4
    rising: int = 500
    interest: tuple[float, float] = (20.0, 40.0)  # previous, current
    volume: tuple[float, float] = (2.0, 5.0)  # per day: baseline, recent week
    evidence: dict[str, int] = field(default_factory=lambda: dict(STRONG))
    wellness: bool = True

    def headlines(self) -> list[str]:
        return [
            _ANGLES[i % len(_ANGLES)].format(s=self.subject, o=self.outcome or "health")
            for i in range(self.outlets)
        ]


TOPICS = [
    Topic("creatine", "sleep", "creatine before bed", rising=900, interest=(10, 40)),
    Topic("magnesium glycinate", "sleep", "magnesium glycinate sleep", interest=(20, 30)),
    Topic("ashwagandha", "cortisol", "ashwagandha cortisol", interest=(15, 30)),
    Topic(
        "zone 2 training",
        "endurance",
        "zone 2 training",
        interest=(30, 45),
        evidence=dict(MODERATE),
    ),
    Topic("sauna", "recovery", "sauna recovery", interest=(10, 18)),
    Topic(
        "miracle mushroom",
        "focus",
        "miracle mushroom gummies",
        rising=5500,
        evidence=dict(NOTHING),
    ),
    Topic(
        "taylor swift",
        "workout",
        "taylor swift workout",
        rising=4000,
        outlets=0,
        wellness=False,
    ),
]


def _slug(text: str) -> str:
    return "-".join(normalize_query(text).split())[:40]


def _item(
    source: str,
    kind: SourceType,
    title: str,
    url: str,
    when: datetime,
    publisher: str | None = None,
) -> SourceItem:
    item = make_item(
        source_name=source,
        source_type=kind,
        title=title,
        url=url,
        publisher=publisher,
        published_at=when,
        collected_at=NOW,
        category=Category.SUPPLEMENTS,
        seed="feed",
    )
    assert item is not None
    return item


def _topic_for(text: str, topics: list[Topic]) -> Topic | None:
    words = set(normalize_query(text).split())
    for topic in topics:
        if set(normalize_query(topic.subject).split()) <= words:
            return topic
    return None


# --- fake sources --------------------------------------------------------------


class FakeTrendsProvider:
    name = "google_trends"

    def __init__(self, topics: list[Topic], *, down: bool = False) -> None:
        self._topics = topics
        self._down = down
        self.related_calls = 0

    async def related_queries(self, seed: str, *, window_days: int, geo: str) -> RelatedQueries:
        self.related_calls += 1
        if self._down:
            raise ProviderUnavailable("google trends 429")
        if seed != "creatine":  # every rising query comes back once, under one seed
            return RelatedQueries()
        return RelatedQueries(
            rising=[RelatedQuery(query=t.query, value=t.rising) for t in self._topics]
        )

    async def interest_over_time(
        self, queries: list[str], *, days: int, geo: str
    ) -> dict[str, list[float]]:
        series: dict[str, list[float]] = {}
        for query in queries:
            if query == ANCHOR:
                series[query] = [20.0] * days
                continue
            topic = _topic_for(query, self._topics)
            previous, current = topic.interest if topic else (0.0, 0.0)
            half = days // 2
            series[query] = [previous] * half + [current] * half
        return series


class FakeFeeds:
    """Publisher feeds: each topic reported by ``outlets`` different outlets,
    plus one wire story syndicated word for word to three more sites."""

    name = "rss"
    source_type = SourceType.FEED
    capabilities = frozenset({Capability.DISCOVER})

    def __init__(
        self,
        topics: list[Topic],
        *,
        down: bool = False,
        empty: bool = False,
        permanent: bool = False,
    ) -> None:
        self._topics = topics
        self._down, self._empty, self._permanent = down, empty, permanent
        self.calls = 0

    def discovery(self, plan: DiscoveryPlan) -> list[SourceQuery]:
        return [
            SourceQuery(since=plan.since, until=plan.until, target=f"feed{i}") for i in range(2)
        ]

    async def search(self, query: SourceQuery) -> SourceBatch:
        self.calls += 1
        if self._down or self._permanent:
            raise ProviderUnavailable("feed down", permanent=self._permanent)
        if self._empty or query.target != "feed0":
            return SourceBatch()
        items = []
        for topic in self._topics:
            for i, headline in enumerate(topic.headlines()):
                items.append(
                    _item(
                        self.name,
                        self.source_type,
                        headline,
                        f"https://www.{_slug(topic.subject)}-outlet{i}.com/{_slug(headline)}?utm_source=rss",
                        NOW - timedelta(days=1 + i % 3),
                    )
                )
        wire = "Wire: sleep researchers report new findings on bedtime routines"
        for site in ("alpha-times.com", "beta-post.com", "gamma-herald.com"):
            items.append(_item(self.name, self.source_type, wire, f"https://{site}/wire", NOW))
        return SourceBatch(items=items, total=len(items))


class FakeNews:
    """Topic searches: recent coverage, an earlier week, and one irrelevant result."""

    name = "news"
    source_type = SourceType.NEWS
    capabilities = frozenset({Capability.SEARCH})

    def __init__(self, topics: list[Topic], *, down: bool = False, hang: bool = False) -> None:
        self._topics = topics
        self._down, self._hang = down, hang
        self.calls = 0

    async def search(self, query: SourceQuery) -> SourceBatch:
        self.calls += 1
        if self._hang:
            await asyncio.sleep(30)
        if self._down:
            raise ProviderUnavailable("news down")
        topic = _topic_for(query.text, self._topics)
        items = [
            _item(
                self.name,
                self.source_type,
                "Celebrity diet secrets revealed",
                "https://gossip.example/diet",
                NOW - timedelta(days=1),
            )
        ]
        if topic is not None and topic.outlets:
            for i in range(topic.outlets + 2):
                items.append(
                    _item(
                        self.name,
                        self.source_type,
                        _NEWS_ANGLES[i % len(_NEWS_ANGLES)].format(s=topic.subject),
                        f"https://www.daily{i}-{_slug(topic.subject)}.net/story",
                        NOW - timedelta(days=2),
                        publisher=f"Daily {i}",
                    )
                )
            for i in range(2):
                items.append(
                    _item(
                        self.name,
                        self.source_type,
                        ("Last month's look at {s}", "{s}: an earlier explainer")[i].format(
                            s=topic.subject
                        ),
                        f"https://www.archive{i}-{_slug(topic.subject)}.org/old",
                        NOW - timedelta(days=10),
                    )
                )
        return SourceBatch(items=items, total=len(items))


class FakeGdelt:
    name = "gdelt"
    source_type = SourceType.NEWS
    capabilities = frozenset({Capability.VOLUME})

    def __init__(self, topics: list[Topic], *, down: bool = False) -> None:
        self._topics = topics
        self._down = down
        self.calls = 0

    async def search(self, query: SourceQuery) -> SourceBatch:  # pragma: no cover
        raise AssertionError("enrichment asks GDELT for volume only")

    async def volume(self, query: SourceQuery) -> DailySeries:
        self.calls += 1
        if self._down:
            raise ProviderUnavailable("gdelt: Please limit requests")
        topic = _topic_for(query.text, self._topics)
        base, recent = topic.volume if topic else (0.0, 0.0)
        start = query.since.date()
        days = [start + timedelta(days=n) for n in range((query.until.date() - start).days + 1)]
        cut = query.until.date() - timedelta(days=7)
        return DailySeries(
            source=self.name,
            query=query.text,
            dates=days,
            values=[recent if d >= cut else base for d in days],
        )


class FakeTriage:
    def __init__(self, topics: list[Topic]) -> None:
        self._topics = topics

    async def triage(self, clusters: list[QueryCluster], examples: Any = None) -> TriageOutput:
        items = []
        for cluster in clusters:
            topic = _topic_for(cluster.representative.query, self._topics)
            if topic is None:
                items.append(
                    TriageTopic(
                        ids=[cluster.cluster_id],
                        canonical_topic=cluster.representative.query[:80]
                        if len(cluster.representative.query) >= 3
                        else "unknown topic",
                        category=Category.SUPPLEMENTS,
                        is_wellness_topic=False,
                        subject=cluster.representative.query[:60] or "x",
                    )
                )
                continue
            items.append(
                TriageTopic(
                    ids=[cluster.cluster_id],
                    canonical_topic=topic.query.capitalize(),
                    category=Category.SUPPLEMENTS,
                    is_wellness_topic=topic.wellness,
                    subject=topic.subject,
                    outcome=topic.outcome,
                    reader_question=f"Does {topic.subject} help {topic.outcome}?",
                )
            )
        return TriageOutput(topics=items)


class FakeEmbedder:
    """One-hot per normalised text: identical texts are 1.0 apart, others 0."""

    model_id = "fake"
    dimension = 64

    def __init__(self) -> None:
        self._index: dict[str, int] = {}

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            slot = self._index.setdefault(text.lower(), len(self._index)) % self.dimension
            vectors.append([1.0 if i == slot else 0.0 for i in range(self.dimension)])
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]


# --- fake evidence -------------------------------------------------------------


def _counts(values: dict[str, int], source: str = "pubmed") -> EvidenceCounts:
    return EvidenceCounts(
        source=source,
        query="q",
        total=values["total"],
        recent=values["recent"],
        reviews_or_meta=values["reviews"],
        rcts=values["rcts"],
    )


class FakeScience:
    def __init__(self, topics: list[Topic], name: str, *, down: bool = False) -> None:
        self.name = name
        self._by_subject = {t.subject: t.evidence for t in topics}
        self._down = down
        self.subjects: list[str] = []

    async def profile(self, query: EvidenceQuery) -> EvidenceCounts:
        self.subjects.append(query.subject)
        if self._down:
            raise ProviderUnavailable(f"{self.name} returned 503")
        return _counts(self._by_subject.get(query.subject, NOTHING), self.name)


class FakeDeep:
    name = "pubmed"

    def __init__(self, overrides: dict[str, dict[str, int]] | None = None) -> None:
        self._overrides = {t.query.capitalize().lower(): t.evidence for t in TOPICS} | (
            overrides or {}
        )

    async def profile(self, query: EvidenceQuery) -> EvidenceCounts:  # pragma: no cover
        raise AssertionError("deep research counts terms, not questions")

    async def profile_terms(self, terms: str, recent_since: str) -> EvidenceCounts:
        return _counts(self._overrides.get(terms.removeprefix("T:"), STRONG))


class FakeExtraction:
    async def extract(self, payload: ExtractionInput) -> LLMResult[ExtractionOutput]:
        product = payload.topic.lower()
        return LLMResult(
            output=ExtractionOutput(
                product=product,
                target_claims=[f"{product} helps"],
                ingredients=[product.split()[0]],
            ),
            usage=TokenUsage(),
            model="ollama/fake",
        )


class FakeStrategy:
    def __init__(self, unanchored: set[str] | None = None) -> None:
        self._unanchored = unanchored or set()

    def build(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        if product in self._unanchored:
            raise UnanchoredQuery(claim, product, [])
        return [SearchQuery(claim=claim, terms=f"T:{product}")]

    def broaden(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        return self.build(claim, product, ingredients)


# --- building a run ------------------------------------------------------------


@dataclass
class World:
    topics: list[Topic] = field(default_factory=lambda: list(TOPICS))
    trends: FakeTrendsProvider | None = None
    feeds: FakeFeeds | None = None
    news: FakeNews | None = None
    gdelt: FakeGdelt | None = None
    history: list[PastTopic] = field(default_factory=list)
    embedder: FakeEmbedder | None = None
    store: InMemoryTopicStore | None = None
    pubmed_down: bool = False
    epmc_down: bool = False
    deep: FakeDeep | None = None
    strategy: FakeStrategy | None = None

    def sources(self) -> list[Any]:
        trends = self.trends or FakeTrendsProvider(self.topics)
        self.trends = trends
        self.feeds = self.feeds or FakeFeeds(self.topics)
        self.news = self.news or FakeNews(self.topics)
        self.gdelt = self.gdelt or FakeGdelt(self.topics)
        return [
            GoogleTrendsSource(trends, window_days=7, now=lambda: NOW),
            self.feeds,
            self.gdelt,
            self.news,
        ]

    def trend_deps(self) -> TrendDeps:
        async def history(since: datetime) -> list[PastTopic]:
            return self.history

        return TrendDeps(
            sources=self.sources(),
            unavailable={"youtube": "no YOUTUBE_API_KEY configured"},
            triage=FakeTriage(self.topics),
            embedder=self.embedder,
            history=history,
            topics=self.store,
            now=lambda: NOW,
        )

    def evidence_deps(self) -> EvidenceDeps:
        return EvidenceDeps(
            science=FakeScience(self.topics, "pubmed", down=self.pubmed_down),
            cross_check=FakeScience(self.topics, "europe_pmc", down=self.epmc_down),
            deep_counts=self.deep or FakeDeep(),
            papers=None,
            extraction=FakeExtraction(),
            query_strategy=self.strategy or FakeStrategy(),
            now=lambda: NOW,
        )


def _state(**settings: Any) -> ResearchState:
    base: dict[str, Any] = {
        "research_enabled_sources": ["google_trends", "rss", "gdelt", "news", "youtube"],
        "research_collect_deadline_seconds": 5,
        "research_enrich_deadline_seconds": 5,
        **settings,
    }
    config = ResearchConfig.resolve(
        Settings(**base), ResearchParams(categories=[Category.SUPPLEMENTS])
    )
    return ResearchState(
        run_id="00000000-0000-0000-0000-000000000001",
        label="research_test",
        mode=ResearchRunMode.MANUAL,
        config=config,
        today=TODAY,
    )


async def _run(
    world: World | None = None,
    state: ResearchState | None = None,
    *,
    evidence: bool = True,
    checkpoint: Any = None,
) -> ResearchState:
    world = world or World()
    agent = ResearchAgent(
        world.trend_deps(),
        evidence=world.evidence_deps() if evidence else None,
        checkpoint=checkpoint,
    )
    return await agent.run(state or _state())


def _selected(state: ResearchState) -> list[str]:
    return [c.subject for c in state.with_status(ResearchCandidateStatus.SELECTED)]


def _by_subject(state: ResearchState, subject: str) -> Any:
    return next(c for c in state.candidates if c.subject == subject)


# --- the ordinary path ---------------------------------------------------------


async def test_a_run_proposes_trending_topics_that_pass_the_evidence_check() -> None:
    state = await _run()

    assert state.failed is None
    assert state.stage is ResearchStage.COMPLETED
    selected = _selected(state)
    assert sorted(selected) == sorted(
        ["creatine", "magnesium glycinate", "ashwagandha", "zone 2 training", "sauna"]
    )
    for candidate in state.with_status(ResearchCandidateStatus.SELECTED):
        assert candidate.scores.overall is not None
        assert candidate.metrics is not None
        assert candidate.metrics.independent_publishers >= 3
        assert candidate.supporting, "every proposal links what it rests on"
        assert candidate.topic_ref_id is None  # no topic store in this world
    assert state.provider_status["youtube"].status == "unavailable"
    assert state.provider_status["google_news"].status == "skipped"


async def test_nothing_is_selected_before_the_evidence_check_has_run() -> None:
    """The desk polls mid-run: a Promote button before the gate would bypass it."""
    seen: list[tuple[ResearchStage, set[ResearchCandidateStatus]]] = []

    async def checkpoint(state: ResearchState) -> None:
        seen.append((state.stage, {c.status for c in state.candidates}))

    await _run(checkpoint=checkpoint)
    evidence_stages = {
        ResearchStage.RESEARCHING_SCIENCE,
        ResearchStage.DEEP_RESEARCH,
        ResearchStage.SELECTING_TOPICS,
        ResearchStage.BACKFILLING,
        ResearchStage.COMPLETED,
    }
    before_gate = [statuses for stage, statuses in seen if stage not in evidence_stages]
    assert before_gate
    assert all(ResearchCandidateStatus.SELECTED not in s for s in before_gate)
    assert all(ResearchCandidateStatus.SHORTLISTED not in s for s in before_gate)


async def test_the_trend_score_never_contains_evidence() -> None:
    state = await _run()
    creatine = _by_subject(state, "creatine")
    assert set(creatine.scores.components) <= {
        "momentum",
        "coverage",
        "audience",
        "freshness",
        "editorial_fit",
    }
    assert state.trend_summary is not None
    assert "evidence" not in str(state.trend_summary["topics"][0]["score_breakdown"])
    assert all(
        t["scientific_assessment"] == "not_assessed" for t in state.trend_summary["topics"]
    )


async def test_syndicated_copies_count_once_and_duplicates_are_reported() -> None:
    state = await _run()
    removed = state.counts["duplicates_removed"]
    assert removed["title"] >= 2  # the wire story on three sites is one story
    # Every exact-URL repeat (feed item found again by enrichment) folded too.
    assert sum(removed.values()) >= 2


async def test_a_viral_topic_without_literature_is_never_proposed() -> None:
    state = await _run(state=_state(research_evidence_pool_size=8))
    viral = _by_subject(state, "miracle mushroom")
    assert viral.trend is not None and viral.trend.is_breakout
    assert viral.status is ResearchCandidateStatus.DISCARDED
    assert (viral.discard_reason or "").startswith("evidence limited")


async def test_a_non_wellness_trend_is_kept_as_discarded_with_its_reason() -> None:
    state = await _run()
    celebrity = _by_subject(state, "taylor swift")
    assert celebrity.status is ResearchCandidateStatus.DISCARDED
    assert "not a wellness" in (celebrity.discard_reason or "")


async def test_the_standalone_trend_agent_ranks_but_never_selects() -> None:
    state = await _run(evidence=False)
    assert state.failed is None
    assert _selected(state) == []
    assert not any(c.science.primary for c in state.candidates)
    summary = state.trend_summary
    assert summary is not None and summary["status"] == "completed"
    shortlist = [t for t in summary["topics"] if t["trend_shortlisted"]]
    assert 1 <= len(shortlist) <= state.config.target_article_count
    stages = [entry["stage"] for entry in state.stage_log]
    assert stages[-1] == "selecting_trends"


async def test_every_supporting_source_is_a_collected_record() -> None:
    state = await _run(evidence=False)
    collected = {item.url for item in state.items}
    assert state.trend_summary is not None
    for topic in state.trend_summary["topics"]:
        assert topic["supporting_sources"]
        for source in topic["supporting_sources"]:
            assert source["url"] in collected


async def test_two_runs_over_the_same_world_rank_identically() -> None:
    first = await _run(evidence=False)
    second = await _run(evidence=False)

    def ranking(state: ResearchState) -> list[tuple[str, int | None, float | None]]:
        return sorted((c.canonical_topic, c.rank, c.scores.overall) for c in state.candidates)

    assert ranking(first) == ranking(second)


# --- sources failing -------------------------------------------------------------


async def test_every_source_down_fails_the_run_rather_than_inventing_topics() -> None:
    world = World(
        trends=FakeTrendsProvider(TOPICS, down=True),
        feeds=FakeFeeds(TOPICS, down=True),
    )
    state = await _run(world)
    assert state.failed is not None and state.failed.startswith("no_trend_data")
    assert state.stage is ResearchStage.FAILED
    assert state.candidates == []
    assert state.provider_status["google_trends"].status == "failed"
    assert state.provider_status["rss"].status == "failed"


async def test_sources_that_answer_with_nothing_complete_an_empty_run() -> None:
    world = World(
        topics=[],
        feeds=FakeFeeds([], empty=True),
    )
    state = await _run(world)
    assert state.failed is None
    assert _selected(state) == []
    assert any("no candidate topics" in note for note in state.notes)


async def test_news_down_is_missing_coverage_data_not_zero_interest() -> None:
    world = World(news=FakeNews(TOPICS, down=True))
    state = await _run(world, evidence=False)

    assert state.failed is None
    assert state.provider_status["news"].status == "failed"
    creatine = _by_subject(state, "creatine")
    momentum = creatine.scores.dimensions["momentum"]
    assert momentum.signals["news_counts"] is None  # not measured, not 0
    assert momentum.score is not None and momentum.score > 0
    assert creatine.scores.data_coverage < 1.0


async def test_a_source_refusing_for_good_is_not_asked_again() -> None:
    feeds = FakeFeeds(TOPICS, permanent=True)
    state = await _run(World(feeds=feeds), evidence=False)
    assert feeds.calls == 1
    assert state.provider_status["rss"].status == "failed"
    assert "stopped asking" in (state.provider_status["rss"].detail or "")


async def test_three_failures_in_a_row_open_the_circuit() -> None:
    gdelt = FakeGdelt(TOPICS, down=True)
    state = await _run(World(gdelt=gdelt), evidence=False)
    assert gdelt.calls == 3
    assert state.provider_status["gdelt"].status == "failed"
    assert state.failed is None


async def test_the_request_budget_caps_a_source_and_says_so() -> None:
    news = FakeNews(TOPICS)
    state = await _run(
        World(news=news), _state(research_max_requests_per_source=2), evidence=False
    )
    assert news.calls == 2
    status = state.provider_status["news"]
    assert status.status == "partial"
    assert "budget" in (status.detail or "")


async def test_a_source_that_hangs_is_cut_off_at_the_deadline() -> None:
    news = FakeNews(TOPICS, hang=True)
    state = await _run(
        World(news=news), _state(research_enrich_deadline_seconds=1), evidence=False
    )
    assert state.failed is None
    assert state.provider_status["news"].status == "partial"
    assert "deadline" in (state.provider_status["news"].detail or "")


async def test_europe_pmc_stands_in_when_pubmed_is_down() -> None:
    state = await _run(World(pubmed_down=True))
    assert state.failed is None
    assert _selected(state)
    chosen = state.with_status(ResearchCandidateStatus.SELECTED)[0]
    assert chosen.science.primary is not None
    assert chosen.science.primary.source == "europe_pmc"
    assert state.provider_status["pubmed"].status == "failed"


async def test_with_no_literature_source_the_run_fails_but_keeps_its_trends() -> None:
    state = await _run(World(pubmed_down=True, epmc_down=True))
    assert state.failed is not None
    assert state.failed.startswith("scientific_evidence_unavailable")
    assert _selected(state) == []
    assert state.trend_summary is not None  # the trend output was frozen first


# --- the evidence check ---------------------------------------------------------


async def test_only_the_trend_ranked_pool_is_checked() -> None:
    world = World()
    deps = world.evidence_deps()
    agent = ResearchAgent(world.trend_deps(), evidence=deps)
    state = await agent.run(
        _state(
            research_evidence_pool_size=2,
            research_target_article_count=4,
            research_min_article_count=1,
        )
    )
    assert isinstance(deps.science, FakeScience)
    pooled = {c.subject for c in state.candidates if c.in_evidence_pool}
    assert set(deps.science.subjects) <= pooled
    assert len(set(deps.science.subjects)) < len([c for c in state.candidates if c.rank])


async def test_a_topic_the_pipeline_could_not_anchor_is_discarded() -> None:
    state = await _run(World(strategy=FakeStrategy(unanchored={"ashwagandha cortisol"})))
    ashwagandha = _by_subject(state, "ashwagandha")
    assert ashwagandha.status is ResearchCandidateStatus.DISCARDED
    assert "could not anchor" in (ashwagandha.discard_reason or "")


async def test_backfill_runs_once_and_sends_new_topics_through_the_whole_check() -> None:
    deep = FakeDeep(
        {
            "creatine before bed": THIN,
            "magnesium glycinate sleep": THIN,
            "ashwagandha cortisol": THIN,
        }
    )
    state = await _run(
        World(deep=deep),
        _state(research_evidence_pool_size=4, research_target_article_count=4),
    )
    stages = [entry["stage"] for entry in state.stage_log]
    assert stages.count("backfilling") == MAX_BACKFILL_ROUNDS
    assert stages.count("researching_science") == 2
    assert state.backfill_round == MAX_BACKFILL_ROUNDS
    assert state.failed is None
    assert any("requested" in note for note in state.notes)


# --- history ------------------------------------------------------------------


async def test_a_topic_already_written_is_not_proposed_again() -> None:
    history = [PastTopic(text="Creatine before bed", kind="article", decided_at=NOW)]
    state = await _run(World(embedder=FakeEmbedder(), history=history))
    repeat = next(c for c in state.candidates if c.canonical_topic == "Creatine before bed")
    assert repeat.status is ResearchCandidateStatus.DISCARDED
    assert "already written about" in (repeat.discard_reason or "")


def _observe(store: InMemoryTopicStore, key: str, observation: Observation) -> None:
    """Seed an earlier run's sighting of a topic, as ``service.finish`` would."""
    store.topics[key].observations.insert(0, observation)
    if observation.run_id not in {run for run, _ in store.runs}:
        store.runs.append((observation.run_id, observation.observed_at))


async def test_topics_get_a_persistent_identity_and_history() -> None:
    store = InMemoryTopicStore()
    first = await _run(World(store=store), evidence=False)
    creatine = _by_subject(first, "creatine")
    assert creatine.topic_ref_id is not None
    assert creatine.trend_status is not None

    key = topic_key("creatine", "sleep")
    _observe(
        store,
        key,
        Observation(
            run_id="run-before",
            observed_at=NOW - timedelta(days=7),
            stories=2,
            proposed=False,
        ),
    )
    second = await _run(World(store=store), evidence=False)
    again = _by_subject(second, "creatine")
    assert again.topic_ref_id == creatine.topic_ref_id
    assert again.metrics is not None and again.metrics.history is not None
    assert again.metrics.history.previous_observations == 1
    assert again.scores.dimensions["momentum"].signals["history"] is not None


async def test_a_recently_proposed_topic_with_nothing_new_is_suppressed() -> None:
    store = InMemoryTopicStore()
    await _run(World(store=store), evidence=False)  # creates the topic rows
    for topic in TOPICS:
        key = topic_key(topic.subject, topic.outcome)
        if key in store.topics:
            _observe(
                store,
                key,
                Observation(
                    run_id="last-week",
                    observed_at=NOW - timedelta(days=7),
                    stories=10,
                    proposed=True,
                ),
            )
    state = await _run(World(store=store), evidence=False)
    repeats = [
        c for c in state.candidates if "not meaningfully changed" in (c.discard_reason or "")
    ]
    assert repeats
    assert state.counts["repeats_suppressed"] == len(repeats)


async def test_insufficient_data_is_a_discard_with_its_reason_never_a_low_score() -> None:
    state = await _run(
        World(news=FakeNews(TOPICS, down=True), gdelt=FakeGdelt(TOPICS, down=True)),
        _state(research_min_data_coverage=0.95),
        evidence=False,
    )
    thin = [c for c in state.candidates if (c.discard_reason or "").startswith("insufficient")]
    assert thin
    assert all(c.scores.overall is None for c in thin)


def test_recursion_limit_covers_a_backfill_round() -> None:
    from app.research.graph import recursion_limit

    assert recursion_limit(True) >= 8 + 4 + 4
    assert recursion_limit(False) >= 8


async def test_an_ordinary_run_checkpoints_every_node_twice() -> None:
    stages: list[ResearchStage] = []

    async def checkpoint(state: ResearchState) -> None:
        stages.append(state.stage)

    state = await _run(checkpoint=checkpoint)
    assert stages[:2] == [ResearchStage.LOADING_CONFIG] * 2
    assert len(stages) == 2 * len(state.stage_log)
    assert ResearchStage.BACKFILLING not in stages


def test_the_today_field_is_a_date() -> None:
    assert isinstance(_state().today, date)
