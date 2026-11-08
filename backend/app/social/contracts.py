"""Shapes for social listening, product discovery and their collection runs.

The status vocabulary is the one rule everything else leans on: a source that
could not be asked (NOT_CONFIGURED, UNAVAILABLE), refused (RATE_LIMITED) or
broke (ERROR) is never read as a source that answered with nothing (EMPTY).
Only SUCCESS and EMPTY count as coverage.

``SocialListeningResult`` is served snake_case, like ``TrendSearchResult``: it
is the module's output schema, not a console record.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class CollectionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    EMPTY = "EMPTY"
    UNAVAILABLE = "UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    ERROR = "ERROR"
    NOT_CONFIGURED = "NOT_CONFIGURED"


#: Statuses that mean "we asked, and the answer is real".
COVERED = frozenset({CollectionStatus.SUCCESS, CollectionStatus.EMPTY})
#: When calls failed differently, the run reports the most telling one.
_FAILURE_ORDER = (
    CollectionStatus.RATE_LIMITED,
    CollectionStatus.UNAVAILABLE,
    CollectionStatus.ERROR,
)


class CollectionKind(StrEnum):
    SOCIAL_DAILY = "social_daily"
    SOCIAL_WEEKLY = "social_weekly"
    PRODUCT_DISCOVERY = "product_discovery"


class CollectionError(RuntimeError):
    """A source call that did not answer, with the status it maps to."""

    def __init__(self, status: CollectionStatus, detail: str) -> None:
        super().__init__(detail)
        self.status = status


class SourceLog:
    """Every call one source made in a run, folded into one status.

    SUCCESS only when every call answered; EMPTY when they all answered with
    nothing; otherwise the most telling failure, with the failures listed.
    A source never asked keeps the status it was created with.
    """

    def __init__(
        self, status: CollectionStatus | None = None, detail: str | None = None
    ) -> None:
        self._fixed = status
        self.detail = detail
        self.calls = 0
        self.items = 0
        self.failures: list[dict[str, str]] = []

    def ok(self, items: int) -> None:
        self.calls += 1
        self.items += items

    def failed(self, error: CollectionError, what: str) -> None:
        self.calls += 1
        self.failures.append({"call": what, "status": error.status.value, "detail": str(error)})

    @property
    def stopped(self) -> bool:
        """A permanent failure was seen: ask nothing more this run."""
        return any(
            f["status"] in (CollectionStatus.RATE_LIMITED, CollectionStatus.UNAVAILABLE)
            for f in self.failures
        )

    @property
    def status(self) -> CollectionStatus:
        if self.calls == 0:
            return self._fixed or CollectionStatus.NOT_CONFIGURED
        seen = {f["status"] for f in self.failures}
        for status in _FAILURE_ORDER:
            if status.value in seen:
                return status
        return CollectionStatus.SUCCESS if self.items else CollectionStatus.EMPTY

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            # Why it was never asked; a source that was asked speaks through its calls.
            "detail": None if self.calls else self.detail,
            "calls": self.calls,
            "items": self.items,
            "failures": self.failures[:20],
        }


# --- the weekly result -------------------------------------------------------


class WindowMetrics(BaseModel):
    """One 7-day window. Every count is None when the window was not covered."""

    start: datetime
    end: datetime
    covered: bool
    covered_days: int
    uploads: int | None = None
    creators: int | None = None
    #: A search in this window hit its page cap: counts are a floor.
    truncated: bool = False


class TopicMetrics(BaseModel):
    """Deterministic numbers for one topic (``social/analytics.py``)."""

    version: str
    windows: list[WindowMetrics]
    #: Growth: (current + 5) / (previous + 5), None unless both covered.
    upload_growth: float | None = None
    creator_growth: float | None = None
    #: Change in upload growth between the last two window pairs; needs three.
    acceleration: float | None = None
    #: Popularity (a level, not growth): median views per day since upload.
    median_views_per_day: float | None = None
    #: Views gained per day between a post's two latest readings, summed.
    view_velocity: float | None = None
    velocity_posts: int = 0
    #: Median (likes + comments) / views over posts reporting all three.
    engagement_rate: float | None = None
    engagement_posts: int = 0
    posts: int = 0
    independent_creators: int = 0
    platforms: list[str] = Field(default_factory=list)
    platforms_collected: list[str] = Field(default_factory=list)


class Momentum(BaseModel):
    """An assessment of the metrics, kept apart from them."""

    status: str  # rising | steady | declining | insufficient_history
    reason: str


class EvidenceAssessment(BaseModel):
    """Literature counts for the topic. Says nothing about efficacy."""

    status: str  # strong | moderate | emerging | limited | none | not_checked
    source: str | None = None
    counts: dict[str, int] | None = None
    reason: str | None = None
    #: The search counted: the article pipeline's own query for the topic's question.
    query: str | None = None


class SupportingPost(BaseModel):
    url: str
    title: str
    creator: str | None = None
    published_at: datetime | None = None
    views: int | None = None


class SocialTopicResult(BaseModel):
    candidate_id: str
    topic_id: str
    label: str
    #: What an article would answer. None until written; the label stands in.
    question: str | None = None
    category: str
    rank: int
    status: str
    metrics: TopicMetrics
    momentum: Momentum
    evidence: EvidenceAssessment
    supporting_posts: list[SupportingPost] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class SocialListeningResult(BaseModel):
    run_id: str
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    collection: dict[str, Any] = Field(default_factory=dict)
    topics: list[SocialTopicResult] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
