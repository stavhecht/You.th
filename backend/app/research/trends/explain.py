"""Explanations and the result, built from measured numbers. No model call.

A topic's ``summary``, ``selection_reason`` and ``limitations`` are templates
filled from its ``TopicMetrics`` and ``TrendScores``: each clause appears only
when the number behind it was measured. A language model is never asked to
explain a ranking, because it would explain one it could not see — and would
happily say "rapidly growing" about a topic nobody measured twice.

``supporting_sources`` are collected items, one per story, so every link shown
as evidence of a trend is a record a source actually returned.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.research.contracts import CandidateTopic, ResearchState
from app.research.trends.contracts import (
    CollectionWindow,
    ResultSources,
    ResultTopic,
    SourceItem,
    SourceType,
    Story,
    SupportingSource,
    TopicMetrics,
    TrendScores,
    TrendSearchResult,
    TrendStatus,
)
from app.research.trends.scoring import DIMENSIONS, growth_percent

#: Supporting sources kept per topic.
SUPPORTING_LIMIT = 8
_DIMENSION_WORDS = {
    "momentum": "momentum",
    "coverage": "cross-source coverage",
    "audience": "audience interest",
    "freshness": "freshness",
    "editorial_fit": "editorial fit",
}
_TYPE_ORDER = {
    SourceType.NEWS: 0,
    SourceType.FEED: 1,
    SourceType.VIDEO: 3,
    SourceType.WEB: 4,
}


def supporting_sources(
    items: Sequence[SourceItem],
    stories: Sequence[Story],
    story_of: dict[int, int],
    item_ids: Sequence[int],
    window_start: datetime,
    limit: int = SUPPORTING_LIMIT,
) -> list[SupportingSource]:
    """One item per story, reporting first, then newest; current window first."""
    seen: set[int] = set()
    leads: list[SourceItem] = []
    for index in item_ids:
        if index >= len(items) or items[index].url is None:
            continue
        story_id = story_of.get(index)
        if story_id is None or story_id in seen:
            continue
        seen.add(story_id)
        leads.append(items[stories[story_id].representative])

    def order(item: SourceItem) -> tuple[int, int, float, str]:
        current = item.published_at is None or item.published_at >= window_start
        when = item.published_at.timestamp() if item.published_at else 0.0
        return (0 if current else 1, _TYPE_ORDER.get(item.source_type, 5), -when, item.title)

    # Prefer variety of source types in the first few, then fill by order.
    leads.sort(key=order)
    picked: list[SourceItem] = []
    types_seen: set[SourceType] = set()
    for item in leads:
        if item.source_type not in types_seen:
            picked.append(item)
            types_seen.add(item.source_type)
    for item in leads:
        if len(picked) >= limit:
            break
        if item not in picked:
            picked.append(item)
    picked = sorted(picked[:limit], key=order)
    return [
        SupportingSource(
            source_name=item.source_name,
            source_type=item.source_type,
            title=item.title,
            url=item.url or "",
            publisher=item.publisher or item.publisher_domain,
            published_at=item.published_at,
        )
        for item in picked
        if item.url
    ]


def summary(metrics: TopicMetrics) -> str:
    """Why this topic may be relevant now — measured clauses only."""
    clauses: list[str] = []
    window = f"{metrics.window_days} days"
    if metrics.coverage_measured:
        outlets = metrics.independent_publishers
        clauses.append(
            f"{outlets} independent outlet{'s' if outlets != 1 else ''} covered it in the "
            f"last {window}"
            + (
                f" ({metrics.syndicated_copies} syndicated copies collapsed)"
                if metrics.syndicated_copies
                else ""
            )
        )
    volume = metrics.news_volume
    if volume is not None and volume.baseline is not None:
        change = growth_percent(volume.recent, volume.baseline)
        if change is not None:
            clauses.append(
                f"news volume {change:+.0f}% against its {volume.baseline_windows}-window "
                "baseline (GDELT)"
            )
    search = metrics.search
    if search is not None:
        if search.is_breakout:
            clauses.append("a breakout search on Google Trends")
        elif search.current_interest is not None and search.previous_interest:
            change = growth_percent(search.current_interest, search.previous_interest)
            if change is not None:
                clauses.append(
                    f"search interest {change:+.0f}% on the previous {window} "
                    "(Google Trends, a relative index)"
                )
        elif search.rising_percent is not None:
            clauses.append(f"a rising search (+{search.rising_percent}% on Google Trends)")
    if metrics.video and metrics.video.recent_uploads:
        clauses.append(f"{metrics.video.recent_uploads} new videos")
    if metrics.questions:
        clauses.append(f"{metrics.questions} distinct questions asked")
    if not clauses:
        return "Too little was measured to describe this topic's trend."
    text = "; ".join(clauses)
    return text[0].upper() + text[1:] + "."


def selection_reason(scores: TrendScores, rank: int, ranked: int) -> str:
    """Rank, score, and the two dimensions that contributed most."""
    contributions = sorted(
        (
            (scores.components[name] * scores.weights_used.get(name, 0.0), name)
            for name in DIMENSIONS
            if name in scores.components
        ),
        reverse=True,
    )
    top = [
        f"{_DIMENSION_WORDS[name]} {scores.components[name]:.0f}"
        for _, name in contributions[:2]
    ]
    return (
        f"Ranked {rank} of {ranked} on trend score {scores.overall:.1f}"
        + (f", led by {' and '.join(top)}" if top else "")
        + "."
    )


def limitations(
    metrics: TopicMetrics, scores: TrendScores, status: TrendStatus, notes: Sequence[str] = ()
) -> list[str]:
    """What this topic's assessment could not see, stated plainly."""
    found: list[str] = []
    for name in scores.unavailable:
        found.append(f"{_DIMENSION_WORDS[name]} could not be measured")
    for name, dimension in scores.dimensions.items():
        if dimension.score is not None and dimension.measured_fraction < 1.0:
            missing = [sig for sig, value in dimension.signals.items() if value is None]
            if missing:
                found.append(
                    f"{_DIMENSION_WORDS[name]} rests on part of its signals "
                    f"(not measured: {', '.join(missing)})"
                )
    if status is TrendStatus.PRELIMINARY:
        found.append("preliminary: nothing compared this topic against an earlier period")
    if metrics.history is None or metrics.history.previous_observations == 0:
        found.append("first observation of this topic; no history to compare with")
    if len(set(metrics.source_types)) <= 1:
        found.append("seen in only one kind of source")
    if metrics.search is not None:
        found.append("Google Trends values are relative interest, not search counts")
    found.extend(notes)
    return found


