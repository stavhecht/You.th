"""From collected items to candidate topics: group, order, triage, attribute.

Three kinds of input become one list of query groups (``QueryCluster``):

* **rising searches** from Google Trends, filtered exactly as before
  (``normalize.merge_exact``: shopping intent, off-topic spam, a seed's own
  word);
* **headline phrases** (``phrases.py``), already required to contain a
  wellness word and to have been reported by two independent outlets;
* **manual queries** a trigger asked about. They skip the off-topic filter —
  someone asked — but still go through triage, which can still reject them.

Groups with the same key merge; then groups whose strongest phrasing embeds
close merge (``normalize.merge_similar``, at the threshold measured on short
queries — which is why headlines are reduced to phrases first). The order is a
deterministic pre-score, because the cap before triage keeps the first ones:
breakouts, then how many kinds of source, then how many outlets, then rising
percent.

After triage, each candidate is mapped back to the items behind its queries,
which is what makes every number shown later traceable to collected records.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.research.contracts import CandidateTopic, Category, RawTrend
from app.research.normalize import (
    _FILLER,
    QueryCluster,
    is_not_a_topic,
    merge_exact,
    normalize_query,
    off_topic,
    query_key,
    singular,
    trend_strength,
)
from app.research.trends.contracts import SourceItem, SourceType
from app.research.trends.documents import StoryGrouping
from app.research.trends.phrases import GENERIC_WORDS, Phrase

HEADLINES = "headlines"
MANUAL = "manual"


@dataclass
class Grouped:
    """Query groups ready for triage, and where each query came from."""

    clusters: list[QueryCluster]
    #: Query text -> indexes into the run's items.
    query_items: dict[str, set[int]] = field(default_factory=dict)
    #: Cluster id -> up to two headlines, as triage context.
    examples: dict[int, list[str]] = field(default_factory=dict)
    off_topic: list[RawTrend] = field(default_factory=list)


def trend_queries(items: Sequence[SourceItem]) -> tuple[list[RawTrend], dict[str, set[int]]]:
    """Rising searches as ``RawTrend``s, and the items that reported each."""
    trends: list[RawTrend] = []
    sources: dict[str, set[int]] = defaultdict(set)
    for index, item in enumerate(items):
        if item.source_type is not SourceType.SEARCH:
            continue
        rising = item.metrics.get("rising_percent")
        trends.append(
            RawTrend(
                query=item.title,
                seed=item.seed or "",
                category=item.category or Category.OTHER,
                source=item.source_name,
                rising_percent=int(rising) if rising is not None else None,
                is_breakout=bool(item.metrics.get("breakout")),
                depth=int(item.metrics.get("depth", 0)),
            )
        )
        sources[item.title].add(index)
    return trends, sources


def group(
    items: Sequence[SourceItem],
    grouping: StoryGrouping,
    phrases: list[Phrase],
    manual: Sequence[str],
) -> Grouped:
    """Every input as query groups, merged by key, in pre-score order."""
    trends, query_items = trend_queries(items)
    dropped = off_topic(trends)
    clusters = merge_exact(trends)
    by_key = {query_key(cluster.representative.query): cluster for cluster in clusters}

    def fold(trend: RawTrend) -> None:
        key = query_key(trend.query)
        if not key:
            return
        cluster = by_key.get(key)
        if cluster is None:
            cluster = by_key[key] = QueryCluster(cluster_id=len(by_key))
        cluster.trends.append(trend)

    for phrase in phrases:
        if is_not_a_topic(phrase.text):
            continue
        fold(
            RawTrend(
                query=phrase.text,
                seed=phrase.seed(items),
                category=phrase.category(items),
                source=HEADLINES,
            )
        )
        query_items.setdefault(phrase.text, set()).update(phrase.item_ids)
    for text in manual:
        text = " ".join(text.split())
        if text:
            fold(RawTrend(query=text, seed=MANUAL, category=Category.OTHER, source=MANUAL))
            query_items.setdefault(text, set())

    ordered = sorted(
        by_key.values(), key=lambda c: _pre_score(c, query_items, items, grouping), reverse=True
    )
    for index, cluster in enumerate(ordered):
        cluster.cluster_id = index
    return Grouped(clusters=ordered, query_items=dict(query_items), off_topic=dropped)


def _pre_score(
    cluster: QueryCluster,
    query_items: dict[str, set[int]],
    items: Sequence[SourceItem],
    grouping: StoryGrouping,
) -> tuple[int, int, int, int, float]:
    members = set().union(*(query_items.get(q, set()) for q in cluster.queries))
    kinds = {items[i].source_type for i in members}
    outlets = {
        items[grouping.stories[grouping.story_of[i]].representative].publisher_domain
        for i in members
        if i in grouping.story_of
    }
    manual = any(t.source == MANUAL for t in cluster.trends)
    breakout = any(t.is_breakout for t in cluster.trends)
    strongest = max((trend_strength(t) for t in cluster.trends), default=0.0)
    return (int(manual), int(breakout), len(kinds), len(outlets - {None}), strongest)


def examples_for(
    clusters: list[QueryCluster],
    query_items: dict[str, set[int]],
    items: Sequence[SourceItem],
    grouping: StoryGrouping,
) -> dict[int, list[str]]:
    """Two headlines per headline-derived group, most widely reported first."""
    examples: dict[int, list[str]] = {}
    for cluster in clusters:
        if not any(t.source == HEADLINES for t in cluster.trends):
            continue
        stories: dict[int, int] = defaultdict(int)
        for query in cluster.queries:
            for index in query_items.get(query, ()):
                if index in grouping.story_of:
                    stories[grouping.story_of[index]] += 1
        best = sorted(stories, key=lambda s: (-stories[s], s))[:2]
        examples[cluster.cluster_id] = [
            items[grouping.stories[s].representative].title for s in best
        ]
    return examples


def attribute(candidates: list[CandidateTopic], query_items: dict[str, set[int]]) -> int:
    """Give each candidate the items behind its queries; return queries ignored.

    Only through queries that share a word with the topic's subject or
    outcome. Triage groups queries, and a 7B model sometimes groups unrelated
    ones: measured 2026-10-12, "coneflower for immune support" was handed a
    phrase about carbohydrates, and its supporting sources were two articles
    about carbs. The queries stay on the candidate as triage grouped them; they
    just do not count as its coverage.
    """
    ignored = 0
    for candidate in candidates:
        topic_words = _words(candidate.subject) | _words(candidate.outcome)
        found: set[int] = set()
        for query in candidate.queries:
            if topic_words and not (_words(query) & topic_words):
                ignored += 1
                continue
            found.update(query_items.get(query, ()))
        candidate.item_ids = sorted(found)
    return ignored


def too_broad(candidate: CandidateTopic, seed_keys: frozenset[str]) -> bool:
    """A bare subject that names a category ("protein", "weight loss") is no article.

    The rule the raw queries already follow (a seed's own word is the
    category, not a trend in it), applied to what triage named: measured
    2026-10-12, "protein", "strength training" and "weight loss" were ranked.
    An outcome makes it an angle ("protein for older adults"), which is kept.
    """
    if candidate.outcome.strip():
        return False
    if query_key(candidate.subject) in seed_keys:
        return True
    words = normalize_query(candidate.subject).split()
    return bool(words) and all(
        singular(word) in GENERIC_WORDS or word in GENERIC_WORDS or word in _FILLER
        for word in words
    )


def _words(text: str) -> set[str]:
    return {singular(w) for w in normalize_query(text).split() if w not in _FILLER}
