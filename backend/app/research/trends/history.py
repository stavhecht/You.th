"""Topics across runs: persistent identity, what earlier runs saw, and repeats.

A run's candidates are new rows every week; ``research_topics`` is what says
"this week's *Creatine for women* is the topic we saw three weeks ago". The key
is the topic's subject and outcome, normalised (``topic_key``); a topic whose
triage wording drifted ("creatine women" / "women creatine supplementation")
is matched to a known topic of the same category by embedding instead, at the
novelty duplicate threshold.

``research_topic_observations`` then holds one row per topic per run — the
trend history: stories, outlets, score, status, whether it was proposed. From
those the graph learns, per topic:

* how many runs saw it before, and how many in a row (``newness``);
* its mean independent stories then (the history momentum signal);
* when it was last proposed, and with how much coverage (repeat suppression).

Counts are derived from the observation rows every time, never kept as
incremented counters (the discovery ledger's rule): a counter drifts the first
time a write is retried, a ``COUNT`` cannot.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from pydantic import BaseModel

from app.research.categories import Category
from app.research.normalize import query_key
from app.research.trends.contracts import HistoryMetrics, TopicMetrics, TrendScores

#: Earlier observations read for a topic's history.
HISTORY_DEPTH = 8
#: How far back trend status and newness read observations. Mirrored by
#: ``config._TREND_STATUS_LOOKBACK_DAYS``, which keeps retention above it.
STATUS_LOOKBACK_DAYS = 56
#: Momentum at or above this lets a recently proposed topic be proposed again.
REPEAT_OVERRIDE_MOMENTUM = 65.0


def topic_key(subject: str, outcome: str) -> str:
    """``creatine|women`` — word order, case, plurals and filler ignored."""
    return f"{query_key(subject) or subject.strip().lower()}|{query_key(outcome)}"


class KnownTopic(BaseModel):
    id: str
    topic_key: str
    label: str
    category: Category
    last_seen_at: datetime


class TopicRef(BaseModel):
    topic_key: str
    label: str
    subject: str
    outcome: str
    category: Category


class Observation(BaseModel):
    """One earlier run's view of a topic: what ``HistoryMetrics`` is built from."""

    run_id: str
    observed_at: datetime
    stories: int
    proposed: bool


class ResolvedTopic(BaseModel):
    id: str
    first_seen_at: datetime
    observations: list[Observation]


class TopicStore(Protocol):
    async def known(self, since: datetime) -> list[KnownTopic]:
        """Topics seen since ``since``, for matching drifted wording."""
        ...

    async def resolve(
        self, refs: list[TopicRef], *, now: datetime, exclude_run: str
    ) -> dict[str, ResolvedTopic]:
        """Upsert each topic by key and return its id and earlier observations.

        Idempotent: resolving the same keys twice yields the same ids. Must
        commit before returning — the next checkpoint writes candidates that
        reference these ids.
        """
        ...

    async def recent_runs(self, since: datetime, *, exclude_run: str) -> list[str]:
        """Completed trend runs since ``since``, newest first."""
        ...


class InMemoryTopicStore:
    """For tests and the dry-run script; ``observe`` stands in for ``finish``."""

    def __init__(self) -> None:
        self.topics: dict[str, ResolvedTopic] = {}
        self.labels: dict[str, KnownTopic] = {}
        self.runs: list[tuple[str, datetime]] = []
        self._next = 0

    async def known(self, since: datetime) -> list[KnownTopic]:
        return [topic for topic in self.labels.values() if topic.last_seen_at >= since]

    async def resolve(
        self, refs: list[TopicRef], *, now: datetime, exclude_run: str
    ) -> dict[str, ResolvedTopic]:
        resolved: dict[str, ResolvedTopic] = {}
        for ref in refs:
            topic = self.topics.get(ref.topic_key)
            if topic is None:
                self._next += 1
                topic = ResolvedTopic(
                    id=f"00000000-0000-4000-8000-{self._next:012d}",
                    first_seen_at=now,
                    observations=[],
                )
                self.topics[ref.topic_key] = topic
            self.labels[ref.topic_key] = KnownTopic(
                id=topic.id,
                topic_key=ref.topic_key,
                label=ref.label,
                category=ref.category,
                last_seen_at=now,
            )
            resolved[ref.topic_key] = ResolvedTopic(
                id=topic.id,
                first_seen_at=topic.first_seen_at,
                observations=[o for o in topic.observations if o.run_id != exclude_run],
            )
        return resolved

    async def recent_runs(self, since: datetime, *, exclude_run: str) -> list[str]:
        return [
            run
            for run, at in sorted(self.runs, key=lambda r: r[1], reverse=True)
            if at >= since and run != exclude_run
        ]


def history_metrics(
    topic: ResolvedTopic, recent_runs: list[str], *, now: datetime
) -> HistoryMetrics:
    """``HistoryMetrics`` for one topic from its earlier observations.

    ``recent_runs`` (newest first) is every completed trend run in the
    lookback; ``consecutive_runs`` counts how many of the newest ones in a row
    observed this topic.
    """
    cutoff = now - timedelta(days=STATUS_LOOKBACK_DAYS)
    observations = sorted(topic.observations, key=lambda o: o.observed_at, reverse=True)
    window = [o for o in observations if o.observed_at >= cutoff][:HISTORY_DEPTH]
    seen_runs = {o.run_id for o in observations}
    consecutive = 0
    for run in recent_runs:
        if run not in seen_runs:
            break
        consecutive += 1
    proposed = next((o for o in observations if o.proposed), None)
    return HistoryMetrics(
        previous_observations=len(observations),
        previous_stories=(
            round(sum(o.stories for o in window) / len(window), 2) if window else None
        ),
        first_seen_at=topic.first_seen_at,
        last_observed_at=observations[0].observed_at if observations else None,
        consecutive_runs=consecutive,
        system_runs=len(recent_runs),
        last_proposed_at=proposed.observed_at if proposed else None,
        stories_when_proposed=proposed.stories if proposed else None,
    )


def repeat_reason(
    metrics: TopicMetrics, scores: TrendScores, *, now: datetime, cooloff_days: int
) -> str | None:
    """Why a recently proposed topic is not proposed again, or None.

    Proposed within the cooloff and not meaningfully changed since: its
    independent stories have not doubled and its momentum is not high. Time
    alone would let a slow-burn topic nag every week; a real new wave lifts
    the suppression.
    """
    history = metrics.history
    if history is None or history.last_proposed_at is None:
        return None
    if now - history.last_proposed_at > timedelta(days=cooloff_days):
        return None
    then = history.stories_when_proposed or 0
    momentum = scores.components.get("momentum")
    if metrics.stories >= 2 * max(1, then):
        return None
    if momentum is not None and momentum >= REPEAT_OVERRIDE_MOMENTUM:
        return None
    return (
        f"proposed on {history.last_proposed_at.date()} and not meaningfully changed since "
        f"({metrics.stories} stories now, {then} then; momentum "
        f"{'not measured' if momentum is None else f'{momentum:.0f}'})"
    )
