"""The trend score. Pure functions: no I/O, no model, no clock.

Five dimensions, each 0-100, with integer base weights (30/25/20/15/10 by
default, configurable, summing to 100):

======================  =====================================================
momentum (30)           is interest *increasing*? Change against a baseline
                        in each source — never a level, never a lifetime total
coverage (25)           how many independent outlets, and how many kinds of
                        source, are discussing it
audience (20)           how much demand is visible: search level, views per
                        day, questions asked
freshness (15)          how recent the activity is, and whether the angle is new
editorial_fit (10)      relevance to the publication; not a validity judgement
======================  =====================================================

Each dimension is the weighted mean of its **sub-signals that were measured**.
A sub-signal is only counted at all when it was *applicable* — its source was
enabled and configured, and (for history) there was a history to read — so a
deployment without a YouTube key is not marked down forever for lacking one.

**Missing data policy: reweight, with a floor.** A dimension with nothing
measured is left out and the remaining weights are renormalised — never filled
with zero, never guessed. ``data_coverage`` says how much of the full score
was measured: each dimension's base weight times the share of its applicable
sub-signal weight that was measured. A topic whose momentum could not be
measured at all, or whose coverage is below the run's minimum, gets **no
score** (``insufficient``) rather than a score built on what little remained:
a trend whose rise nobody measured is not a trend.

Count-based growth uses a stronger prior than ``log_ratio_score``'s (``K``
added to both windows), because coverage counts are small: 0 -> 2 articles is
not a surge, 30 -> 90 is.

Everything a score reads is in ``TopicMetrics``, stored beside it, so
``score_from_metrics`` reproduces any stored score exactly.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from app.research.trends.contracts import (
    TREND_SCORING_VERSION,
    Confidence,
    DimensionScore,
    TopicMetrics,
    TrendScores,
    TrendStatus,
)

MOMENTUM = "momentum"
COVERAGE = "coverage"
AUDIENCE = "audience"
FRESHNESS = "freshness"
EDITORIAL_FIT = "editorial_fit"
DIMENSIONS = (MOMENTUM, COVERAGE, AUDIENCE, FRESHNESS, EDITORIAL_FIT)
DEFAULT_WEIGHTS = {MOMENTUM: 30, COVERAGE: 25, AUDIENCE: 20, FRESHNESS: 15, EDITORIAL_FIT: 10}

#: Prior added to both windows of a count comparison.
GROWTH_PRIOR = 5.0
#: Median views per day of recent uploads that scores 100 on the log scale.
FULL_VIDEO_VIEWS_PER_DAY = 50_000.0
#: Recent posts' score + comments that scores 100 on the log scale.
#: Below this similarity to anything already written, a topic is wholly new.
NOVELTY_FLOOR = 0.5
#: A topic absent from runs for this long counts as a new wave if it returns.
NEW_WAVE_AFTER_DAYS = 28
#: Momentum at or above this is "accelerating"; below the lower one, "declining".
ACCELERATING_AT = 65.0
DECLINING_BELOW = 40.0
#: A first sighting is "emerging" only with momentum at least this (a rise).
EMERGING_AT = 55.0


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def log_ratio_score(current: float, previous: float) -> float:
    """50 when flat, +25 per doubling, -25 per halving; Laplace-smoothed by 1.

    For Google Trends' 0-100 interest values, where one point is not noise.
    """
    return clamp(50.0 + 25.0 * math.log2((current + 1.0) / (previous + 1.0)))


def count_growth(recent: float, previous: float) -> float:
    """``log_ratio_score`` for small counts, with ``GROWTH_PRIOR`` on both sides."""
    return clamp(50.0 + 25.0 * math.log2((recent + GROWTH_PRIOR) / (previous + GROWTH_PRIOR)))


def growth_percent(current: float, previous: float) -> float | None:
    if previous <= 0:
        return None
    return round((current - previous) / previous * 100.0, 1)


def log_scale(value: float, full: float) -> float:
    """0 at 0, 100 at ``full``, logarithmic between."""
    return clamp(100.0 * math.log1p(max(0.0, value)) / math.log1p(full))


def saturating(count: float, half: float) -> float:
    """``100 * (1 - 2^(-count/half))``: 50 at ``half``, 75 at twice it."""
    return 100.0 * (1.0 - math.pow(2.0, -max(0.0, count) / half))


# --- sub-signals -------------------------------------------------------------


def search_growth(m: TopicMetrics) -> float | None:
    """Google Trends: the interest series when it has data, else "rising"."""
    s = m.search
    if s is None:
        return None
    if s.is_breakout:
        return 100.0
    if (
        s.current_interest is not None
        and s.previous_interest is not None
        and s.current_interest + s.previous_interest > 0
    ):
        return log_ratio_score(s.current_interest, s.previous_interest)
    if s.rising_percent is not None:
        return clamp(50.0 + 25.0 * math.log2(1.0 + s.rising_percent / 100.0))
    return None


def news_volume_growth(m: TopicMetrics) -> float | None:
    v = m.news_volume
    if v is None or v.baseline is None:
        return None
    return count_growth(v.recent, v.baseline)


def news_count_growth(m: TopicMetrics) -> float | None:
    """Mean growth over the news searches whose earlier window is known."""
    values = [
        count_growth(c.recent, c.previous) for c in m.news_counts if c.previous is not None
    ]
    return statistics.fmean(values) if values else None


def video_growth(m: TopicMetrics) -> float | None:
    v = m.video
    if v is None or v.previous_uploads is None:
        return None
    return count_growth(v.recent_uploads, v.previous_uploads)


def history_growth(m: TopicMetrics) -> float | None:
    h = m.history
    if h is None or h.previous_stories is None or not m.coverage_measured:
        return None
    return count_growth(m.stories, h.previous_stories)


def publishers(m: TopicMetrics) -> float | None:
    """Independent outlets this window: 4 scores 50, 8 scores 75, 12 ~88."""
    if not m.coverage_measured:
        return None
    return saturating(m.independent_publishers, 4.0)


def source_types(m: TopicMetrics) -> float | None:
    """Kinds of source with activity, of the kinds that answered for this topic."""
    if not m.coverage_measured or not m.source_types_available:
        return None
    available = set(m.source_types_available)
    present = set(m.source_types) & available
    return 100.0 * len(present) / len(available)


def search_level(m: TopicMetrics) -> float | None:
    """Search interest against the anchor term in the same Trends request.

    Equal to the anchor scores 50, four times it 100, a quarter of it 0.
    """
    s = m.search
    if s is None or s.current_interest is None or s.anchor_interest is None:
        return None
    return clamp(
        50.0 + 25.0 * math.log2((s.current_interest + 0.5) / (s.anchor_interest + 0.5))
    )


def video_views(m: TopicMetrics) -> float | None:
    v = m.video
    if v is None or v.median_views_per_day is None:
        return None
    return log_scale(v.median_views_per_day, FULL_VIDEO_VIEWS_PER_DAY)


def questions(m: TopicMetrics) -> float | None:
    """Distinct questions people asked: 2 scores 50, 4 scores 75."""
    return saturating(m.questions, 2.0)


def recency(m: TopicMetrics) -> float | None:
    """Median age of dated items: today 100, half a window 50, a window 25."""
    if m.median_age_days is None:
        return None
    half_life = max(0.5, m.window_days / 2.0)
    return 100.0 * math.pow(2.0, -max(0.0, m.median_age_days) / half_life)


def newness(m: TopicMetrics) -> float | None:
    """New to our runs, back after an absence, or seen run after run.

    ``None`` when there is no run history at all — on the first run every
    topic would be "new", which says nothing. A topic seen in recent runs is
    not penalised when its coverage has grown by half: an evergreen subject
    with a new wave of interest is still news.
    """
    h = m.history
    if h is None or h.system_runs < 1:
        return None
    if h.previous_observations == 0 or h.last_observed_at is None:
        return 100.0
    if (m.observed_at - h.last_observed_at).days >= NEW_WAVE_AFTER_DAYS:
        return 75.0
    value = max(25.0, 100.0 - 25.0 * h.consecutive_runs)
    if h.previous_stories and m.stories >= 1.5 * h.previous_stories:
        value = max(value, 75.0)
    return value


def novelty(m: TopicMetrics) -> float | None:
    """100 for nothing similar already written or decided, 0 at the duplicate line."""
    if m.novelty_similarity is None:
        return None
    span = m.duplicate_threshold - NOVELTY_FLOOR
    return clamp(100.0 * (m.duplicate_threshold - m.novelty_similarity) / span)


def on_topic(m: TopicMetrics) -> float | None:
    return None if m.on_topic_share is None else 100.0 * m.on_topic_share


def specific_subject(m: TopicMetrics) -> float | None:
    return 100.0 if m.specific_subject else 40.0


def angle(m: TopicMetrics) -> float | None:
    return 100.0 if m.has_outcome else 60.0


@dataclass(frozen=True, slots=True)
class SubSignal:
    name: str
    dimension: str
    weight: float
    measure: Callable[[TopicMetrics], float | None]
    #: Counted whatever the configuration: computed from data every run has.
    always: bool = False


SUB_SIGNALS: tuple[SubSignal, ...] = (
    SubSignal("search", MOMENTUM, 1.0, search_growth),
    SubSignal("news_volume", MOMENTUM, 1.0, news_volume_growth),
    SubSignal("news_counts", MOMENTUM, 1.0, news_count_growth),
    SubSignal("video", MOMENTUM, 1.0, video_growth),
    SubSignal("history", MOMENTUM, 1.0, history_growth),
    SubSignal("publishers", COVERAGE, 0.7, publishers, always=True),
    SubSignal("source_types", COVERAGE, 0.3, source_types, always=True),
    SubSignal("search_level", AUDIENCE, 2.0, search_level),
    SubSignal("video_views", AUDIENCE, 1.0, video_views),
    SubSignal("questions", AUDIENCE, 0.5, questions, always=True),
    SubSignal("recency", FRESHNESS, 1.0, recency, always=True),
    SubSignal("newness", FRESHNESS, 1.0, newness),
    SubSignal("novelty", FRESHNESS, 1.0, novelty),
    SubSignal("on_topic", EDITORIAL_FIT, 0.5, on_topic, always=True),
    SubSignal("specific_subject", EDITORIAL_FIT, 0.25, specific_subject, always=True),
    SubSignal("angle", EDITORIAL_FIT, 0.25, angle, always=True),
)
SUB_SIGNAL_NAMES = tuple(s.name for s in SUB_SIGNALS)
#: Sub-signals whose source a run may or may not have: the graph lists the
#: applicable ones in ``TopicMetrics.applicable``.
OPTIONAL_SUB_SIGNALS = tuple(s.name for s in SUB_SIGNALS if not s.always)


def validate_weights(weights: Mapping[str, int]) -> dict[str, int]:
    if set(weights) != set(DIMENSIONS):
        raise ValueError(f"trend weights must name exactly {', '.join(DIMENSIONS)}")
    if any(int(w) != w or w < 0 for w in weights.values()):
        raise ValueError("trend weights must be non-negative integers")
    if sum(weights.values()) != 100:
        raise ValueError(f"trend weights must sum to 100, got {sum(weights.values())}")
    return {name: int(weights[name]) for name in DIMENSIONS}


def score_from_metrics(
    metrics: TopicMetrics,
    weights: Mapping[str, int],
    *,
    min_data_coverage: float,
) -> TrendScores:
    """The trend score of one topic, from its metrics and nothing else."""
    weights = validate_weights(weights)
    applicable = set(metrics.applicable)
    dimensions: dict[str, DimensionScore] = {}
    for dimension in DIMENSIONS:
        signals: dict[str, float | None] = {}
        weight_total = weight_measured = weighted_sum = 0.0
        for sub in SUB_SIGNALS:
            if sub.dimension != dimension or not (sub.always or sub.name in applicable):
                continue
            value = sub.measure(metrics)
            signals[sub.name] = None if value is None else round(value, 1)
            weight_total += sub.weight
            if value is not None:
                weight_measured += sub.weight
                weighted_sum += sub.weight * value
        dimensions[dimension] = DimensionScore(
            score=round(weighted_sum / weight_measured, 1) if weight_measured else None,
            measured_fraction=round(weight_measured / weight_total, 4) if weight_total else 0.0,
            signals=signals,
        )

    components = {name: dim.score for name, dim in dimensions.items() if dim.score is not None}
    unavailable = [name for name in DIMENSIONS if name not in components]
    coverage = round(
        sum(weights[name] * dimensions[name].measured_fraction for name in DIMENSIONS) / 100.0,
        4,
    )
    scores = TrendScores(
        version=TREND_SCORING_VERSION,
        components=components,
        unavailable=unavailable,
        weights=weights,
        dimensions=dimensions,
        data_coverage=coverage,
    )
    if MOMENTUM not in components:
        scores.insufficient = "no momentum signal could be measured"
        return scores
    if coverage < round(min_data_coverage, 4):
        scores.insufficient = (
            f"only {coverage:.0%} of the score could be measured "
            f"(needs {min_data_coverage:.0%})"
        )
        return scores
    total = sum(weights[name] for name in components)
    if total == 0:
        scores.insufficient = "every measured dimension has zero weight"
        return scores
    scores.weights_used = {name: round(weights[name] / total, 4) for name in components}
    scores.overall = round(
        sum(components[name] * weights[name] for name in components) / total, 2
    )
    scores.confidence = confidence(metrics, coverage)
    return scores


def confidence(metrics: TopicMetrics, coverage: float) -> Confidence:
    """How much the score rests on: coverage, kinds of source, and history."""
    kinds = len(set(metrics.source_types))
    seen_before = bool(metrics.history and metrics.history.previous_observations >= 1)
    if coverage >= 0.85 and kinds >= 3 and seen_before:
        return Confidence.HIGH
    if coverage >= 0.7 and kinds >= 2:
        return Confidence.MEDIUM
    return Confidence.LOW


def baselined(metrics: TopicMetrics) -> bool:
    """True when some momentum sub-signal compared against an earlier period."""
    s = metrics.search
    return any(
        (
            s is not None
            and (
                s.is_breakout
                or s.rising_percent is not None
                or (s.current_interest is not None and s.previous_interest is not None)
            ),
            metrics.news_volume is not None and metrics.news_volume.baseline is not None,
            any(c.previous is not None for c in metrics.news_counts),
            metrics.video is not None and metrics.video.previous_uploads is not None,
            metrics.history is not None and metrics.history.previous_stories is not None,
        )
    )


def trend_status(metrics: TopicMetrics, scores: TrendScores) -> TrendStatus:
    """Emerging, accelerating, steady or declining — or preliminary.

    Preliminary whenever nothing measured a change against an earlier period:
    "rising" is a claim about two periods, and it is not made from one. A
    topic seen for the first time is *emerging* only when its momentum shows a
    rise; first seen and flat is preliminary (measured 2026-10-12: a manual
    query with no coverage in either window read "emerging" at momentum 50).
    """
    momentum = scores.components.get(MOMENTUM)
    if momentum is None or not baselined(metrics):
        return TrendStatus.PRELIMINARY
    if momentum < DECLINING_BELOW:
        return TrendStatus.DECLINING
    if metrics.history is None or metrics.history.previous_observations == 0:
        return TrendStatus.EMERGING if momentum >= EMERGING_AT else TrendStatus.PRELIMINARY
    if momentum >= ACCELERATING_AT:
        return TrendStatus.ACCELERATING
    return TrendStatus.STEADY


def rank_key(
    overall: float | None, coverage_score: float | None, name: str
) -> tuple[float, float, str]:
    """Best first; ties on coverage, then name, so a ranking never depends on order."""
    return (-(overall or 0.0), -(coverage_score or 0.0), name.lower())
