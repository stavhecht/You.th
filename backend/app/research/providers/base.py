"""One protocol per kind of signal. The graph depends on these, never on a library.

Swapping Google Trends for another trend source, DuckDuckGo for a paid search
API, or News API for GDELT is a new class here and a line in
``research/factory.py`` — nothing in ``graph.py`` changes.

**Every provider raises ``ProviderUnavailable`` rather than returning an empty
result when it could not answer.** An empty list means "asked, and there was
nothing"; the graph records a failure differently, and never scores it as zero.
Same principle as ``retrieval/throttle.py``: a search that could not run must
never look like one that found nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from app.research.contracts import EvidenceCounts, NewsArticle, WebResult


class ProviderUnavailable(RuntimeError):
    """The provider could not answer: throttled, down, unconfigured, or broken.

    ``permanent`` marks a failure that asking again cannot fix within this run
    — a rejected key (401/403), a spent daily quota. The trend agent stops
    calling such a source for the rest of the run instead of burning its time
    budget on identical refusals.
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class RelatedQuery:
    query: str
    #: Rising: percent growth. Top: relative volume (0-100).
    value: int


@dataclass(frozen=True, slots=True)
class RelatedQueries:
    rising: list[RelatedQuery] = field(default_factory=list)
    top: list[RelatedQuery] = field(default_factory=list)


class TrendProvider(Protocol):
    name: str

    async def related_queries(self, seed: str, *, window_days: int, geo: str) -> RelatedQueries:
        """Queries rising around ``seed`` over the window."""
        ...

    async def interest_over_time(
        self, queries: list[str], *, days: int, geo: str
    ) -> dict[str, list[float]]:
        """Daily interest per query, oldest first, all on one request's scale.

        At most five queries per call (Google Trends' limit); the caller puts
        an anchor term in every batch so levels compare across batches.
        """
        ...


class WebSearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, region: str, max_results: int) -> list[WebResult]: ...


@dataclass(frozen=True, slots=True)
class NewsResults:
    source: str
    #: What the provider says matched, which may exceed ``len(articles)``.
    total: int
    articles: list[NewsArticle]


class NewsProvider(Protocol):
    name: str

    async def search(
        self, terms: list[str], *, since: date, until: date, language: str
    ) -> NewsResults:
        """Articles published in [since, until] mentioning every term, newest first.

        ``terms`` are phrases — ``["vitamin d", "sleep"]`` — so each provider can
        quote a multi-word subject in its own syntax.
        """
        ...


@dataclass(frozen=True, slots=True)
class EvidenceQuery:
    """A topic as a literature question: the subject, and what it is for."""

    subject: str
    outcome: str
    #: Publications on or after this date count as recent.
    recent_since: date


class ScientificEvidenceProvider(Protocol):
    name: str

    async def profile(self, query: EvidenceQuery) -> EvidenceCounts:
        """Total, recent, review/meta-analysis and RCT counts for the question."""
        ...
