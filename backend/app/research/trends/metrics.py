"""From collected items to one topic's ``TopicMetrics``. Pure functions, no I/O.

Every number here is counted from items a source returned, and every place
something could not be counted says ``None``:

* a window's earlier count is ``None`` when the source's page filled up first
  (it may hold more than we saw), never zero;
* coverage is ``coverage_measured = False`` when no news-like source answered
  for the topic — zero outlets would be a claim nobody checked;
* undated items count toward coverage but are left out of freshness, never
  treated as old.

Only items that **mention the topic's subject** count toward it. A search
engine pads its results; a GDELT or Google News query for "creatine women"
returns articles that mention neither, and counting those would let one loose
query inflate a topic's coverage.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.research.normalize import WELLNESS_TERMS, normalize_query, query_key, singular
from app.research.trends.contracts import (
    REPORTING_TYPES,
    DailySeries,
    HistoryMetrics,
    SearchMetrics,
    SourceItem,
    SourceType,
    Story,
    TopicMetrics,
    VideoMetrics,
    VolumeWindows,
    WindowCounts,
)

#: Fewer earlier windows than this and there is no baseline, only a number.
MIN_BASELINE_WINDOWS = 2
_QUESTION_START = re.compile(
    r"^(how|what|why|when|which|who|does|do|is|are|can|could|should|will|would|"
    r"am i|is it|are there)\b",
    re.IGNORECASE,
)
_WELLNESS_SINGULAR = frozenset(singular(word) for word in WELLNESS_TERMS)
#: Subjects that name a category rather than a thing an article can be about.
_VAGUE_SUBJECTS = frozenset(
    {
        "health",
        "wellness",
        "fitness",
        "nutrition",
        "diet",
        "exercise",
        "sleep",
        "lifestyle",
        "supplement",
        "supplements",
        "general",
        "various",
        "other",
        "news",
    }
)


@dataclass
class SourceEnrichment:
    """What one source answered for one topic during enrichment."""

    source: str
    source_type: SourceType
    answered: bool = False
    items: list[SourceItem] = field(default_factory=list)
    counts: WindowCounts | None = None
    volume: VolumeWindows | None = None
    video: VideoMetrics | None = None


# --- relevance, windows, questions ---------------------------------------


def mentions_subject(item: SourceItem, subject: str) -> bool:
    """Every content word of the subject appears in the title or description.

    Filler and headline furniture are not required: triage names subjects
    like "sauna use", and measured 2026-10-12 requiring "use" in a headline
    left "Sauna Use" with no outlets at all.
    """
    from app.research.normalize import _FILLER
    from app.research.trends.phrases import EDGE_STOPWORDS

    tokens = normalize_query(subject).split()
    words = [singular(w) for w in tokens if w not in _FILLER and w not in EDGE_STOPWORDS]
    if not words:
        words = [singular(w) for w in tokens]
    if not words:
        return False
    haystack = {
        singular(w) for w in normalize_query(f"{item.title} {item.description or ''}").split()
    }
    return all(word in haystack for word in words)


def window_counts(
    source: str, items: Sequence[SourceItem], boundary: datetime, *, truncated: bool
) -> WindowCounts:
    """Dated items on or after ``boundary`` (recent) and before it (previous).

    ``previous`` is ``None`` when the page was truncated: the earlier window
    may hold more than the page reached.
    """
    recent = sum(1 for item in items if item.published_at and item.published_at >= boundary)
    earlier = sum(1 for item in items if item.published_at and item.published_at < boundary)
    return WindowCounts(source=source, recent=recent, previous=None if truncated else earlier)


def volume_windows(series: DailySeries, *, window_days: int, until: datetime) -> VolumeWindows:
    """The last full ``window_days`` days against the full windows before them.

    Today is left out (a partial day reads as a dip for every topic alike).
    ``baseline`` is ``None`` with fewer than ``MIN_BASELINE_WINDOWS`` earlier
    windows: a single earlier week is a comparison, not a baseline.
    """
    today = until.date()
    days = [(d, v) for d, v in zip(series.dates, series.values, strict=True) if d < today]
    values = [v for _, v in days]
    recent = sum(values[-window_days:]) if len(values) >= window_days else float(sum(values))
    earlier = values[:-window_days] if len(values) > window_days else []
    windows = [
        sum(earlier[end - window_days : end])
        for end in range(len(earlier), window_days - 1, -window_days)
    ]
    baseline = statistics.fmean(windows) if len(windows) >= MIN_BASELINE_WINDOWS else None
    return VolumeWindows(
        source=series.source,
        recent=float(recent),
        baseline=round(baseline, 2) if baseline is not None else None,
        baseline_windows=len(windows),
    )


def video_metrics(
    source: str,
    items: Sequence[SourceItem],
    boundary: datetime,
    now: datetime,
    *,
    truncated: bool,
) -> VideoMetrics:
    recent = [i for i in items if i.published_at and i.published_at >= boundary]
    earlier = [i for i in items if i.published_at and i.published_at < boundary]
    per_day = [
        item.engagement["views"] / max(1.0, (now - item.published_at).total_seconds() / 86400)
        for item in recent
        if item.published_at and "views" in item.engagement
    ]
    return VideoMetrics(
        source=source,
        videos=len(items),
        recent_uploads=len(recent),
        previous_uploads=None if truncated else len(earlier),
        median_views_per_day=round(statistics.median(per_day), 1) if per_day else None,
    )


def is_question(text: str, *, asked: bool = True) -> bool:
    """A question someone asked.

    A search query that opens with "how", "does", "is"…
    is a question even without a question mark. A headline is not: "What the
    new research says about creatine" is an explainer, so for headlines
    (``asked=False``) only a trailing "?" counts.
    """
    text = text.strip()
    return text.endswith("?") or (asked and bool(_QUESTION_START.match(text)))


def questions_in(
    asked: Iterable[str], headlines: Iterable[str] = (), limit: int | None = None
) -> list[str]:
    """Distinct questions: from what people asked first, then from headlines."""
    seen: set[str] = set()
    found: list[str] = []
    candidates = [(text, True) for text in asked] + [(text, False) for text in headlines]
    for text, is_asked in candidates:
        if not is_question(text, asked=is_asked):
            continue
        key = query_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(text.strip())
        if limit is not None and len(found) >= limit:
            break
    return found


#: Where a title is something a person asked rather than an outlet published.
_ASKED_TYPES = frozenset({SourceType.SEARCH})


def split_questions(
    queries: Iterable[str], items: Iterable[SourceItem]
) -> tuple[list[str], list[str]]:
    """Texts people asked (queries, searches, posts) and texts outlets published."""
    asked = list(queries)
    headlines: list[str] = []
    for item in items:
        (asked if item.source_type in _ASKED_TYPES else headlines).append(item.title)
    return asked, headlines


def is_wellness_text(text: str) -> bool:
    return any(singular(w) in _WELLNESS_SINGULAR for w in normalize_query(text).split())


def specific_subject(subject: str, generic_keys: frozenset[str]) -> bool:
    words = normalize_query(subject).split()
    key = query_key(subject)
    return (
        0 < len(words) <= 4
        and subject.strip().lower() not in _VAGUE_SUBJECTS
        and key not in generic_keys
    )


# --- the whole topic --------------------------------------------------------


@dataclass
class TopicInputs:
    """What ``build_metrics`` needs about the run, beyond the topic itself."""

    items: Sequence[SourceItem]
    stories: Sequence[Story]
    story_of: dict[int, int]
    window_start: datetime
    window_end: datetime
    window_days: int
    #: Optional sub-signals whose source this run has (``scoring.OPTIONAL_SUB_SIGNALS``).
    applicable: set[str]
    #: Source types of discovery sources that answered at all this run.
    discovery_types: set[SourceType]
    #: News-like discovery sources that answered (RSS, GDELT, Google News).
    news_discovery_sources: int
    duplicate_threshold: float
    generic_keys: frozenset[str] = frozenset()


def build_metrics(
    *,
    run: TopicInputs,
    item_ids: Sequence[int],
    subject: str,
    outcome: str,
    queries: Sequence[str],
    search: SearchMetrics | None,
    enrichment: dict[str, SourceEnrichment],
    history: HistoryMetrics | None,
    novelty_similarity: float | None,
) -> TopicMetrics:
    items = run.items
    own = [i for i in item_ids if 0 <= i < len(items)]
    documents = [i for i in own if items[i].url is not None]
    current = [i for i in documents if _in_window(items[i], run.window_start)]

    # Per story, the topic's *own* earliest item names the outlet: a story's
    # representative may be another topic's copy of a shared wire headline.
    reporting_stories: dict[int, Story] = {}
    lead_of: dict[int, int] = {}

    def _when(index: int) -> tuple[datetime, int]:
        return (items[index].published_at or run.window_end, index)

    for index in sorted(current, key=_when):
        story_id = run.story_of.get(index)
        if story_id is None or items[index].source_type not in REPORTING_TYPES:
            continue
        reporting_stories.setdefault(story_id, run.stories[story_id])
        lead_of.setdefault(story_id, index)
    publishers = {
        items[lead].publisher_domain or f"story:{story_id}"
        for story_id, lead in lead_of.items()
    }

    types = {items[i].source_type.value for i in current}
    if search is not None or any(items[i].source_type is SourceType.SEARCH for i in own):
        types.add(SourceType.SEARCH.value)
    for result in enrichment.values():
        if result.video and result.video.recent_uploads:
            types.add(SourceType.VIDEO.value)

    available = {t.value for t in run.discovery_types}
    available.update(r.source_type.value for r in enrichment.values() if r.answered)
    news_answered = sum(
        1 for r in enrichment.values() if r.answered and r.source_type in REPORTING_TYPES
    )
    coverage_measured = news_answered >= 1 or run.news_discovery_sources >= 2

    dated = [
        when
        for i in current
        if (when := items[i].published_at) is not None and when <= run.window_end
    ]
    ages = [(run.window_end - when).total_seconds() / 86400 for when in dated if when]
    asked, headlines = split_questions(queries, (items[i] for i in own))
    texts = [*asked, *headlines]
    on_topic = [
        is_wellness_text(text) or bool(subject and subject.lower() in text.lower())
        for text in texts
    ]

    return TopicMetrics(
        window_days=run.window_days,
        observed_at=run.window_end,
        applicable=sorted(run.applicable | _history_applicable(history)),
        stories=len(reporting_stories),
        independent_publishers=len(publishers),
        syndicated_copies=sum(len(s.item_ids) - 1 for s in reporting_stories.values()),
        source_types=sorted(types),
        source_types_available=sorted(available),
        coverage_measured=coverage_measured,
        search=search,
        news_volume=next((r.volume for r in enrichment.values() if r.volume), None),
        news_counts=[r.counts for r in enrichment.values() if r.counts is not None],
        video=next((r.video for r in enrichment.values() if r.video), None),
        history=history,
        questions=len(questions_in(asked, headlines)),
        dated_items=len(dated),
        median_age_days=round(statistics.median(ages), 2) if ages else None,
        novelty_similarity=novelty_similarity,
        duplicate_threshold=run.duplicate_threshold,
        on_topic_share=round(sum(on_topic) / len(on_topic), 4) if on_topic else None,
        specific_subject=specific_subject(subject, run.generic_keys),
        has_outcome=bool(outcome.strip()),
    )


def _in_window(item: SourceItem, start: datetime) -> bool:
    """Undated items count as current: they came from a window-bounded query."""
    return item.published_at is None or item.published_at >= start


def _history_applicable(history: HistoryMetrics | None) -> set[str]:
    names: set[str] = set()
    if history is None:
        return names
    if history.previous_observations >= 1:
        names.add("history")
    if history.system_runs >= 1:
        names.add("newness")
    return names


def boundary(window_end: datetime, window_days: int) -> datetime:
    return window_end - timedelta(days=window_days)
