"""Shapes the research agent passes around, and the state its graph threads.

A research run has two halves (``graph.py``): the **Trend Search Agent**
(``trends/``), which finds and scores what people are getting interested in,
and the downstream **evidence check** (``evidence.py``), which asks PubMed and
Europe PMC whether a responsible article could be written about it. Both write
the same candidates, so the shapes live here; the trend-only shapes live in
``trends/contracts.py``.

Every signal is a record of what a source actually returned. A field that is
``None`` means "not measured" and is never a stand-in for zero: the trend
scorer leaves unmeasured dimensions out and renormalises, and says so in
``TrendScores.unavailable``.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.config import Settings
from app.domain.enums import ResearchCandidateStatus, ResearchRunMode
from app.research.categories import Category
from app.research.trends.contracts import (
    SourceItem,
    Story,
    SupportingSource,
    TopicMetrics,
    TrendScores,
    TrendStatus,
)

__all__ = ["Category"]

#: Every trend source a run can use, by the name triggers enable them with.
SOURCE_NAMES = (
    "google_trends",
    "rss",
    "google_news",
    "gdelt",
    "news",
    "web",
    "youtube",
)
#: The trend score's dimensions. ``trends/scoring.py`` validates the weights.
TREND_DIMENSIONS = ("momentum", "coverage", "audience", "freshness", "editorial_fit")
_DEFAULT_TREND_WEIGHTS = {
    "momentum": 30,
    "coverage": 25,
    "audience": 20,
    "freshness": 15,
    "editorial_fit": 10,
}


class EvidenceStatus(StrEnum):
    """How much credible literature a topic has, weakest first.

    Declaration order is the ranking (``EVIDENCE_RANK``). Derived from counts
    by ``scoring.evidence_status`` — never from a model's opinion.
    """

    NONE = "none"
    LIMITED = "limited"
    EMERGING = "emerging"
    MODERATE = "moderate"
    STRONG = "strong"


EVIDENCE_RANK: dict[EvidenceStatus, int] = {
    status: rank for rank, status in enumerate(EvidenceStatus)
}


class ResearchStage(StrEnum):
    """Where a run is. Written to ``research_runs.stage`` as it moves.

    Stages no longer used (``discovering_trends`` … ``shortlisting``) stay so
    older runs' rows still name a known stage.
    """

    QUEUED = "queued"
    LOADING_CONFIG = "loading_config"
    # trend search
    COLLECTING_SOURCES = "collecting_sources"
    NORMALIZING_ITEMS = "normalizing_items"
    CLUSTERING_TOPICS = "clustering_topics"
    CHECKING_HISTORY = "checking_history"
    ENRICHING_TOPICS = "enriching_topics"
    SCORING_TOPICS = "scoring_topics"
    SELECTING_TRENDS = "selecting_trends"
    # evidence check
    RESEARCHING_SCIENCE = "researching_science"
    DEEP_RESEARCH = "deep_research"
    SELECTING_TOPICS = "selecting_topics"
    BACKFILLING = "backfilling"
    # before the split (2026-10-12)
    DISCOVERING_TRENDS = "discovering_trends"
    EXPANDING_TOPICS = "expanding_topics"
    BUILDING_CANDIDATES = "building_candidates"
    CHECKING_NOVELTY = "checking_novelty"
    RESEARCHING_NEWS = "researching_news"
    SEARCHING_WEB = "searching_web"
    SHORTLISTING = "shortlisting"
    COMPLETED = "completed"
    FAILED = "failed"


# --- request ---------------------------------------------------------------


class ResearchParams(BaseModel):
    """What a trigger may ask for. Anything omitted falls back to settings.

    The same model for the desk button and for n8n's calls, so the two cannot
    drift into asking different things. Every field narrows or tightens what
    settings allow; none can switch the evidence check off.
    """

    target_article_count: int | None = Field(default=None, ge=1, le=10)
    categories: list[Category] | None = None
    trend_window_days: int | None = Field(default=None, ge=1, le=30)
    geo: str | None = Field(default=None, min_length=2, max_length=2)
    language: str | None = Field(default=None, min_length=2, max_length=2)
    #: A subset of the sources settings enable; it cannot enable another.
    enabled_sources: list[str] | None = None
    #: Lower than the settings ceiling only.
    max_requests_per_source: int | None = Field(default=None, ge=1, le=200)
    min_trend_score: float | None = Field(default=None, ge=0, le=100)
    min_data_coverage: float | None = Field(default=None, ge=0, le=1)
    #: Topics to look into on top of what the sweep finds.
    queries: list[str] | None = Field(default=None, max_length=10)

    @field_validator("enabled_sources")
    @classmethod
    def _known_sources(cls, value: list[str] | None) -> list[str] | None:
        return known_sources(value)

    @field_validator("queries")
    @classmethod
    def _clean_queries(cls, value: list[str] | None) -> list[str] | None:
        return clean_queries(value)


def known_sources(value: list[str] | None) -> list[str] | None:
    """Every name a known trend source, duplicates dropped. Shared with the API schema."""
    if value is None:
        return None
    unknown = sorted(set(value) - set(SOURCE_NAMES))
    if unknown:
        raise ValueError(f"unknown sources {unknown}; known: {', '.join(SOURCE_NAMES)}")
    return list(dict.fromkeys(value))


def clean_queries(value: list[str] | None) -> list[str] | None:
    """Whitespace collapsed, 2-80 characters each, duplicates dropped."""
    if value is None:
        return None
    cleaned = [" ".join(q.split()) for q in value]
    if any(not 2 <= len(q) <= 80 for q in cleaned):
        raise ValueError("each query must be 2-80 characters")
    return list(dict.fromkeys(cleaned))


class ResearchConfig(BaseModel):
    """Everything a run collects, scores and selects with, frozen when it is created.

    Stored on ``research_runs.config`` so a run can be explained later even
    after the settings changed. Fields added after the first runs have
    defaults, so a run queued before a deploy still loads.
    """

    target_article_count: int
    min_article_count: int
    max_article_count: int
    max_candidates: int
    trend_window_days: int
    scientific_window_days: int
    min_evidence_status: EvidenceStatus
    cluster_threshold: float
    duplicate_threshold: float
    novelty_lookback_days: int
    trends_anchor: str
    geo: str
    language: str
    categories: list[Category]
    authoritative_domains: list[str]
    # trend search
    weights: dict[str, int] = Field(default_factory=lambda: dict(_DEFAULT_TREND_WEIGHTS))
    enabled_sources: list[str] = Field(default_factory=lambda: list(SOURCE_NAMES))
    max_requests_per_source: int = 40
    enrich_pool_size: int = 15
    evidence_pool_size: int = 8
    min_trend_score: float = 0.0
    min_data_coverage: float = 0.6
    min_independent_sources: int = 2
    phrase_min_publishers: int = 1
    max_per_category: int = 2
    repeat_cooloff_days: int = 28
    expand_top: int = 0
    youtube_max_searches: int = 10
    collect_deadline_seconds: float = 300.0
    enrich_deadline_seconds: float = 240.0
    queries: list[str] = Field(default_factory=list)

    @field_validator("weights", mode="before")
    @classmethod
    def _trend_weights(cls, value: Any) -> Any:
        # A run queued before the split stored the old six float weights; it
        # is scored with today's defaults rather than refusing to load.
        if not isinstance(value, dict) or set(value) != set(TREND_DIMENSIONS):
            return dict(_DEFAULT_TREND_WEIGHTS)
        return value

    @classmethod
    def resolve(cls, settings: Settings, params: ResearchParams) -> ResearchConfig:
        low, high = settings.research_min_article_count, settings.research_max_article_count
        target = params.target_article_count or settings.research_target_article_count
        target = max(low, min(high, target))
        allowed = [name for name in settings.research_enabled_sources if name in SOURCE_NAMES]
        sources = (
            [name for name in params.enabled_sources if name in allowed]
            if params.enabled_sources is not None
            else allowed
        )
        ceiling = settings.research_max_requests_per_source
        return cls(
            target_article_count=target,
            # A request for fewer than the configured minimum lowers the
            # minimum with it; asking for 2 and being told "under target" at 3
            # would be nonsense.
            min_article_count=min(low, target),
            max_article_count=high,
            max_candidates=settings.research_max_candidates,
            trend_window_days=params.trend_window_days or settings.research_trend_window_days,
            scientific_window_days=settings.research_scientific_window_days,
            weights={
                "momentum": settings.research_weight_momentum,
                "coverage": settings.research_weight_coverage,
                "audience": settings.research_weight_audience,
                "freshness": settings.research_weight_freshness,
                "editorial_fit": settings.research_weight_editorial_fit,
            },
            min_evidence_status=EvidenceStatus(settings.research_min_evidence_status),
            cluster_threshold=settings.research_cluster_threshold,
            duplicate_threshold=settings.research_duplicate_threshold,
            novelty_lookback_days=settings.research_novelty_lookback_days,
            trends_anchor=settings.research_trends_anchor,
            expand_top=settings.research_expand_top,
            geo=(params.geo or settings.research_geo).upper(),
            language=(params.language or settings.research_language).lower(),
            categories=params.categories or list(Category),
            authoritative_domains=[d.lower() for d in settings.research_authoritative_domains],
            enabled_sources=sources,
            max_requests_per_source=min(ceiling, params.max_requests_per_source or ceiling),
            enrich_pool_size=settings.research_enrich_pool_size,
            evidence_pool_size=max(settings.research_evidence_pool_size, target),
            # A trigger may only raise the bars, never lower them below settings.
            min_trend_score=max(
                settings.research_min_trend_score, params.min_trend_score or 0.0
            ),
            min_data_coverage=max(
                settings.research_min_data_coverage, params.min_data_coverage or 0.0
            ),
            min_independent_sources=settings.research_min_independent_sources,
            phrase_min_publishers=settings.research_phrase_min_publishers,
            max_per_category=settings.research_max_per_category,
            repeat_cooloff_days=settings.research_repeat_cooloff_days,
            youtube_max_searches=settings.youtube_max_searches_per_run,
            collect_deadline_seconds=settings.research_collect_deadline_seconds,
            enrich_deadline_seconds=settings.research_enrich_deadline_seconds,
            queries=params.queries or [],
        )


# --- provider results ------------------------------------------------------


class RawTrend(BaseModel):
    """A query to consider: a rising search, a headline phrase, or a manual query."""

    query: str
    seed: str
    category: Category
    #: The trend source's name, ``headlines`` or ``manual``.
    source: str
    #: Trends' "rising" value: percent growth against the preceding period.
    rising_percent: int | None = None
    #: Trends labels growth above 5000% "Breakout".
    is_breakout: bool = False
    #: 0 for a seed's own related queries, 1 for an expansion of one of them.
    depth: int = 0


class TrendSignal(BaseModel):
    source: str
    #: Mean interest over the trend window, on the anchored request's scale.
    current_interest: float | None = None
    previous_interest: float | None = None
    #: The anchor term's current mean in the same request, for the level score.
    anchor_interest: float | None = None
    growth_percent: float | None = None
    rising_percent: int | None = None
    is_breakout: bool = False
    #: Which measurement ``growth_percent`` came from.
    growth_source: str | None = None
    related_queries: list[str] = Field(default_factory=list)


class WebResult(BaseModel):
    title: str
    url: str
    snippet: str = ""


class WebSignal(BaseModel):
    source: str
    results_found: int
    relevant_results: int
    distinct_domains: int
    authoritative_results: int
    top_results: list[WebResult] = Field(default_factory=list)


class NewsArticle(BaseModel):
    title: str
    url: str
    source: str | None = None
    published_at: datetime | None = None


class NewsSignal(BaseModel):
    source: str
    #: Everything the provider matched across both windows.
    articles_found: int
    recent: int
    #: ``None`` when the provider's page was full before reaching the earlier
    #: window, so the previous count is unknown rather than zero.
    previous: int | None
    top_articles: list[NewsArticle] = Field(default_factory=list)


class EvidenceCounts(BaseModel):
    """Literature sizing for one query. Counts, not papers."""

    source: str
    query: str
    total: int
    recent: int
    reviews_or_meta: int
    rcts: int


class PaperRef(BaseModel):
    pmid: str | None = None
    title: str
    year: int | None = None
    study_type: str


class DeepEvidence(BaseModel):
    """What the article pipeline itself would retrieve for this topic.

    Built from the pipeline's own extraction call and ``TemplateQueryStrategy``,
    so a topic that would die at ``UnanchoredQuery`` dies here instead, before
    a reviewer is asked to spend a run on it.
    """

    product: str
    ingredients: list[str]
    claims: list[str]
    queries: list[str]
    counts: EvidenceCounts
    top_papers: list[PaperRef] = Field(default_factory=list)


class ScienceSignal(BaseModel):
    """Stage 1 counts on the topic's phrase, and deep research if it got that far."""

    primary: EvidenceCounts | None = None
    #: The other provider's total for the same question, as a cross-check.
    cross_check: EvidenceCounts | None = None
    deep: DeepEvidence | None = None
    failures: list[str] = Field(default_factory=list)

    @property
    def best(self) -> EvidenceCounts | None:
        """Deep counts when present — they are the article's own query."""
        return self.deep.counts if self.deep is not None else self.primary


