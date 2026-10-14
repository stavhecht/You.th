"""The Trend Search Agent: what people are getting interested in, and how much.

```
load_config -> collect_sources -> normalize_items -> cluster_topics
  -> check_history -> enrich_topics -> score_topics -> select_trends
```

* **collect_sources** sweeps every enabled source that can discover — Google
  Trends' rising queries per seed, publisher RSS feeds, GDELT and Google News
  per category — concurrently, each at its own pace and
  within a per-source request budget and a phase deadline.
* **normalize_items** groups the items into stories (``documents.py``): exact
  URL, canonical URL, headline, near-duplicate headline. Syndicated copies
  become one story.
* **cluster_topics** turns rising queries and headline phrases into query
  groups, triages them with the local model (subject, outcome, category,
  wellness or not), and maps each candidate back to its items.
* **check_history** discards what was already written or decided (novelty),
  and gives each candidate its persistent identity and history.
* **enrich_topics** searches each source for the strongest candidates: news
  counts per window, GDELT's three-month volume, Google Trends' interest
  series, web and YouTube.
* **score_topics** measures and scores each candidate (``metrics.py``,
  ``scoring.py``); a candidate whose score rests on too little is discarded
  with the reason, never ranked on a guess.
* **select_trends** ranks the eligible topics and freezes the trend output.
  It marks the best ``evidence_pool_size`` for the downstream evidence check
  and **selects nothing itself**: a trend is not yet a proposal.

**Nothing here judges scientific evidence or calls PubMed, Europe PMC, the
extraction model or the article pipeline** — ``test_trend_scope.py`` holds the
imports to that. The one model call is triage, which reads what a phrase
*means*, never whether a claim is true.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import statistics
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.llm.embeddings.base import EmbeddingProvider
from app.research import normalize
from app.research.contracts import (
    SOURCE_NAMES,
    CandidateTopic,
    NewsArticle,
    NewsSignal,
    NoveltySignal,
    PastTopic,
    ProviderStatus,
    ResearchStage,
    ResearchState,
    TrendSignal,
    WebResult,
)
from app.research.providers.base import ProviderUnavailable
from app.research.runtime import Step, settle
from app.research.scoring import select, web_signal
from app.research.seeds import seeds_for
from app.research.trends import cluster, explain, metrics, phrases, scoring
from app.research.trends.contracts import (
    REPORTING_TYPES,
    Capability,
    HistoryMetrics,
    SearchMetrics,
    SourceBatch,
    SourceItem,
    SourceQuery,
    SourceType,
)
from app.research.trends.documents import StoryGrouping, group_stories
from app.research.trends.history import (
    STATUS_LOOKBACK_DAYS,
    TopicRef,
    TopicStore,
    history_metrics,
    repeat_reason,
    topic_key,
)
from app.research.trends.sources.base import (
    DiscoveryPlan,
    RequestBudget,
    TrendSource,
    retries_of,
)
from app.research.triage import TriageClient, triage_or_fallback

logger = logging.getLogger(__name__)

#: A source that fails this many requests in a row is not asked again this run.
MAX_CONSECUTIVE_FAILURES = 3
#: Groups handed to triage, as a multiple of ``max_candidates``: some will be
#: judged not to be wellness topics.
TRIAGE_HEADROOM = 1.5
#: Queries per Google Trends interest request; the anchor makes five, its limit.
TRENDS_BATCH = 4
#: Ranked topics kept in the frozen result.
RESULT_TOPICS = 25
#: Results asked of each source per enrichment search.
ENRICH_RESULTS = {"google_news": 100, "news": 100, "web": 20, "youtube": 50}
#: GDELT's volume series reaches back this far for the baseline.
VOLUME_LOOKBACK_DAYS = 90
_NEWS_LIKE = frozenset({SourceType.NEWS, SourceType.FEED})
_KIND_VERB = {"article": "written about", "promoted": "promoted", "dismissed": "dismissed"}
#: Nodes in this fragment, for the graph's recursion limit.
NODES = 8


@dataclass
class TrendDeps:
    """Everything the trend agent talks to. Only built, enabled sources are listed."""

    sources: list[TrendSource]
    #: Enabled sources that could not be built (a missing key): name -> why.
    unavailable: dict[str, str]
    triage: TriageClient | None
    embedder: EmbeddingProvider | None
    #: Articles written and topics decided, for novelty.
    history: Callable[[datetime], Awaitable[list[PastTopic]]]
    #: Persistent topic identity and trend history. None: no history (dry run).
    topics: TopicStore | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


class _Calls:
    """One run's bookkeeping for source calls: budget, failures, circuit breaker."""

    def __init__(self, state: ResearchState, budget: RequestBudget) -> None:
        self._state = state
        self._budget = budget
        self._failures: dict[str, int] = {}
        self.down: dict[str, str] = {}
        self.spent: set[str] = set()

    def available(self, name: str) -> bool:
        return name not in self.down and name not in self.spent

    async def run(self, name: str, call: Callable[[], Awaitable[Any]]) -> Any | None:
        if not self.available(name):
            return None
        if not self._budget.take(name):
            self.spent.add(name)
            return None
        status = self._state.provider(name)
        status.calls += 1
        try:
            result = await call()
        except ProviderUnavailable as exc:
            status.failures += 1
            status.detail = str(exc)[:300]
            failures = self._failures[name] = self._failures.get(name, 0) + 1
            if exc.permanent:
                self.down[name] = f"stopped asking: {exc}"
            elif failures >= MAX_CONSECUTIVE_FAILURES:
                self.down[name] = f"stopped asking after {failures} failures in a row"
            return None
        self._failures[name] = 0
        return result


