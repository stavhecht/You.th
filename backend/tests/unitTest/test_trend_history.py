"""Trend history: identity across runs, history metrics, repeat suppression."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.research.categories import Category
from app.research.trends.contracts import (
    HistoryMetrics,
    SourceType,
    TopicMetrics,
    TrendScores,
    WindowCounts,
)
from app.research.trends.documents import make_item
from app.research.trends.history import (
    InMemoryTopicStore,
    Observation,
    ResolvedTopic,
    TopicRef,
    history_metrics,
    repeat_reason,
    topic_key,
)
from app.research.trends.metrics import questions_in, window_counts

NOW = datetime(2026, 10, 12, 8, tzinfo=UTC)


def test_topic_keys_ignore_order_case_plurals_and_filler() -> None:
    assert topic_key("Creatine", "Women") == topic_key("creatine", "the women")
    assert topic_key("creatine gummies", "") == topic_key("Gummy creatine", "")
    assert topic_key("creatine", "sleep") != topic_key("creatine", "women")


async def test_resolving_twice_gives_the_same_identity() -> None:
    store = InMemoryTopicStore()
    ref = TopicRef(
        topic_key="creatine|women",
        label="Creatine for women",
        subject="creatine",
        outcome="women",
        category=Category.SUPPLEMENTS,
    )
    first = await store.resolve([ref], now=NOW, exclude_run="r1")
    second = await store.resolve([ref], now=NOW + timedelta(days=7), exclude_run="r2")
    assert first["creatine|women"].id == second["creatine|women"].id
    assert second["creatine|women"].first_seen_at == NOW


def _topic(*observations: Observation) -> ResolvedTopic:
    return ResolvedTopic(
        id="t", first_seen_at=NOW - timedelta(days=60), observations=list(observations)
    )


def test_history_metrics_count_runs_in_a_row_and_the_last_proposal() -> None:
    topic = _topic(
        Observation(
            run_id="r3", observed_at=NOW - timedelta(days=7), stories=6, proposed=False
        ),
        Observation(
            run_id="r2", observed_at=NOW - timedelta(days=14), stories=4, proposed=True
        ),
        Observation(
            run_id="r0", observed_at=NOW - timedelta(days=28), stories=2, proposed=False
        ),
    )
    history = history_metrics(topic, ["r3", "r2", "r1", "r0"], now=NOW)
    assert history.previous_observations == 3
    assert history.consecutive_runs == 2  # r3, r2 — then r1 missed it
    assert history.system_runs == 4
    assert history.previous_stories == 4.0
    assert history.last_proposed_at == NOW - timedelta(days=14)
    assert history.stories_when_proposed == 4


def test_a_topic_never_seen_has_an_empty_history_not_a_missing_one() -> None:
    history = history_metrics(_topic(), ["r1"], now=NOW)
    assert history.previous_observations == 0
    assert history.previous_stories is None
    assert history.system_runs == 1


def _metrics(stories: int, history: HistoryMetrics | None) -> TopicMetrics:
    return TopicMetrics(
        window_days=7, observed_at=NOW, stories=stories, history=history, coverage_measured=True
    )


def test_a_recent_proposal_with_nothing_new_is_suppressed() -> None:
    history = HistoryMetrics(
        previous_observations=1,
        last_proposed_at=NOW - timedelta(days=7),
        stories_when_proposed=6,
    )
    reason = repeat_reason(
        _metrics(7, history),
        TrendScores(components={"momentum": 55.0}),
        now=NOW,
        cooloff_days=28,
    )
    assert reason is not None and "not meaningfully changed" in reason


def test_a_new_wave_lifts_the_suppression() -> None:
    history = HistoryMetrics(
        previous_observations=1,
        last_proposed_at=NOW - timedelta(days=7),
        stories_when_proposed=6,
    )
    doubled = repeat_reason(
        _metrics(12, history),
        TrendScores(components={"momentum": 50.0}),
        now=NOW,
        cooloff_days=28,
    )
    accelerating = repeat_reason(
        _metrics(6, history),
        TrendScores(components={"momentum": 70.0}),
        now=NOW,
        cooloff_days=28,
    )
    assert doubled is None and accelerating is None


def test_after_the_cooloff_a_topic_may_return() -> None:
    history = HistoryMetrics(
        previous_observations=1,
        last_proposed_at=NOW - timedelta(days=40),
        stories_when_proposed=6,
    )
    assert repeat_reason(_metrics(6, history), TrendScores(), now=NOW, cooloff_days=28) is None


# --- window arithmetic the history rests on ------------------------------------------


def _dated(days_ago: float, kind: SourceType = SourceType.NEWS, **engagement: float):
    item = make_item(
        source_name="s",
        source_type=kind,
        title=f"creatine {days_ago}",
        url=f"https://x.example/{days_ago}",
        published_at=NOW - timedelta(days=days_ago),
        collected_at=NOW,
        engagement=engagement or None,
    )
    assert item is not None
    return item


def test_window_counts_split_at_the_boundary_and_unknown_when_truncated() -> None:
    items = [_dated(1), _dated(2), _dated(9)]
    boundary = NOW - timedelta(days=7)
    assert window_counts("n", items, boundary, truncated=False) == WindowCounts(
        source="n", recent=2, previous=1
    )
    assert window_counts("n", items, boundary, truncated=True).previous is None


def test_headlines_are_questions_only_with_a_question_mark() -> None:
    found = questions_in(
        ["how much creatine per day", "creatine loading phase"],
        ["What the new research says about creatine", "Is creatine safe for teens?"],
    )
    assert found == ["how much creatine per day", "Is creatine safe for teens?"]