class NoveltySignal(BaseModel):
    max_similarity: float
    nearest: str | None = None


# --- candidates and state --------------------------------------------------


class CandidateTopic(BaseModel):
    topic_id: str
    canonical_topic: str
    category: Category
    #: The substance, food or activity the topic is about ("creatine").
    subject: str
    #: What it is for ("sleep"). Empty for a bare-subject topic.
    outcome: str = ""
    reader_question: str | None = None
    queries: list[str]
    seeds: list[str] = Field(default_factory=list)
    #: The categories of the seeds that found it, before triage filed it.
    seed_categories: list[Category] = Field(default_factory=list)
    #: The persistent identity across runs (``research_topics.id``).
    topic_ref_id: str | None = None
    #: Indexes into ``ResearchState.items``: what this topic rests on. Run-local.
    item_ids: list[int] = Field(default_factory=list)
    trend: TrendSignal | None = None
    web: WebSignal | None = None
    news: NewsSignal | None = None
    science: ScienceSignal = Field(default_factory=ScienceSignal)
    novelty: NoveltySignal | None = None
    metrics: TopicMetrics | None = None
    scores: TrendScores = Field(default_factory=TrendScores)
    trend_status: TrendStatus | None = None
    key_questions: list[str] = Field(default_factory=list)
    supporting: list[SupportingSource] = Field(default_factory=list)
    evidence_status: EvidenceStatus | None = None
    status: ResearchCandidateStatus = ResearchCandidateStatus.CANDIDATE
    discard_reason: str | None = None
    rank: int | None = None
    in_enrich_pool: bool = False
    in_evidence_pool: bool = False

    def discard(self, reason: str) -> None:
        self.status = ResearchCandidateStatus.DISCARDED
        self.discard_reason = reason

    @property
    def live(self) -> bool:
        return self.status is not ResearchCandidateStatus.DISCARDED

    @property
    def search_terms(self) -> tuple[str, ...]:
        """The topic as search terms: the subject, then the outcome if any."""
        return (self.subject, self.outcome) if self.outcome else (self.subject,)

    def signals_json(self) -> dict[str, Any]:
        """What is stored in ``research_candidates.signals``.

        A discarded candidate keeps its counts and loses its examples (web
        results, news articles, top papers, supporting sources): the desk shows
        it only as a topic and a reason, and the examples were about two thirds
        of every row.
        """
        slim = self.status is ResearchCandidateStatus.DISCARDED
        drop_examples = {"top_results", "top_articles", "top_papers"} if slim else set()
        return {
            "subject": self.subject,
            "outcome": self.outcome,
            "reader_question": self.reader_question,
            "seeds": self.seeds,
            "trend": self.trend.model_dump(mode="json") if self.trend else None,
            "web": (
                self.web.model_dump(mode="json", exclude=drop_examples) if self.web else None
            ),
            "news": (
                self.news.model_dump(mode="json", exclude=drop_examples) if self.news else None
            ),
            "science": self.science.model_dump(
                mode="json",
                exclude={"deep": {"top_papers"}} if slim and self.science.deep else None,
            ),
            "novelty": self.novelty.model_dump(mode="json") if self.novelty else None,
            "metrics": self.metrics.model_dump(mode="json") if self.metrics else None,
            "trend_status": self.trend_status.value if self.trend_status else None,
            "key_questions": self.key_questions,
            "supporting_sources": (
                [] if slim else [s.model_dump(mode="json") for s in self.supporting]
            ),
            "in_enrich_pool": self.in_enrich_pool,
            "in_evidence_pool": self.in_evidence_pool,
        }