class TrendSearchNodes:
    """The trend agent's steps, for ``research/graph.py`` to wire."""

    def __init__(
        self, deps: TrendDeps, *, extra_providers: dict[str, object | None] | None = None
    ) -> None:
        self._deps = deps
        #: Dependencies of later stages (the evidence check), listed in the
        #: run's provider status from the start.
        self._extra = dict(extra_providers or {})
        self._sources = {source.name: source for source in deps.sources}
        self._calls: _Calls | None = None
        self._histories: dict[str, HistoryMetrics] = {}
        self._enrichment: dict[str, dict[str, metrics.SourceEnrichment]] = {}
        self._started: datetime | None = None

    def steps(self) -> list[Step]:
        return [
            ("load_config", ResearchStage.LOADING_CONFIG, self.load_config),
            ("collect_sources", ResearchStage.COLLECTING_SOURCES, self.collect_sources),
            ("normalize_items", ResearchStage.NORMALIZING_ITEMS, self.normalize_items),
            ("cluster_topics", ResearchStage.CLUSTERING_TOPICS, self.cluster_topics),
            ("check_history", ResearchStage.CHECKING_HISTORY, self.check_history),
            ("enrich_topics", ResearchStage.ENRICHING_TOPICS, self.enrich_topics),
            ("score_topics", ResearchStage.SCORING_TOPICS, self.score_topics),
            ("select_trends", ResearchStage.SELECTING_TRENDS, self.select_trends),
        ]

    # --- nodes ---------------------------------------------------------------

    async def load_config(self, state: ResearchState) -> dict[str, Any]:
        config = state.config
        now = self._deps.now()
        self._started = now
        state.window_end = now
        state.window_start = now - timedelta(days=config.trend_window_days)
        self._calls = _Calls(
            state,
            RequestBudget(
                config.max_requests_per_source, {"youtube": config.youtube_max_searches}
            ),
        )
        self._histories = {}
        self._enrichment = {}
        enabled = set(config.enabled_sources)
        for name in SOURCE_NAMES:
            if name in self._sources:
                state.provider_status[name] = ProviderStatus(status="ok")
            elif name in self._deps.unavailable:
                state.provider_status[name] = ProviderStatus(
                    status="unavailable", detail=self._deps.unavailable[name]
                )
            elif name not in enabled:
                state.provider_status[name] = ProviderStatus(
                    status="skipped", detail="not enabled for this run"
                )
            else:
                state.provider_status[name] = ProviderStatus(
                    status="unavailable", detail="not configured"
                )
        for name, dependency in (
            ("triage", self._deps.triage),
            ("embeddings", self._deps.embedder),
            *self._extra.items(),
        ):
            state.provider_status[name] = ProviderStatus(
                status="ok" if dependency is not None else "unavailable",
                detail=None if dependency is not None else "not configured",
            )
        if self._deps.topics is not None:
            since = now - timedelta(days=STATUS_LOOKBACK_DAYS)
            state.history_runs = len(
                await self._deps.topics.recent_runs(since, exclude_run=state.run_id)
            )
        return {
            "sources": sorted(self._sources),
            "categories": [c.value for c in config.categories],
            "window_days": config.trend_window_days,
            "target": config.target_article_count,
            "history_runs": state.history_runs,
        }

    async def collect_sources(self, state: ResearchState) -> dict[str, Any]:
        config = state.config
        assert state.window_start is not None and state.window_end is not None
        plan = DiscoveryPlan(
            seeds=tuple(seeds_for(config.categories)),
            categories=tuple(config.categories),
            manual=tuple(config.queries),
            since=state.window_start,
            until=state.window_end,
            geo=config.geo,
            language=config.language,
        )
        discovering = [
            s for s in self._sources.values() if Capability.DISCOVER in s.capabilities
        ]
        results: dict[str, list[SourceBatch]] = {s.name: [] for s in discovering}

        async def sweep(source: TrendSource) -> None:
            discovery = getattr(source, "discovery", None)
            if discovery is None:
                return
            for query in discovery(plan):
                batch = await self._ask(source, query)
                if batch is not None:
                    results[source.name].append(batch)
                elif not self._calls_for().available(source.name):
                    break
            if source.name == "google_trends" and config.expand_top > 0:
                await self._expand_trends(source, results[source.name], plan, config.expand_top)

        timed_out = await self._concurrently(
            {s.name: sweep(s) for s in discovering}, config.collect_deadline_seconds
        )

        collected = outside = 0
        for source in discovering:
            for batch in results[source.name]:
                for item in batch.items:
                    if item.published_at is not None and item.published_at < state.window_start:
                        outside += 1
                        continue
                    state.items.append(item)
                    state.provider(source.name).items += 1
                    collected += 1
        self._settle_all(state, timed_out, phase="collection")

        answered = [
            name
            for name in results
            if state.provider(name).calls > state.provider(name).failures
        ]
        state.counts.update(
            items_collected=collected,
            items_outside_window=outside,
            items_by_source={name: state.provider(name).items for name in results},
        )
        if not answered and not config.queries:
            state.failed = "no_trend_data: no source could be swept " + (
                f"({'; '.join(self._errors(state))})"
                if self._errors(state)
                else "(none enabled and configured)"
            )
        elif not state.items and not config.queries:
            state.notes.append(
                f"the sources answered but returned nothing in the last "
                f"{config.trend_window_days} days"
            )
        return {
            "items": collected,
            "outside_window": outside,
            "answered": answered,
            "deadline_hit": sorted(timed_out),
        }

    async def normalize_items(self, state: ResearchState) -> dict[str, Any]:
        grouping = await asyncio.to_thread(group_stories, list(state.items))
        state.stories = grouping.stories
        state.story_of = grouping.story_of
        state.counts.update(
            documents=len(grouping.story_of),
            stories_discovered=len(grouping.stories),
            duplicates_removed=dict(grouping.removed),
        )
        return {
            "documents": len(grouping.story_of),
            "stories": len(grouping.stories),
            "duplicates_removed": grouping.removed,
        }

    async def cluster_topics(self, state: ResearchState) -> dict[str, Any]:
        config = state.config
        grouping = StoryGrouping(state.stories, state.story_of, {})
        seeds = seeds_for(config.categories)
        known_subjects: list[str] = []
        if self._deps.topics is not None:
            known = await self._deps.topics.known(
                self._deps.now() - timedelta(days=config.novelty_lookback_days)
            )
            known_subjects = [topic.label for topic in known]
        vocabulary = phrases.lexicon(
            [seed for seed, _ in seeds], known_subjects, config.queries
        )
        trend_keys = frozenset(
            normalize.query_key(item.title)
            for item in state.items
            if item.source_type is SourceType.SEARCH
        )
        found = phrases.extract_phrases(
            state.items,
            grouping,
            vocabulary,
            trend_keys=trend_keys,
            seed_keys=frozenset(normalize.query_key(seed) for seed, _ in seeds),
            min_publishers=config.phrase_min_publishers,
        )
        grouped = cluster.group(state.items, grouping, found, config.queries)
        if grouped.off_topic:
            dropped = ", ".join(sorted({t.query for t in grouped.off_topic})[:3])
            state.notes.append(
                f"{len(grouped.off_topic)} rising queries were not about health or their "
                f"seed and were dropped (e.g. {dropped})"
            )
        clusters = grouped.clusters
        exact = len(clusters)
        embedder = self._deps.embedder
        if embedder is not None and len(clusters) > 1:
            try:
                vectors = await embedder.embed_documents(
                    [c.representative.query for c in clusters]
                )
                clusters = normalize.merge_similar(clusters, vectors, config.cluster_threshold)
            except Exception as exc:  # an embedding outage costs merging, not the run
                state.provider("embeddings").status = "failed"
                state.provider("embeddings").detail = str(exc)
                state.notes.append(f"similar topics were not merged: embeddings failed ({exc})")
        clusters = clusters[: int(config.max_candidates * TRIAGE_HEADROOM)]
        for index, group in enumerate(clusters):
            group.cluster_id = index
        examples = cluster.examples_for(clusters, grouped.query_items, state.items, grouping)

        candidates, note = await triage_or_fallback(
            self._deps.triage, clusters, allowed=config.categories, examples=examples
        )
        if note:
            state.notes.append(note)
            if self._deps.triage is not None:
                failed = note.startswith("triage failed")
                state.provider("triage").status = "failed" if failed else "partial"
                state.provider("triage").detail = note
        for candidate in [c for c in candidates if c.live][config.max_candidates :]:
            candidate.discard("beyond the candidate cap")
        ignored = cluster.attribute(candidates, grouped.query_items)
        seed_keys = frozenset(normalize.query_key(seed) for seed, _ in seeds)
        for candidate in candidates:
            if not candidate.live:
                continue
            if normalize.is_not_a_topic(candidate.canonical_topic) or normalize.is_not_a_topic(
                candidate.subject
            ):
                candidate.discard("shopping or product intent, not a health question")
            elif cluster.too_broad(candidate, seed_keys):
                candidate.discard(
                    f"too broad for one article: {candidate.subject!r} names a category"
                )
        self._attribute_mentions(state, candidates)
        self._rising(state, candidates)
        state.candidates = candidates
        state.counts.update(
            phrases=len(found),
            clusters=len(clusters),
            candidates=len(state.live()),
            grouped_queries_ignored=ignored,
        )
        if not state.live():
            state.notes.append("no candidate topics: nothing collected was a wellness topic")
        return {
            "phrases": len(found),
            "clusters_exact": exact,
            "clusters": len(clusters),
            "candidates": len(state.live()),
            "not_wellness_or_out_of_scope": sum(1 for c in candidates if not c.live),
        }

    async def check_history(self, state: ResearchState) -> dict[str, Any]:
        duplicates = await self._novelty(state)
        resolved = await self._identity(state)
        return {"duplicates": duplicates, "topics_resolved": resolved}

    async def enrich_topics(self, state: ResearchState) -> dict[str, Any]:
        config = state.config
        assert state.window_start is not None and state.window_end is not None
        window_end: datetime = state.window_end
        pool = sorted(
            state.live(), key=lambda c: self._discovery_strength(state, c), reverse=True
        )[: config.enrich_pool_size]
        for candidate in pool:
            candidate.in_enrich_pool = True
            self._enrichment[candidate.topic_id] = {}
        boundary = state.window_start
        since = boundary - timedelta(days=config.trend_window_days)
        found: dict[str, list[tuple[CandidateTopic, list[SourceItem]]]] = {}

        async def search(source: TrendSource) -> None:
            out = found.setdefault(source.name, [])
            for candidate in pool:
                query = self._topic_query(state, candidate, since)
                query = SourceQuery(
                    **{**_fields(query), "max_results": ENRICH_RESULTS.get(source.name, 50)}
                )
                batch = await self._ask(source, query)
                if batch is None:
                    if not self._calls_for().available(source.name):
                        return
                    continue
                relevant = [
                    i for i in batch.items if metrics.mentions_subject(i, candidate.subject)
                ]
                result = metrics.SourceEnrichment(
                    source=source.name,
                    source_type=source.source_type,
                    answered=True,
                    items=relevant,
                )
                if source.source_type is SourceType.VIDEO:
                    result.video = metrics.video_metrics(
                        source.name,
                        relevant,
                        boundary,
                        window_end,
                        truncated=batch.truncated,
                    )
                elif source.source_type in _NEWS_LIKE:
                    result.counts = metrics.window_counts(
                        source.name, relevant, boundary, truncated=batch.truncated
                    )
                    if source.name == "news":
                        candidate.news = _news_signal(batch, relevant, boundary)
                elif source.source_type is SourceType.WEB:
                    candidate.web = web_signal(
                        [
                            WebResult(
                                title=i.title, url=i.url or "", snippet=i.description or ""
                            )
                            for i in batch.items
                            if i.url
                        ],
                        source=batch.items[0].source_name if batch.items else source.name,
                        subject=candidate.subject,
                        authoritative=config.authoritative_domains,
                    )
                self._enrichment[candidate.topic_id][source.name] = result
                out.append((candidate, relevant))

        async def volume(source: TrendSource) -> None:
            volume_of = getattr(source, "volume", None)
            if volume_of is None:
                return
            for candidate in pool:
                query = self._topic_query(
                    state, candidate, window_end - timedelta(days=VOLUME_LOOKBACK_DAYS)
                )
                series = await self._call(source.name, functools.partial(volume_of, query))
                if series is None:
                    if not self._calls_for().available(source.name):
                        return
                    continue
                self._enrichment[candidate.topic_id][source.name] = metrics.SourceEnrichment(
                    source=source.name,
                    source_type=source.source_type,
                    answered=True,
                    volume=metrics.volume_windows(
                        series, window_days=config.trend_window_days, until=window_end
                    ),
                )

        tasks: dict[str, Awaitable[None]] = {}
        for source in self._sources.values():
            if Capability.INTEREST in source.capabilities:
                tasks[source.name] = self._interest(state, source, pool)
            elif Capability.VOLUME in source.capabilities:
                tasks[source.name] = volume(source)
            elif Capability.SEARCH in source.capabilities:
                tasks[source.name] = search(source)
        timed_out = await self._concurrently(tasks, config.enrich_deadline_seconds)

        added = 0
        for name in self._sources:
            for candidate, relevant in found.get(name, []):
                start = len(state.items)
                state.items.extend(relevant)
                candidate.item_ids = sorted(
                    {*candidate.item_ids, *range(start, len(state.items))}
                )
                state.provider(name).items += len(relevant)
                added += len(relevant)
        grouping = await asyncio.to_thread(group_stories, list(state.items))
        state.stories, state.story_of = grouping.stories, grouping.story_of
        self._settle_all(state, timed_out, phase="enrichment")
        state.counts.update(
            enriched=len(pool),
            enrichment_items=added,
            stories=len(grouping.stories),
            duplicates_removed=dict(grouping.removed),
        )
        return {"enriched": len(pool), "items_added": added, "deadline_hit": sorted(timed_out)}

    async def score_topics(self, state: ResearchState) -> dict[str, Any]:
        config = state.config
        assert state.window_start is not None and state.window_end is not None
        discovery_types = {
            self._sources[name].source_type
            for name in self._sources
            if Capability.DISCOVER in self._sources[name].capabilities
            and state.provider(name).calls > state.provider(name).failures
        }
        news_discovery = sum(
            1
            for name, source in self._sources.items()
            if Capability.DISCOVER in source.capabilities
            and source.source_type in _NEWS_LIKE
            and state.provider(name).calls > state.provider(name).failures
        )
        run = metrics.TopicInputs(
            items=state.items,
            stories=state.stories,
            story_of=state.story_of,
            window_start=state.window_start,
            window_end=state.window_end,
            window_days=config.trend_window_days,
            applicable=self._applicable(),
            discovery_types=discovery_types,
            news_discovery_sources=news_discovery,
            duplicate_threshold=config.duplicate_threshold,
            generic_keys=frozenset(normalize.query_key(w) for w in phrases.GENERIC_WORDS),
        )
        insufficient = repeats = 0
        for candidate in state.live():
            history = self._histories.get(candidate.topic_ref_id or "")
            topic_metrics = metrics.build_metrics(
                run=run,
                item_ids=candidate.item_ids,
                subject=candidate.subject,
                outcome=candidate.outcome,
                queries=candidate.queries,
                search=_search_metrics(candidate.trend),
                enrichment=self._enrichment.get(candidate.topic_id, {}),
                history=history,
                novelty_similarity=(
                    candidate.novelty.max_similarity if candidate.novelty else None
                ),
            )
            scores = scoring.score_from_metrics(
                topic_metrics, config.weights, min_data_coverage=config.min_data_coverage
            )
            candidate.metrics = topic_metrics
            candidate.scores = scores
            candidate.trend_status = scoring.trend_status(topic_metrics, scores)
            asked, headlines = metrics.split_questions(
                candidate.queries, (state.items[i] for i in candidate.item_ids)
            )
            candidate.key_questions = metrics.questions_in(asked, headlines, limit=5)
            if (
                candidate.reader_question
                and len(candidate.key_questions) < 5
                and not any(
                    normalize.query_key(q) == normalize.query_key(candidate.reader_question)
                    for q in candidate.key_questions
                )
            ):
                candidate.key_questions.append(candidate.reader_question)
            candidate.supporting = explain.supporting_sources(
                state.items,
                state.stories,
                state.story_of,
                candidate.item_ids,
                state.window_start,
            )
            if not candidate.in_enrich_pool:
                continue
            if scores.overall is None:
                candidate.discard(f"insufficient data: {scores.insufficient}")
                insufficient += 1
                continue
            reason = repeat_reason(
                topic_metrics,
                scores,
                now=state.window_end,
                cooloff_days=config.repeat_cooloff_days,
            )
            if reason is not None:
                candidate.discard(reason)
                repeats += 1
        state.counts.update(insufficient_data=insufficient, repeats_suppressed=repeats)
        return {
            "scored": len(state.live()) + insufficient + repeats,
            "insufficient_data": insufficient,
            "repeats_suppressed": repeats,
        }

    async def select_trends(self, state: ResearchState) -> dict[str, Any]:
        config = state.config
        eligible: list[CandidateTopic] = []
        for candidate in state.live():
            if not candidate.in_enrich_pool or candidate.scores.overall is None:
                continue
            reason = _ineligible(
                candidate, config.min_trend_score, config.min_independent_sources
            )
            if reason is not None:
                candidate.discard(reason)
                continue
            eligible.append(candidate)
        ranked = sorted(
            eligible,
            key=lambda c: scoring.rank_key(
                c.scores.overall, c.scores.components.get(scoring.COVERAGE), c.canonical_topic
            ),
        )
        for position, candidate in enumerate(ranked, start=1):
            candidate.rank = position
        cap = config.max_per_category if len(config.categories) > 1 else None
        pool = select(ranked, target=config.evidence_pool_size, max_per_category=cap)
        for candidate in pool:
            candidate.in_evidence_pool = True
        shortlist = pool[: config.target_article_count]
        if len(shortlist) < config.target_article_count:
            state.notes.append(
                f"{len(shortlist)} of {config.target_article_count} requested topics met "
                "the trend thresholds (minimum score, independent outlets, data coverage)"
            )
        state.counts.update(
            eligible=len(eligible),
            ranked=len(ranked),
            downstream_pool=len(pool),
            trend_shortlist=len(shortlist),
        )
        kept = ranked[:RESULT_TOPICS]
        kept += [c for c in pool if c not in kept]
        state.trend_summary = explain.build_result(
            state,
            kept,
            {c.topic_id for c in shortlist},
            started_at=self._started,
            completed_at=self._deps.now(),
        ).model_dump(mode="json")
        return {
            "eligible": len(eligible),
            "downstream_pool": len(pool),
            "trend_shortlist": [c.canonical_topic for c in shortlist],
        }

    # --- helpers -------------------------------------------------------------

    def _calls_for(self) -> _Calls:
        assert self._calls is not None, "load_config runs first"
        return self._calls

    async def _ask(self, source: TrendSource, query: SourceQuery) -> SourceBatch | None:
        result = await self._call(source.name, lambda: source.search(query))
        return result if isinstance(result, SourceBatch) else None

    async def _call(self, name: str, call: Callable[[], Awaitable[Any]]) -> Any | None:
        return await self._calls_for().run(name, call)

    async def _concurrently(
        self, tasks: dict[str, Awaitable[None]], deadline: float
    ) -> set[str]:
        """Run per-source tasks together; return the names cut off by the deadline."""
        if not tasks:
            return set()
        running = {name: asyncio.ensure_future(task) for name, task in tasks.items()}
        done, pending = await asyncio.wait(running.values(), timeout=max(1.0, deadline))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for name, task in running.items():
            if task in done and not task.cancelled() and task.exception() is not None:
                logger.error("trend source %s crashed", name, exc_info=task.exception())
        return {name for name, task in running.items() if task in pending}

    def _settle_all(self, state: ResearchState, timed_out: set[str], *, phase: str) -> None:
        calls = self._calls_for()
        for name, source in self._sources.items():
            status = state.provider(name)
            status.retries = retries_of(getattr(source, "http", None))
            errors = [status.detail] if status.failures and status.detail else []
            settle(status, errors)
            if name in calls.down and status.status == "ok":
                status.status = "partial"
            if name in calls.spent:
                if status.calls == 0:
                    status.status = "skipped"
                elif status.status == "ok":
                    status.status = "partial"
                status.detail = f"request budget spent during {phase}"
            if name in timed_out:
                status.status = "partial" if status.calls else "skipped"
                status.detail = f"{phase} deadline reached"
            if name in calls.down:
                status.detail = calls.down[name]

    @staticmethod
    def _errors(state: ResearchState) -> list[str]:
        return [
            f"{name}: {status.detail}"
            for name, status in state.provider_status.items()
            if status.detail and status.status in ("failed", "unavailable")
        ]

    async def _expand_trends(
        self,
        source: TrendSource,
        batches: list[SourceBatch],
        plan: DiscoveryPlan,
        top: int,
    ) -> None:
        rising = sorted(
            (item for batch in batches for item in batch.items),
            key=lambda i: i.metrics.get("rising_percent", 0.0),
            reverse=True,
        )
        seen: set[str] = set()
        for item in rising:
            if len(seen) >= top:
                break
            if item.title in seen:
                continue
            seen.add(item.title)
            query = SourceQuery(
                since=plan.since,
                until=plan.until,
                geo=plan.geo,
                language=plan.language,
                terms=(item.title,),
                seed=item.seed,
                category=item.category,
            )
            batch = await self._ask(source, query)
            if batch is None:
                continue
            for expanded in batch.items:
                expanded.metrics["depth"] = 1.0
                expanded.seed = item.seed
            batches.append(batch)

    @staticmethod
    def _attribute_mentions(state: ResearchState, candidates: list[CandidateTopic]) -> None:
        """Add every collected document naming the topic's subject and outcome.

        The phrases that formed a topic are a few exact wordings; an outlet
        writing "creatine may improve sleep" reported on the creatine-and-sleep
        topic whichever phrase triage grouped. Matching every word of the
        subject *and* of the outcome keeps one subject's other angles out.
        """
        documents = [
            (index, item) for index, item in enumerate(state.items) if item.url is not None
        ]
        for candidate in candidates:
            if not candidate.live:
                continue
            found = {
                index
                for index, item in documents
                if metrics.mentions_subject(item, candidate.subject)
                and (not candidate.outcome or metrics.mentions_subject(item, candidate.outcome))
            }
            if found:
                candidate.item_ids = sorted({*candidate.item_ids, *found})

    def _rising(self, state: ResearchState, candidates: list[CandidateTopic]) -> None:
        """Each candidate's strongest rising query, from items already collected."""
        by_query: dict[str, SourceItem] = {}
        for item in state.items:
            if item.source_type is not SourceType.SEARCH:
                continue
            best = by_query.get(item.title)
            if best is None or item.metrics.get("rising_percent", 0) > best.metrics.get(
                "rising_percent", 0
            ):
                by_query[item.title] = item
        for candidate in candidates:
            hits = [by_query[q] for q in candidate.queries if q in by_query]
            if not hits:
                continue
            best = max(hits, key=lambda i: i.metrics.get("rising_percent", 0.0))
            rising = best.metrics.get("rising_percent")
            candidate.trend = TrendSignal(
                source=best.source_name,
                rising_percent=int(rising) if rising is not None else None,
                is_breakout=bool(best.metrics.get("breakout")),
                growth_source="rising_percent" if rising else None,
                related_queries=candidate.queries[1:6],
            )

    async def _novelty(self, state: ResearchState) -> int:
        embedder = self._deps.embedder
        live = state.live()
        if embedder is None or not live:
            return 0
        since = self._deps.now() - timedelta(days=state.config.novelty_lookback_days)
        history = await self._deps.history(since)
        if not history:
            for candidate in live:
                candidate.novelty = NoveltySignal(max_similarity=0.0)
            return 0
        try:
            vectors = await embedder.embed_documents(
                [c.canonical_topic for c in live] + [past.text for past in history]
            )
        except Exception as exc:
            state.provider("embeddings").status = "failed"
            state.provider("embeddings").detail = str(exc)
            state.notes.append(f"novelty was not measured: embeddings failed ({exc})")
            return 0
        own, past = vectors[: len(live)], vectors[len(live) :]
        duplicates = 0
        for candidate, vector in zip(live, own, strict=True):
            similarities = [normalize.cosine(vector, other) for other in past]
            best = max(range(len(history)), key=lambda i: similarities[i])
            candidate.novelty = NoveltySignal(
                max_similarity=round(similarities[best], 4), nearest=history[best].text
            )
            if similarities[best] >= state.config.duplicate_threshold:
                duplicates += 1
                item = history[best]
                when = f" on {item.decided_at.date()}" if item.decided_at else ""
                candidate.discard(f"already {_KIND_VERB[item.kind]}{when}: {item.text!r}")
        return duplicates

    async def _identity(self, state: ResearchState) -> int:
        """Persistent ids and history for live candidates. Upserted before the
        next checkpoint writes candidates that reference them."""
        store = self._deps.topics
        live = state.live()
        if store is None or not live:
            return 0
        now = self._deps.now()
        keys = {c.topic_id: topic_key(c.subject, c.outcome) for c in live}
        known = await store.known(now - timedelta(days=state.config.novelty_lookback_days))
        known_keys = {topic.topic_key for topic in known}
        unmatched = [c for c in live if keys[c.topic_id] not in known_keys]
        embedder = self._deps.embedder
        if unmatched and known and embedder is not None:
            try:
                vectors = await embedder.embed_documents(
                    [c.canonical_topic for c in unmatched] + [t.label for t in known]
                )
                own, theirs = vectors[: len(unmatched)], vectors[len(unmatched) :]
                for candidate, vector in zip(unmatched, own, strict=True):
                    same_category = [
                        (normalize.cosine(vector, other), topic)
                        for other, topic in zip(theirs, known, strict=True)
                        if topic.category is candidate.category
                    ]
                    if same_category:
                        similarity, topic = max(same_category, key=lambda pair: pair[0])
                        if similarity >= state.config.duplicate_threshold:
                            keys[candidate.topic_id] = topic.topic_key
            except Exception as exc:  # identity falls back to exact keys
                state.notes.append(f"topic matching by meaning skipped: {exc}")
        refs = {
            keys[c.topic_id]: TopicRef(
                topic_key=keys[c.topic_id],
                label=c.canonical_topic,
                subject=c.subject,
                outcome=c.outcome,
                category=c.category,
            )
            for c in live
        }
        resolved = await store.resolve(list(refs.values()), now=now, exclude_run=state.run_id)
        recent = await store.recent_runs(
            now - timedelta(days=STATUS_LOOKBACK_DAYS), exclude_run=state.run_id
        )
        for candidate in live:
            match = resolved.get(keys[candidate.topic_id])
            if match is None:
                continue
            candidate.topic_ref_id = match.id
            self._histories[match.id] = history_metrics(match, recent, now=now)
        return len(resolved)

    def _discovery_strength(
        self, state: ResearchState, candidate: CandidateTopic
    ) -> tuple[int, int, int, float]:
        """How strongly discovery alone points at a topic: enrichment goes to these."""
        outlets = {
            state.items[state.stories[state.story_of[i]].representative].publisher_domain
            for i in candidate.item_ids
            if i in state.story_of and state.items[i].source_type in REPORTING_TYPES
        }
        kinds = {state.items[i].source_type for i in candidate.item_ids}
        trend = candidate.trend
        breakout = int(bool(trend and trend.is_breakout))
        manual = int(cluster.MANUAL in candidate.seeds)
        rising = float(trend.rising_percent or 0) if trend else 0.0
        return (manual, len(outlets - {None}) + 3 * breakout, len(kinds), rising)

    @staticmethod
    def _topic_query(
        state: ResearchState, candidate: CandidateTopic, since: datetime
    ) -> SourceQuery:
        assert state.window_end is not None
        return SourceQuery(
            since=since,
            until=state.window_end,
            geo=state.config.geo,
            language=state.config.language,
            terms=candidate.search_terms,
            seed=candidate.canonical_topic,
            category=candidate.category,
        )

    async def _interest(
        self, state: ResearchState, source: TrendSource, pool: Sequence[CandidateTopic]
    ) -> None:
        """Google Trends' interest series for the pool, four topics and the anchor a request."""
        interest = getattr(source, "interest", None)
        if interest is None:
            return
        window = state.config.trend_window_days
        anchor = state.config.trends_anchor
        for start in range(0, len(pool), TRENDS_BATCH):
            batch = list(pool[start : start + TRENDS_BATCH])
            queries = [_trends_query(c) for c in batch]
            series = await self._call(
                source.name,
                functools.partial(
                    interest, [*queries, anchor], days=2 * window, geo=state.config.geo
                ),
            )
            if series is None:
                if not self._calls_for().available(source.name):
                    return
                continue
            anchor_now, _ = _windows(series.get(anchor, []), window)
            for candidate, query in zip(batch, queries, strict=True):
                current, previous = _windows(series.get(query, []), window)
                if current is None:
                    continue
                trend = candidate.trend or TrendSignal(source=source.name)
                trend.current_interest = current
                trend.previous_interest = previous
                trend.anchor_interest = anchor_now
                if previous is not None and current + previous > 0:
                    trend.growth_source = "interest_series"
                    trend.growth_percent = scoring.growth_percent(current, previous)
                candidate.trend = trend

    def _applicable(self) -> set[str]:
        """Optional sub-signals this run has a source for."""
        names = set(self._sources)
        found: set[str] = set()
        if "google_trends" in names:
            found |= {"search", "search_level"}
        if "gdelt" in names:
            found.add("news_volume")
        if names & {"google_news", "news"}:
            found.add("news_counts")
        if "youtube" in names:
            found |= {"video", "video_views"}
        if self._deps.embedder is not None:
            found.add("novelty")
        return found


