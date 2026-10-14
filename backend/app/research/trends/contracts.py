"""Shapes the Trend Search Agent collects, measures, scores and returns.

Three layers, each a record of what was actually observed:

* **Source items** — one article, feed entry, video, post or rising search, as
  a source returned it, normalised (``SourceItem``). Never invented: a source
  that could not answer raises ``ProviderUnavailable`` instead of returning an
  empty list, and nothing here is filled in to look complete.
* **Topic metrics** — what was measured about one candidate topic across those
  items (``TopicMetrics``). ``None`` means "not measured", never zero. This is
  the whole input to the score, stored with it, so a score can be recomputed
  from the row it sits on.
* **The result** — the ranked shortlist with its evidence of trending
  (``TrendSearchResult``). It carries **no scientific evidence score**: the
  literature check is a separate downstream step, and a topic's
  ``scientific_assessment`` here is always ``not_assessed``.

Enums in this module have no docstrings on purpose: any of them may end up in
a model's generation grammar, where a docstring is read as instructions (the
``Category`` trap, see ``research/categories.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.research.categories import Category

#: Bump when anything in ``trends/scoring.py`` changes what a score means, so
#: stored scores from different formulas are never compared as if they were one.
TREND_SCORING_VERSION = "trend-v1"


class SourceType(StrEnum):
    SEARCH = "search"
    NEWS = "news"
    FEED = "feed"
    WEB = "web"
    VIDEO = "video"


#: Source types whose items count as independent *reporting*. A video channel
#: is a different platform, not a different outlet, and a web
#: page is not dated coverage: they count toward source diversity, never
#: toward the independent-publisher count.
REPORTING_TYPES = frozenset({SourceType.NEWS, SourceType.FEED})


class Capability(StrEnum):
    #: Can sweep without a topic: feeds, seed queries.
    DISCOVER = "discover"
    #: Can search for one topic.
    SEARCH = "search"
    #: Daily counts over a long window, for a baseline (GDELT timelines).
    VOLUME = "volume"
    #: Google Trends' relative interest series, compared against an anchor.
    INTEREST = "interest"


class TrendStatus(StrEnum):
    EMERGING = "emerging"
    ACCELERATING = "accelerating"
    STEADY = "steady"
    DECLINING = "declining"
    PRELIMINARY = "preliminary"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# --- what a source is asked, and what it returns ---------------------------


@dataclass(frozen=True, slots=True)
class SourceQuery:
    """One request to one source.

    ``terms`` are phrases — ``("vitamin d", "sleep")`` — so each source can
    quote a multi-word subject in its own syntax. Empty for a listing sweep,
    where ``target`` names what is swept (a feed URL).
    """

    since: datetime
    until: datetime
    geo: str = "US"
    language: str = "en"
    max_results: int = 50
    terms: tuple[str, ...] = ()
    target: str | None = None
    seed: str | None = None
    category: Category | None = None

    @property
    def text(self) -> str:
        return " ".join(self.terms) if self.terms else (self.target or "")


class SourceItem(BaseModel):
    """One record a source returned, normalised. Built by ``documents.make_item``.

    ``url`` is ``None`` only for a search query (a Google Trends rising query
    is a phrase, not a page). Publication time and collection time are
    separate fields because they answer different questions: one is when the
    world said it, the other is when we saw it.
    """

    source_name: str
    source_type: SourceType
    title: str = Field(min_length=1, max_length=400)
    url: str | None = None
    canonical_url: str | None = None
    publisher: str | None = None
    publisher_domain: str | None = None
    published_at: datetime | None = None
    collected_at: datetime
    #: A short description or snippet, truncated. Never a full article body.
    description: str | None = Field(default=None, max_length=600)
    #: A news byline when the source gives one.
    author: str | None = None
    #: Platform engagement as reported (views, likes, comments, score).
    engagement: dict[str, float] = Field(default_factory=dict)
    #: Source-specific measurements (Trends' rising percent).
    metrics: dict[str, float] = Field(default_factory=dict)
    #: Small provenance extras (language, source country, feed name).
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    #: The query that produced it, and the seed and category it was asked under.
    query: str | None = None
    seed: str | None = None
    category: Category | None = None

    @field_validator("url", "canonical_url")
    @classmethod
    def _http_only(cls, value: str | None) -> str | None:
        if value is not None and not value.lower().startswith(("http://", "https://")):
            raise ValueError("only http(s) URLs are kept")
        return value

    @field_validator("published_at", "collected_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value


@dataclass(slots=True)
class SourceBatch:
    """What one request returned.

    ``total`` is what the source says matched, which may exceed
    ``len(items)``; ``truncated`` means a page filled up, so anything counted
    from these items is a floor — and a previous-window count from them is
    unknown, not zero.
    """

    items: list[SourceItem] = field(default_factory=list)
    total: int | None = None
    truncated: bool = False


class DailySeries(BaseModel):
    """Daily counts for one query, oldest first (GDELT ``timelinevolraw``)."""

    source: str
    query: str
    dates: list[date]
    values: list[float]


class Story(BaseModel):
    """Items that are one piece of reporting: copies, syndications, re-posts.

    Built by ``documents.group_stories``. Counting stories rather than items is
    what stops thirty syndicated copies of one wire story reading as thirty
    outlets.
    """

    story_id: int
    item_ids: list[int]
    #: The earliest-published (else first-collected) item.
    representative: int
    title_key: str
    publisher_domains: list[str]
    source_types: list[SourceType]
    published_at: datetime | None = None


class SupportingSource(BaseModel):
    """A collected item shown as evidence that a topic is being discussed."""

    source_name: str
    source_type: SourceType
    title: str
    url: str
    publisher: str | None = None
    published_at: datetime | None = None


# --- what was measured about one topic -------------------------------------


class SearchMetrics(BaseModel):
    """Google Trends. Relative indices (0-100 per request), never search counts."""

    source: str
    current_interest: float | None = None
    previous_interest: float | None = None
    anchor_interest: float | None = None
    rising_percent: int | None = None
    is_breakout: bool = False


class WindowCounts(BaseModel):
    """Items in the recent window and the one before it, from one source.

    ``previous`` is ``None`` when the source's page filled before reaching the
    earlier window: unknown, which must never read as zero (a surge).
    """

    source: str
    recent: int
    previous: int | None


class VolumeWindows(BaseModel):
    """GDELT article volume: the recent window against earlier windows."""

    source: str
    recent: float
    #: Mean per window over ``baseline_windows`` earlier windows.
    baseline: float | None
    baseline_windows: int


class VideoMetrics(BaseModel):
    """YouTube: upload counts per window, and views per day of recent uploads.

    Lifetime views are never used: a five-year-old video's total says nothing
    about this week.
    """

    source: str
    videos: int
    recent_uploads: int
    previous_uploads: int | None
    median_views_per_day: float | None


class HistoryMetrics(BaseModel):
    """What earlier runs observed about this topic."""

    previous_observations: int
    #: Mean independent stories per previous observation.
    previous_stories: float | None = None
    first_seen_at: datetime | None = None
    last_observed_at: datetime | None = None
    #: Earlier runs in a row (most recent first) that observed it.
    consecutive_runs: int = 0
    #: Completed trend runs of any topic before this one, within retention.
    system_runs: int = 0
    last_proposed_at: datetime | None = None
    stories_when_proposed: int | None = None


class TopicMetrics(BaseModel):
    """Everything the score of one topic is computed from.

    Self-contained on purpose: ``trends/scoring.py::score_from_metrics`` takes
    this and the weights and nothing else, so a stored score can be checked
    against the stored metrics.
    """

    window_days: int
    #: When the run measured it (the end of its collection window).
    observed_at: datetime
    #: Sub-signal names whose source was enabled, configured and applicable
    #: to this topic. A sub-signal outside this list is neither measured nor
    #: missing: it does not count against data coverage.
    applicable: list[str] = Field(default_factory=list)
    # coverage
    stories: int = 0
    independent_publishers: int = 0
    syndicated_copies: int = 0
    source_types: list[str] = Field(default_factory=list)
    source_types_available: list[str] = Field(default_factory=list)
    #: False when no news-like source answered for this topic: coverage is
    #: then unknown, and zero publishers would be a lie.
    coverage_measured: bool = False
    # momentum and audience inputs
    search: SearchMetrics | None = None
    news_volume: VolumeWindows | None = None
    news_counts: list[WindowCounts] = Field(default_factory=list)
    video: VideoMetrics | None = None
    history: HistoryMetrics | None = None
    questions: int = 0
    # freshness
    dated_items: int = 0
    median_age_days: float | None = None
    novelty_similarity: float | None = None
    duplicate_threshold: float = 0.92
    # editorial fit
    on_topic_share: float | None = None
    specific_subject: bool = False
    has_outcome: bool = False


class DimensionScore(BaseModel):
    score: float | None = None
    #: Measured sub-signal weight over applicable sub-signal weight.
    measured_fraction: float = 0.0
    #: Every applicable sub-signal's 0-100 value; ``None`` = not measured.
    signals: dict[str, float | None] = Field(default_factory=dict)


class TrendScores(BaseModel):
    """A topic's trend score with everything it rests on.

    ``components``/``unavailable``/``weights_used``/``overall`` keep the shape
    of the scores the desk already renders, so old and new rows read the same
    way; ``version`` tells them apart.
    """

    version: str = TREND_SCORING_VERSION
    #: Dimension -> 0-100, measured dimensions only.
    components: dict[str, float] = Field(default_factory=dict)
    #: Dimensions with nothing measured: left out, never zeroed.
    unavailable: list[str] = Field(default_factory=list)
    #: Base weights (integers summing to 100).
    weights: dict[str, int] = Field(default_factory=dict)
    #: Renormalised over the measured dimensions.
    weights_used: dict[str, float] = Field(default_factory=dict)
    dimensions: dict[str, DimensionScore] = Field(default_factory=dict)
    #: 0-1: how much of the full score could be measured.
    data_coverage: float = 0.0
    overall: float | None = None
    confidence: Confidence | None = None
    #: Why ``overall`` is None, when it is.
    insufficient: str | None = None


# --- the result ------------------------------------------------------------


class CollectionWindow(BaseModel):
    start: datetime
    end: datetime


class ResultSources(BaseModel):
    successful: list[str] = Field(default_factory=list)
    partial: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


class Downstream(BaseModel):
    """What happened after the trend agent: a status, never a score."""

    evidence_gate: Literal["not_checked", "passed", "failed"] = "not_checked"
    reason: str | None = None


class ResultTopic(BaseModel):
    #: The persistent topic id (``research_topics``), stable across runs.
    topic_id: str | None
    #: This run's candidate row (``research_candidates``), for promoting it.
    candidate_id: str
    topic: str
    category: Category
    subject: str
    outcome: str
    rank: int
    summary: str
    trend_score: float
    score_breakdown: dict[str, float | None]
    confidence: Confidence | None
    data_coverage: float
    trend_status: TrendStatus
    key_questions: list[str]
    supporting_sources: list[SupportingSource]
    selection_reason: str
    limitations: list[str]
    status: str
    #: In the trend agent's own shortlist (the top ``target`` after diversity
    #: rules). The frozen result keeps every ranked topic, so one the evidence
    #: check later backfills still has its trend explanation.
    trend_shortlisted: bool = False
    scientific_assessment: Literal["not_assessed"] = "not_assessed"
    downstream: Downstream = Field(default_factory=Downstream)


ResultStatus = Literal["queued", "running", "completed", "partial", "failed"]


class TrendSearchResult(BaseModel):
    """The Trend Search Agent's output for one run, in snake_case.

    Frozen when the trend phase ends (``research_runs.summary``); the read
    endpoint overlays each topic's current status and evidence-gate outcome.
    """

    run_id: str
    label: str
    status: ResultStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    collection_window: CollectionWindow
    scoring_version: str = TREND_SCORING_VERSION
    sources: ResultSources = Field(default_factory=ResultSources)
    topics: list[ResultTopic] = Field(default_factory=list)
    #: Run-level observability: records per source, duplicates removed per
    #: layer, candidates, enriched, ranked, durations.
    counts: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