class ProviderStatus(BaseModel):
    """One source's run: ``ok`` | ``partial`` (some calls failed) | ``failed``
    | ``unavailable`` (not configured) | ``skipped`` (not requested, or its
    budget or time ran out before it was asked)."""

    status: str
    detail: str | None = None
    calls: int = 0
    failures: int = 0
    retries: int = 0
    items: int = 0


class ResearchState(BaseModel):
    """Everything the graph threads. One key in LangGraph's state, replaced
    wholesale by every node — the same shape as ``pipeline/graph.py``.

    ``items`` and ``stories`` are run-local: they are what the candidates'
    numbers were counted from, and only what a candidate keeps as supporting
    sources is persisted.
    """

    run_id: str
    label: str
    mode: ResearchRunMode
    config: ResearchConfig
    stage: ResearchStage = ResearchStage.QUEUED
    today: date
    window_start: datetime | None = None
    window_end: datetime | None = None
    items: list[SourceItem] = Field(default_factory=list)
    stories: list[Story] = Field(default_factory=list)
    story_of: dict[int, int] = Field(default_factory=dict)
    candidates: list[CandidateTopic] = Field(default_factory=list)
    provider_status: dict[str, ProviderStatus] = Field(default_factory=dict)
    stage_log: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    #: Run-level observability, persisted in ``research_runs.summary.counts``.
    counts: dict[str, Any] = Field(default_factory=dict)
    #: Completed trend runs before this one, within history retention.
    history_runs: int = 0
    #: The trend agent's output, frozen when its phase ends.
    trend_summary: dict[str, Any] | None = None
    backfill_round: int = 0
    #: Set by a node that ends the run; the router sends the graph to END.
    failed: str | None = None

    def live(self) -> list[CandidateTopic]:
        return [candidate for candidate in self.candidates if candidate.live]

    def with_status(self, status: ResearchCandidateStatus) -> list[CandidateTopic]:
        return [candidate for candidate in self.candidates if candidate.status is status]

    def provider(self, name: str) -> ProviderStatus:
        return self.provider_status.setdefault(name, ProviderStatus(status="ok"))


class PastTopic(BaseModel):
    """Something already written, promoted or turned down, for the novelty check.

    ``kind`` is ``article`` (a pipeline run's topic), ``promoted`` or
    ``dismissed`` (research or discovery candidates a reviewer decided on).
    """

    text: str
    kind: str
    decided_at: datetime | None = None