# --- module helpers ----------------------------------------------------------


def _fields(query: SourceQuery) -> dict[str, Any]:
    return {name: getattr(query, name) for name in query.__dataclass_fields__}


def _trends_query(candidate: CandidateTopic) -> str:
    """How people would search for it: a rising query when there is one."""
    if candidate.trend is not None and candidate.queries:
        return candidate.queries[0]
    return " ".join(candidate.search_terms)


def _windows(values: list[float], window: int) -> tuple[float | None, float | None]:
    """Means of the last ``window`` days and the ``window`` days before them."""
    if len(values) < window:
        return None, None
    current = statistics.fmean(values[-window:])
    earlier = values[-2 * window : -window]
    return round(current, 2), round(statistics.fmean(earlier), 2) if earlier else None


def _search_metrics(trend: TrendSignal | None) -> SearchMetrics | None:
    if trend is None:
        return None
    return SearchMetrics(
        source=trend.source,
        current_interest=trend.current_interest,
        previous_interest=trend.previous_interest,
        anchor_interest=trend.anchor_interest,
        rising_percent=trend.rising_percent,
        is_breakout=trend.is_breakout,
    )


def _news_signal(
    batch: SourceBatch, relevant: list[SourceItem], boundary: datetime
) -> NewsSignal:
    """The desk's news line: relevant articles this window and the one before."""
    recent = [i for i in relevant if i.published_at is None or i.published_at >= boundary]
    earlier = len(relevant) - len(recent)
    return NewsSignal(
        source=relevant[0].source_name if relevant else "news",
        articles_found=batch.total if batch.total is not None else len(batch.items),
        recent=len(recent),
        previous=None if batch.truncated else earlier,
        top_articles=[
            NewsArticle(
                title=i.title, url=i.url or "", source=i.publisher, published_at=i.published_at
            )
            for i in recent[:5]
            if i.url
        ],
    )


def _ineligible(candidate: CandidateTopic, min_score: float, min_outlets: int) -> str | None:
    """Why an enriched, scored topic cannot be ranked, or None."""
    m, overall = candidate.metrics, candidate.scores.overall
    if m is None or overall is None:
        return "not scored"
    if overall < min_score:
        return f"trend score {overall:.1f} is below the minimum {min_score:.1f}"
    if not m.coverage_measured:
        return "independent coverage could not be measured (no news source answered)"
    if m.independent_publishers < min_outlets:
        return (
            f"only {m.independent_publishers} independent outlet"
            f"{'s' if m.independent_publishers != 1 else ''} this window "
            f"(needs {min_outlets}): not yet a cross-source trend"
        )
    return None
