"""The trend score: explicit arithmetic, missing data never zeroed, reproducible.

Pure functions over ``TopicMetrics``; nothing here touches a source, a model
or a clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.research.trends import scoring
from app.research.trends.contracts import (
    TREND_SCORING_VERSION,
    Confidence,
    HistoryMetrics,
    SearchMetrics,
    TopicMetrics,
    TrendScores,
    TrendStatus,
    VideoMetrics,
    VolumeWindows,
    WindowCounts,
)

NOW = datetime(2026, 10, 12, 8, tzinfo=UTC)
WEIGHTS = dict(scoring.DEFAULT_WEIGHTS)
EVERY_SOURCE = list(scoring.OPTIONAL_SUB_SIGNALS)


def _metrics(**overrides: object) -> TopicMetrics:
    base: dict[str, object] = {
        "window_days": 7,
        "observed_at": NOW,
        "applicable": ["search", "search_level", "news_volume", "news_counts", "novelty"],
        "stories": 6,
        "independent_publishers": 6,
        "source_types": ["news", "feed", "search"],
        "source_types_available": ["news", "feed", "search"],
        "coverage_measured": True,
        "search": SearchMetrics(
            source="google_trends",
            current_interest=40,
            previous_interest=20,
            anchor_interest=40,
        ),
        "news_volume": VolumeWindows(
            source="gdelt", recent=30, baseline=15, baseline_windows=8
        ),
        "news_counts": [WindowCounts(source="news", recent=8, previous=4)],
        "questions": 2,
        "dated_items": 6,
        "median_age_days": 2.0,
        "novelty_similarity": 0.5,
        "on_topic_share": 1.0,
        "specific_subject": True,
        "has_outcome": True,
    }
    base.update(overrides)
    return TopicMetrics.model_validate(base)


def _score(metrics: TopicMetrics, min_coverage: float = 0.6) -> TrendScores:
    return scoring.score_from_metrics(metrics, WEIGHTS, min_data_coverage=min_coverage)


# --- sub-signals -------------------------------------------------------------


def test_momentum_beats_level() -> None:
    """40 -> 80 is a stronger trend than 90 -> 93."""
    rising = _metrics(
        search=SearchMetrics(source="t", current_interest=80, previous_interest=40)
    )
    flat = _metrics(search=SearchMetrics(source="t", current_interest=93, previous_interest=90))
    a, b = scoring.search_growth(rising), scoring.search_growth(flat)
    assert a is not None and b is not None and a > b
    assert a == pytest.approx(74.56, abs=0.01)  # 50 + 25 * log2(81 / 41)


def test_a_breakout_scores_full_marks_and_rising_percent_is_the_fallback() -> None:
    assert (
        scoring.search_growth(_metrics(search=SearchMetrics(source="t", is_breakout=True)))
        == 100.0
    )
    fallback = scoring.search_growth(
        _metrics(search=SearchMetrics(source="t", rising_percent=300))
    )
    assert fallback == pytest.approx(100.0)
    assert scoring.search_growth(_metrics(search=None)) is None


def test_small_counts_need_a_real_change_to_read_as_growth() -> None:
    """0 -> 2 articles is not a surge; 30 -> 90 is."""
    assert scoring.count_growth(2, 0) == pytest.approx(62.1, abs=0.1)
    assert scoring.count_growth(90, 30) > 85
    assert scoring.count_growth(10, 10) == 50.0


def test_an_unknown_earlier_window_is_unmeasured_not_a_surge() -> None:
    truncated = _metrics(news_counts=[WindowCounts(source="news", recent=100, previous=None)])
    assert scoring.news_count_growth(truncated) is None
    no_baseline = _metrics(
        news_volume=VolumeWindows(source="gdelt", recent=40, baseline=None, baseline_windows=1)
    )
    assert scoring.news_volume_growth(no_baseline) is None


def test_search_level_is_measured_against_the_anchor() -> None:
    equal = _metrics(search=SearchMetrics(source="t", current_interest=40, anchor_interest=40))
    four_times = _metrics(
        search=SearchMetrics(source="t", current_interest=80, anchor_interest=20)
    )
    assert scoring.search_level(equal) == pytest.approx(50.0)
    assert scoring.search_level(four_times) == pytest.approx(99.6, abs=0.5)


def test_coverage_counts_outlets_and_kinds_and_is_unknown_when_unmeasured() -> None:
    assert scoring.publishers(_metrics(independent_publishers=4)) == pytest.approx(50.0)
    assert scoring.publishers(_metrics(independent_publishers=8)) == pytest.approx(75.0)
    unmeasured = _metrics(coverage_measured=False, independent_publishers=0)
    assert scoring.publishers(unmeasured) is None
    assert scoring.source_types(unmeasured) is None
    two_of_three = _metrics(
        source_types=["news", "feed"], source_types_available=["news", "feed", "video"]
    )
    assert scoring.source_types(two_of_three) == pytest.approx(66.7, abs=0.1)


def test_video_uses_windows_never_lifetime_totals() -> None:
    video = _metrics(
        video=VideoMetrics(
            source="youtube",
            videos=20,
            recent_uploads=12,
            previous_uploads=4,
            median_views_per_day=500.0,
        )
    )
    assert scoring.video_growth(video) is not None and scoring.video_growth(video) > 60
    assert scoring.video_views(video) == pytest.approx(57.4, abs=0.2)


def test_freshness_needs_dates_and_history_to_measure() -> None:
    assert scoring.recency(_metrics(median_age_days=0.0)) == pytest.approx(100.0)
    assert scoring.recency(_metrics(median_age_days=3.5)) == pytest.approx(50.0)
    assert scoring.recency(_metrics(median_age_days=None)) is None
    first_run_ever = _metrics(history=HistoryMetrics(previous_observations=0, system_runs=0))
    assert scoring.newness(first_run_ever) is None
    new_topic = _metrics(history=HistoryMetrics(previous_observations=0, system_runs=3))
    assert scoring.newness(new_topic) == 100.0
    seen_every_week = _metrics(
        history=HistoryMetrics(
            previous_observations=3,
            system_runs=3,
            consecutive_runs=3,
            last_observed_at=NOW - timedelta(days=7),
            previous_stories=6.0,
        )
    )
    assert scoring.newness(seen_every_week) == 25.0
    new_wave = _metrics(
        stories=12,
        history=HistoryMetrics(
            previous_observations=3,
            system_runs=3,
            consecutive_runs=3,
            last_observed_at=NOW - timedelta(days=7),
            previous_stories=6.0,
        ),
    )
    assert scoring.newness(new_wave) == 75.0  # an evergreen topic's new wave is news


def test_novelty_maps_similarity_between_floor_and_duplicate_threshold() -> None:
    assert scoring.novelty(_metrics(novelty_similarity=0.3)) == 100.0
    assert scoring.novelty(_metrics(novelty_similarity=0.92)) == 0.0
    assert scoring.novelty(_metrics(novelty_similarity=0.71)) == pytest.approx(50.0, abs=0.1)
    assert scoring.novelty(_metrics(novelty_similarity=None)) is None


# --- the whole score ---------------------------------------------------------


def test_overall_is_the_weighted_mean_of_the_dimensions() -> None:
    scores = _score(_metrics())
    assert scores.overall is not None
    expected = sum(scores.components[d] * WEIGHTS[d] for d in scores.components) / sum(
        WEIGHTS[d] for d in scores.components
    )
    assert scores.overall == pytest.approx(expected, abs=0.01)
    assert set(scores.components) == set(scoring.DIMENSIONS)
    assert scores.version == TREND_SCORING_VERSION
    assert sum(scores.weights_used.values()) == pytest.approx(1.0, abs=1e-3)


def test_every_dimension_is_on_a_0_to_100_scale() -> None:
    extreme = _metrics(
        search=SearchMetrics(
            source="t", is_breakout=True, current_interest=100, anchor_interest=0.0
        ),
        news_volume=VolumeWindows(source="g", recent=10_000, baseline=1, baseline_windows=8),
        independent_publishers=500,
        questions=99,
        median_age_days=0.0,
    )
    scores = _score(extreme)
    assert all(0.0 <= value <= 100.0 for value in scores.components.values())


def test_a_source_that_failed_lowers_coverage_never_the_score_to_zero() -> None:
    """GDELT and news down: those signals are missing, not zero."""
    full = _score(_metrics())
    degraded_metrics = _metrics(news_volume=None, news_counts=[])
    degraded = _score(degraded_metrics)

    assert degraded.dimensions["momentum"].signals["news_volume"] is None
    assert degraded.dimensions["momentum"].measured_fraction == pytest.approx(1 / 3, abs=1e-3)
    assert degraded.components["momentum"] == degraded.dimensions["momentum"].signals["search"]
    assert degraded.data_coverage < full.data_coverage
    assert degraded.overall is not None and degraded.overall > 0


def test_a_source_this_deployment_does_not_have_is_neither_measured_nor_missing() -> None:
    without_youtube = _score(_metrics())
    assert "video" not in without_youtube.dimensions["momentum"].signals
    with_youtube_down = _score(_metrics(applicable=[*_metrics().applicable, "video"]))
    assert with_youtube_down.dimensions["momentum"].signals["video"] is None
    assert with_youtube_down.data_coverage < without_youtube.data_coverage


def test_no_momentum_means_no_score_however_much_else_was_measured() -> None:
    scores = _score(_metrics(search=None, news_volume=None, news_counts=[]))
    assert scores.overall is None
    assert scores.insufficient == "no momentum signal could be measured"
    assert "momentum" in scores.unavailable


def test_too_little_measured_means_no_score_rather_than_a_guess() -> None:
    thin = _metrics(
        applicable=EVERY_SOURCE,
        search=SearchMetrics(source="t", rising_percent=200),
        news_volume=None,
        news_counts=[],
        coverage_measured=False,
        median_age_days=None,
        novelty_similarity=None,
    )
    scores = _score(thin)
    assert scores.overall is None
    assert scores.insufficient is not None and scores.insufficient.startswith("only ")
    assert scores.data_coverage < 0.6
    assert _score(thin, min_coverage=0.0).overall is not None


def test_coverage_at_exactly_the_threshold_passes() -> None:
    """Integer weights: 30 + 20 + 10 is exactly 60, never 0.6000000001 short."""
    metrics = _metrics(
        applicable=["search", "search_level"],
        coverage_measured=False,
        median_age_days=None,
    )
    scores = _score(metrics)
    assert scores.data_coverage == 0.6
    assert scores.overall is not None


def test_a_stored_score_is_reproduced_from_its_stored_metrics() -> None:
    metrics = _metrics(
        history=HistoryMetrics(
            previous_observations=2,
            previous_stories=3.0,
            system_runs=4,
            consecutive_runs=1,
            last_observed_at=NOW - timedelta(days=7),
        )
    )
    stored_metrics = json.loads(json.dumps(metrics.model_dump(mode="json")))
    stored_scores = json.loads(json.dumps(_score(metrics).model_dump(mode="json")))

    again = _score(TopicMetrics.model_validate(stored_metrics))
    assert again.model_dump(mode="json") == stored_scores


def test_ranking_is_deterministic_on_ties() -> None:
    keys = [
        scoring.rank_key(70.0, 50.0, "Creatine for women"),
        scoring.rank_key(70.0, 60.0, "Zone 2 training"),
        scoring.rank_key(70.0, 50.0, "ashwagandha stress"),
        scoring.rank_key(80.0, 10.0, "Sauna"),
    ]
    assert sorted(keys) == sorted(reversed(keys))
    assert [k[2] for k in sorted(keys)] == [
        "sauna",
        "zone 2 training",
        "ashwagandha stress",
        "creatine for women",
    ]


def test_weights_must_be_integers_summing_to_100() -> None:
    with pytest.raises(ValueError, match="sum to 100"):
        scoring.validate_weights({**WEIGHTS, "momentum": 31})
    with pytest.raises(ValueError, match="exactly"):
        scoring.validate_weights({"momentum": 100})


def test_confidence_reflects_coverage_kinds_and_history() -> None:
    seen = HistoryMetrics(previous_observations=2, system_runs=2)
    rich = _metrics(source_types=["news", "feed", "search"], history=seen)
    assert scoring.confidence(rich, 0.9) is Confidence.HIGH
    assert scoring.confidence(_metrics(history=None), 0.9) is Confidence.MEDIUM
    assert scoring.confidence(_metrics(source_types=["news"]), 0.9) is Confidence.LOW


# --- trend status ------------------------------------------------------------


def test_status_is_preliminary_when_nothing_compared_two_periods() -> None:
    metrics = _metrics(
        search=SearchMetrics(source="t", current_interest=50, anchor_interest=40),
        news_volume=VolumeWindows(source="g", recent=10, baseline=None, baseline_windows=0),
        news_counts=[WindowCounts(source="n", recent=10, previous=None)],
    )
    scores = scoring.score_from_metrics(metrics, WEIGHTS, min_data_coverage=0.0)
    assert scoring.trend_status(metrics, scores) is TrendStatus.PRELIMINARY


def test_status_follows_momentum_and_history() -> None:
    def status(momentum_source: VolumeWindows, history: HistoryMetrics | None) -> TrendStatus:
        metrics = _metrics(
            search=None, news_counts=[], news_volume=momentum_source, history=history
        )
        return scoring.trend_status(metrics, _score(metrics, min_coverage=0.0))

    rising = VolumeWindows(source="g", recent=60, baseline=10, baseline_windows=8)
    flat = VolumeWindows(source="g", recent=12, baseline=11, baseline_windows=8)
    falling = VolumeWindows(source="g", recent=2, baseline=30, baseline_windows=8)
    seen = HistoryMetrics(previous_observations=3, system_runs=3)

    assert status(rising, None) is TrendStatus.EMERGING
    assert status(rising, seen) is TrendStatus.ACCELERATING
    assert status(flat, seen) is TrendStatus.STEADY
    assert status(falling, seen) is TrendStatus.DECLINING
    # First seen and flat is not "emerging": nothing rose.
    assert status(flat, None) is TrendStatus.PRELIMINARY