def result_topic(
    candidate: CandidateTopic,
    *,
    rank: int,
    ranked: int,
    shortlisted: bool,
) -> ResultTopic:
    metrics, scores = candidate.metrics, candidate.scores
    assert metrics is not None and scores.overall is not None
    status = candidate.trend_status or TrendStatus.PRELIMINARY
    google_news = any(s.source_name == "google_news" for s in candidate.supporting)
    return ResultTopic(
        topic_id=candidate.topic_ref_id,
        candidate_id=candidate.topic_id,
        topic=candidate.canonical_topic,
        category=candidate.category,
        subject=candidate.subject,
        outcome=candidate.outcome,
        rank=rank,
        summary=summary(metrics),
        trend_score=scores.overall,
        score_breakdown={name: scores.components.get(name) for name in DIMENSIONS},
        confidence=scores.confidence,
        data_coverage=scores.data_coverage,
        trend_status=status,
        key_questions=candidate.key_questions,
        supporting_sources=candidate.supporting,
        selection_reason=selection_reason(scores, rank, ranked),
        limitations=limitations(
            metrics,
            scores,
            status,
            ["Google News links are Google redirect URLs"] if google_news else [],
        ),
        status=candidate.status.value,
        trend_shortlisted=shortlisted,
    )


def build_result(
    state: ResearchState,
    ranked: Sequence[CandidateTopic],
    shortlist_ids: set[str],
    *,
    started_at: datetime | None,
    completed_at: datetime | None,
) -> TrendSearchResult:
    """The trend agent's output for this run, frozen at the end of its phase."""
    assert state.window_start is not None and state.window_end is not None
    sources = ResultSources()
    for name, status in sorted(state.provider_status.items()):
        if name in ("pubmed", "europe_pmc", "extraction"):
            continue  # the evidence check's, not the trend agent's
        bucket = {
            "ok": sources.successful,
            "partial": sources.partial,
            "failed": sources.failed,
        }.get(status.status, sources.skipped)
        bucket.append(name)
    errors = [
        f"{name}: {status.detail}"
        for name, status in sorted(state.provider_status.items())
        if status.status in ("failed", "partial")
        and status.detail
        and name not in ("pubmed", "europe_pmc", "extraction")
    ]
    return TrendSearchResult(
        run_id=state.run_id,
        label=state.label,
        status="completed",
        started_at=started_at,
        completed_at=completed_at,
        collection_window=CollectionWindow(start=state.window_start, end=state.window_end),
        sources=sources,
        topics=[
            result_topic(
                candidate,
                rank=candidate.rank or position,
                ranked=len(ranked),
                shortlisted=candidate.topic_id in shortlist_ids,
            )
            for position, candidate in enumerate(ranked, start=1)
        ],
        counts=dict(state.counts),
        notes=list(state.notes),
        errors=errors,
    )
