"""Database reads and writes for research runs.

``start_research_run`` is the **one** way a research run is created: the desk's
button and n8n's weekly call both come through it, so a weekly run and a manual
one differ only in ``mode`` and ``requested_by``.

Bookkeeping writes (checkpoint, heartbeat, finish) take a session *factory* and
commit on their own, like the orchestrator's bookkeeping: a run's record of how
far it got has to survive whatever happens to the run.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.enums import (
    DiscoveryCandidateStatus,
    ResearchCandidateStatus,
    ResearchRunMode,
    ResearchRunStatus,
    RunStatus,
)
from app.domain.models import (
    DiscoveryCandidate,
    PipelineRun,
    ResearchCandidate,
    ResearchFeedState,
    ResearchRun,
    ResearchTopic,
    ResearchTopicObservation,
)
from app.research.contracts import (
    CandidateTopic,
    Category,
    PastTopic,
    ResearchConfig,
    ResearchParams,
    ResearchStage,
    ResearchState,
)
from app.research.trends.contracts import (
    CollectionWindow,
    Downstream,
    ResultStatus,
    TrendSearchResult,
)
from app.research.trends.history import (
    KnownTopic,
    Observation,
    ResolvedTopic,
    TopicRef,
)
from app.research.trends.sources.rss import FeedState

logger = logging.getLogger(__name__)

_ACTIVE = (ResearchRunStatus.QUEUED, ResearchRunStatus.RUNNING)


class ResearchAlreadyActive(RuntimeError):
    """A research run is already queued or running."""


def make_label(now: datetime) -> str:
    return f"research_{now:%Y_%m_%d}_{secrets.token_hex(2)}"


async def start_research_run(
    session: AsyncSession,
    *,
    mode: ResearchRunMode,
    params: ResearchParams,
    requested_by: str | None,
    settings: Settings,
) -> ResearchRun:
    """Queue a research run. Does not commit — the request session does.

    Single-flight: a second trigger while one is queued or running is refused
    rather than queued behind it. Two runs a minute apart would spend the same
    Trends, news and PubMed budget to compute the same answer — and News API's
    free plan has 100 requests a day, which one run can use half of.

    Raises:
        ResearchAlreadyActive: naming the run that is in the way.
    """
    active = await session.scalar(
        select(ResearchRun.label).where(ResearchRun.status.in_(_ACTIVE)).limit(1)
    )
    if active is not None:
        raise ResearchAlreadyActive(f"research run {active} is already queued or running")
    # The check above names the run in the way; the partial unique index
    # ``research_runs_one_active`` is what actually holds when two triggers
    # race past it, so the insert runs in a savepoint and a violation is the
    # same 409.

    now = datetime.now(UTC)
    config = ResearchConfig.resolve(settings, params)
    run = ResearchRun(
        label=make_label(now),
        mode=mode,
        status=ResearchRunStatus.QUEUED,
        stage=ResearchStage.QUEUED.value,
        params=params.model_dump(mode="json", exclude_none=True),
        config=config.model_dump(mode="json"),
        provider_status={},
        stage_log=[],
        notes=[],
        requested_by=requested_by,
        attempts=0,
        created_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
    except IntegrityError as exc:
        raise ResearchAlreadyActive(
            "a research run is already queued or running (started at the same moment)"
        ) from exc
    logger.info("queued research run %s (%s)", run.label, mode.value)
    return run


# --- worker side -----------------------------------------------------------


async def claim_next(factory: async_sessionmaker[AsyncSession]) -> tuple[str, str] | None:
    """Claim the oldest queued research run: ``(id, label)``, or None."""
    now = datetime.now(UTC)
    async with factory() as session:
        run = (
            await session.execute(
                select(ResearchRun)
                .where(ResearchRun.status == ResearchRunStatus.QUEUED)
                .order_by(ResearchRun.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if run is None:
            await session.commit()
            return None
        run.status = ResearchRunStatus.RUNNING
        run.started_at = now
        run.heartbeat_at = now
        run.attempts += 1
        claimed = (run.id, run.label)
        await session.commit()
        return claimed


async def beat(factory: async_sessionmaker[AsyncSession], run_id: str) -> None:
    async with factory() as session:
        await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == run_id)
            .values(heartbeat_at=datetime.now(UTC))
        )
        await session.commit()


async def recover_stale(
    factory: async_sessionmaker[AsyncSession], stale_after: timedelta
) -> int:
    """Fail research runs whose worker stopped reporting. Never requeued.

    Unlike a pipeline run, a research run is cheap to start again and has no
    article to protect: failing it and letting the next trigger start fresh is
    simpler than resuming a half-measured candidate pool.
    """
    cutoff = datetime.now(UTC) - stale_after
    async with factory() as session:
        result = await session.execute(
            update(ResearchRun)
            .where(
                ResearchRun.status == ResearchRunStatus.RUNNING,
                func.coalesce(ResearchRun.heartbeat_at, ResearchRun.started_at) < cutoff,
            )
            .values(
                status=ResearchRunStatus.FAILED,
                error={"message": "worker stopped reporting; start a new research run"},
                finished_at=datetime.now(UTC),
            )
            .returning(ResearchRun.label)
        )
        labels = result.scalars().all()
        await session.commit()
    for label in labels:
        logger.error("research run %s abandoned by its worker; marked failed", label)
    return len(labels)


async def load_state(
    factory: async_sessionmaker[AsyncSession], run_id: str, *, today: Any
) -> ResearchState:
    async with factory() as session:
        run = await session.get(ResearchRun, run_id)
        if run is None:
            raise LookupError(f"research run {run_id} not found")
        return ResearchState(
            run_id=run.id,
            label=run.label,
            mode=run.mode,
            config=ResearchConfig.model_validate(run.config),
            today=today,
        )


async def checkpoint(factory: async_sessionmaker[AsyncSession], state: ResearchState) -> None:
    """Write the run's progress and every candidate so far.

    After every node, so the desk can show a run while it is still going, and a
    run that dies keeps what it had measured.
    """
    async with factory() as session:
        await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == state.run_id)
            .values(
                stage=state.stage.value,
                provider_status={
                    name: status.model_dump(mode="json")
                    for name, status in state.provider_status.items()
                },
                stage_log=state.stage_log,
                notes=state.notes,
                summary=run_summary(state),
                heartbeat_at=datetime.now(UTC),
            )
        )
        rows = [candidate_row(candidate, state.run_id) for candidate in state.candidates]
        if rows:
            statement = insert(ResearchCandidate).values(rows)
            updatable = {
                key: statement.excluded[key]
                for key in rows[0]
                if key not in ("id", "research_run_id")
            }
            await session.execute(
                statement.on_conflict_do_update(index_elements=["id"], set_=updatable)
            )
        await session.commit()


async def finish(factory: async_sessionmaker[AsyncSession], state: ResearchState) -> None:
    await checkpoint(factory, state)
    failed = state.failed is not None
    if state.trend_summary is not None:
        # The trend phase finished, so its measurements are whole even when
        # the evidence check later failed. A failed trend phase writes nothing:
        # partial metrics would read as a decline next week.
        try:
            await record_observations(factory, state)
        except Exception:
            logger.exception("could not record trend history for run %s", state.label)
    async with factory() as session:
        await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == state.run_id)
            .values(
                status=ResearchRunStatus.FAILED if failed else ResearchRunStatus.COMPLETED,
                stage=state.stage.value,
                error={"message": state.failed} if failed else None,
                finished_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def fail_unloadable(
    factory: async_sessionmaker[AsyncSession], run_id: str, message: str
) -> None:
    """Mark a claimed run failed when its stored state cannot even be loaded."""
    try:
        async with factory() as session:
            await session.execute(
                update(ResearchRun)
                .where(ResearchRun.id == run_id)
                .values(
                    status=ResearchRunStatus.FAILED,
                    error={"message": f"could not load the run: {message}"},
                    finished_at=datetime.now(UTC),
                )
            )
            await session.commit()
    except Exception:
        logger.exception("could not record that research run %s failed to load", run_id)


async def prune_undecided(
    factory: async_sessionmaker[AsyncSession], retention: timedelta
) -> int:
    """Delete candidates nobody decided on, from runs finished before ``retention``.

    Promoted and dismissed candidates stay — novelty reads them, and a promoted
    one is the provenance of a pipeline run. The run rows stay too (a few KB
    each): they are the record that a run happened and what it saw.
    """
    cutoff = datetime.now(UTC) - retention
    async with factory() as session:
        result = await session.execute(
            delete(ResearchCandidate).where(
                ResearchCandidate.status.not_in(
                    (ResearchCandidateStatus.PROMOTED, ResearchCandidateStatus.DISMISSED)
                ),
                ResearchCandidate.research_run_id.in_(
                    select(ResearchRun.id).where(ResearchRun.finished_at < cutoff)
                ),
            )
        )
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


def candidate_row(candidate: CandidateTopic, run_id: str) -> dict[str, Any]:
    return {
        "id": candidate.topic_id,
        "research_run_id": run_id,
        "canonical_topic": candidate.canonical_topic,
        "category": candidate.category.value,
        "queries": candidate.queries,
        "signals": candidate.signals_json(),
        "scores": candidate.scores.model_dump(mode="json"),
        "evidence_status": candidate.evidence_status.value
        if candidate.evidence_status
        else None,
        "overall": candidate.scores.overall,
        "rank": candidate.rank,
        "status": candidate.status,
        "discard_reason": candidate.discard_reason,
        "topic_ref_id": candidate.topic_ref_id,
    }


def run_summary(state: ResearchState) -> dict[str, Any] | None:
    """What ``research_runs.summary`` holds: the frozen trend output and counts."""
    if state.trend_summary is None and not state.counts:
        return None
    return {"trend": state.trend_summary, "counts": state.counts}


# --- trend history -----------------------------------------------------------


async def record_observations(
    factory: async_sessionmaker[AsyncSession], state: ResearchState
) -> int:
    """One observation per scored, enriched topic: the trend history.

    An upsert on (topic, run), so finishing a run twice writes the same rows.
    ``proposed`` is whether the run finally selected it — what repeat
    suppression asks about.
    """
    assert state.window_start is not None and state.window_end is not None
    rows = []
    for candidate in state.candidates:
        if (
            candidate.topic_ref_id is None
            or candidate.metrics is None
            or not candidate.in_enrich_pool
        ):
            continue
        metrics, scores = candidate.metrics, candidate.scores
        rows.append(
            {
                "topic_id": candidate.topic_ref_id,
                "research_run_id": state.run_id,
                "observed_at": state.window_end,
                "window_start": state.window_start,
                "window_end": state.window_end,
                "stories": metrics.stories,
                "independent_publishers": metrics.independent_publishers,
                "trend_score": scores.overall,
                "data_coverage": scores.data_coverage,
                "trend_status": (
                    candidate.trend_status.value if candidate.trend_status else None
                ),
                "scoring_version": scores.version,
                "proposed": candidate.status
                in (ResearchCandidateStatus.SELECTED, ResearchCandidateStatus.PROMOTED),
                "metrics": metrics.model_dump(mode="json"),
                "scores": scores.model_dump(mode="json"),
            }
        )
    if not rows:
        return 0
    # One topic can back two candidates when wording drifted onto one key;
    # the later (lower-ranked) row would otherwise make the upsert collide.
    unique = {row["topic_id"]: row for row in reversed(rows)}
    async with factory() as session:
        statement = insert(ResearchTopicObservation).values(list(unique.values()))
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["topic_id", "research_run_id"],
                set_={
                    key: statement.excluded[key]
                    for key in rows[0]
                    if key not in ("topic_id", "research_run_id")
                },
            )
        )
        await session.commit()
    return len(unique)


class PostgresTopicStore:
    """``trends/history.py::TopicStore`` over ``research_topics`` and its observations."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def known(self, since: datetime) -> list[KnownTopic]:
        async with self._factory() as session:
            rows = await session.execute(
                select(ResearchTopic).where(
                    ResearchTopic.last_seen_at >= since, ResearchTopic.merged_into.is_(None)
                )
            )
            return [
                KnownTopic(
                    id=topic.id,
                    topic_key=topic.topic_key,
                    label=topic.label,
                    category=Category(topic.category),
                    last_seen_at=topic.last_seen_at,
                )
                for topic in rows.scalars()
            ]

    async def resolve(
        self, refs: list[TopicRef], *, now: datetime, exclude_run: str
    ) -> dict[str, ResolvedTopic]:
        if not refs:
            return {}
        values = [
            {
                "topic_key": ref.topic_key,
                "label": ref.label,
                "subject": ref.subject,
                "outcome": ref.outcome,
                "category": ref.category.value,
                "first_seen_at": now,
                "last_seen_at": now,
            }
            for ref in {ref.topic_key: ref for ref in refs}.values()
        ]
        async with self._factory() as session:
            statement = insert(ResearchTopic).values(values)
            result = await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["topic_key"],
                    set_={
                        "label": statement.excluded.label,
                        "last_seen_at": func.greatest(
                            ResearchTopic.last_seen_at, statement.excluded.last_seen_at
                        ),
                    },
                ).returning(
                    ResearchTopic.topic_key,
                    ResearchTopic.id,
                    ResearchTopic.first_seen_at,
                    ResearchTopic.merged_into,
                )
            )
            rows = result.all()
            target = {key: merged or topic_id for key, topic_id, _, merged in rows}
            first_seen = {key: seen for key, _, seen, _ in rows}
            observed = await session.execute(
                select(
                    ResearchTopicObservation.topic_id,
                    ResearchTopicObservation.research_run_id,
                    ResearchTopicObservation.observed_at,
                    ResearchTopicObservation.stories,
                    ResearchTopicObservation.proposed,
                ).where(
                    ResearchTopicObservation.topic_id.in_(set(target.values())),
                    ResearchTopicObservation.research_run_id != exclude_run,
                )
            )
            by_topic: dict[str, list[Observation]] = {}
            for topic_id, run_id, observed_at, stories, proposed in observed:
                by_topic.setdefault(topic_id, []).append(
                    Observation(
                        run_id=run_id,
                        observed_at=observed_at,
                        stories=stories,
                        proposed=proposed,
                    )
                )
            # Committed before returning: the next checkpoint writes candidates
            # whose topic_ref_id points at these rows.
            await session.commit()
        return {
            key: ResolvedTopic(
                id=topic_id,
                first_seen_at=first_seen[key],
                observations=by_topic.get(topic_id, []),
            )
            for key, topic_id in target.items()
        }

    async def recent_runs(self, since: datetime, *, exclude_run: str) -> list[str]:
        async with self._factory() as session:
            rows = await session.execute(
                select(
                    ResearchTopicObservation.research_run_id,
                    func.max(ResearchTopicObservation.observed_at).label("at"),
                )
                .where(
                    ResearchTopicObservation.observed_at >= since,
                    ResearchTopicObservation.research_run_id != exclude_run,
                )
                .group_by(ResearchTopicObservation.research_run_id)
                .order_by(func.max(ResearchTopicObservation.observed_at).desc())
            )
            return [run_id for run_id, _ in rows]


class PostgresFeedStore:
    """``trends/sources/rss.py::FeedStateStore`` over ``research_feed_state``."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def load(self, urls: list[str]) -> dict[str, FeedState]:
        if not urls:
            return {}
        async with self._factory() as session:
            rows = await session.execute(
                select(ResearchFeedState).where(ResearchFeedState.feed_url.in_(urls))
            )
            return {
                row.feed_url: FeedState(
                    feed_url=row.feed_url,
                    etag=row.etag,
                    last_modified=row.last_modified,
                    last_fetched_at=row.last_fetched_at,
                    last_success_at=row.last_success_at,
                    last_status=row.last_status,
                    last_error=row.last_error,
                    consecutive_failures=row.consecutive_failures,
                    items=row.items or [],
                )
                for row in rows.scalars()
            }

    async def save(self, state: FeedState) -> None:
        values = state.model_dump(mode="json")
        values["items"] = values["items"] or None
        for key in ("last_fetched_at", "last_success_at"):
            values[key] = getattr(state, key)
        async with self._factory() as session:
            statement = insert(ResearchFeedState).values(values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["feed_url"],
                    set_={key: statement.excluded[key] for key in values if key != "feed_url"},
                )
            )
            await session.commit()


async def prune_history(
    factory: async_sessionmaker[AsyncSession], retention: timedelta
) -> tuple[int, int]:
    """Delete observations older than ``retention``, then topics nothing refers to.

    A topic survives while it has an observation, or a candidate row still
    points at it, or it was seen within the retention window.
    """
    cutoff = datetime.now(UTC) - retention
    async with factory() as session:
        observations = await session.execute(
            delete(ResearchTopicObservation).where(
                ResearchTopicObservation.observed_at < cutoff
            )
        )
        topics = await session.execute(
            delete(ResearchTopic).where(
                and_(
                    ResearchTopic.last_seen_at < cutoff,
                    ~exists().where(ResearchTopicObservation.topic_id == ResearchTopic.id),
                    ~exists().where(ResearchCandidate.topic_ref_id == ResearchTopic.id),
                )
            )
        )
        await session.commit()
    return (
        int(getattr(observations, "rowcount", 0) or 0),
        int(getattr(topics, "rowcount", 0) or 0),
    )


def build_result(run: ResearchRun, candidates: list[ResearchCandidate]) -> TrendSearchResult:
    """The trend agent's output for a run, with each topic's current status.

    The frozen trend output (``summary.trend``) says what the trend phase
    measured; the candidate rows say what happened to each topic since — the
    evidence gate's outcome, a reviewer's decision. A topic appears when the
    trend agent shortlisted it or it ended up selected or promoted.
    """
    frozen = (run.summary or {}).get("trend")
    window_end = run.started_at or run.created_at
    if frozen is None:
        return TrendSearchResult(
            run_id=run.id,
            label=run.label,
            status=_result_status(run, trend_done=False),
            started_at=run.started_at,
            completed_at=run.finished_at,
            collection_window=CollectionWindow(
                start=window_end - timedelta(days=run.config.get("trend_window_days", 7)),
                end=window_end,
            ),
            errors=[run.error["message"]] if run.error and run.error.get("message") else [],
            notes=list(run.notes or []),
            counts=(run.summary or {}).get("counts") or {},
        )
    result = TrendSearchResult.model_validate(frozen)
    rows = {candidate.id: candidate for candidate in candidates}
    topics = []
    for topic in result.topics:
        row = rows.get(topic.candidate_id)
        if row is not None:
            topic.status = row.status.value
            topic.downstream = _downstream(row)
        if topic.trend_shortlisted or topic.status in ("selected", "promoted"):
            topics.append(topic)
    result.topics = topics
    result.status = _result_status(run, trend_done=True)
    result.completed_at = run.finished_at or result.completed_at
    if run.error and run.error.get("message"):
        result.errors = [*result.errors, run.error["message"]]
    return result


def _downstream(row: ResearchCandidate) -> Downstream:
    """The evidence check's outcome for one topic, as a status — never a score."""
    if row.status in (
        ResearchCandidateStatus.SELECTED,
        ResearchCandidateStatus.PROMOTED,
        ResearchCandidateStatus.DISMISSED,
    ) or (row.status is ResearchCandidateStatus.SHORTLISTED and row.evidence_status):
        return Downstream(evidence_gate="passed")
    if row.status is ResearchCandidateStatus.DISCARDED and (
        row.evidence_status or (row.discard_reason or "").startswith(
            ("evidence", "no literature", "scientific evidence", "the article pipeline")
        )
    ):
        return Downstream(evidence_gate="failed", reason=row.discard_reason)
    return Downstream()


def _result_status(run: ResearchRun, *, trend_done: bool) -> ResultStatus:
    if run.status is ResearchRunStatus.FAILED:
        return "partial" if trend_done else "failed"
    statuses: dict[ResearchRunStatus, ResultStatus] = {
        ResearchRunStatus.QUEUED: "queued",
        ResearchRunStatus.RUNNING: "running",
        ResearchRunStatus.COMPLETED: "completed",
    }
    return statuses[run.status]


async def recent_history(
    factory: async_sessionmaker[AsyncSession], since: datetime
) -> list[PastTopic]:
    """Topics already written, in flight, promoted or turned down since ``since``.

    Pipeline runs stand in for articles: every article has one, and a run still
    queued is as much a duplicate as a published article. Failed runs are left
    out — they produced nothing. Both proposal tables count, so a research run
    does not re-propose what the MeSH scan's reviewer already promoted.
    """
    async with factory() as session:
        runs = await session.execute(
            select(PipelineRun.topic, PipelineRun.created_at).where(
                PipelineRun.created_at >= since, PipelineRun.status != RunStatus.FAILED
            )
        )
        research = await session.execute(
            select(
                ResearchCandidate.canonical_topic,
                ResearchCandidate.status,
                ResearchCandidate.decided_at,
            ).where(
                ResearchCandidate.decided_at >= since,
                ResearchCandidate.status.in_(
                    (ResearchCandidateStatus.PROMOTED, ResearchCandidateStatus.DISMISSED)
                ),
            )
        )
        discovery = await session.execute(
            select(
                DiscoveryCandidate.topic,
                DiscoveryCandidate.status,
                DiscoveryCandidate.decided_at,
            ).where(
                DiscoveryCandidate.decided_at >= since,
                or_(
                    DiscoveryCandidate.status == DiscoveryCandidateStatus.PROMOTED,
                    DiscoveryCandidate.status == DiscoveryCandidateStatus.DISMISSED,
                ),
            )
        )
    history = [PastTopic(text=topic, kind="article", decided_at=at) for topic, at in runs]
    for topic, status, at in [*research, *discovery]:
        history.append(PastTopic(text=topic, kind=str(status.value), decided_at=at))
    return history


# --- reads for the API -----------------------------------------------------


async def list_runs(session: AsyncSession, limit: int) -> list[ResearchRun]:
    return list(
        (
            await session.execute(
                select(ResearchRun).order_by(ResearchRun.created_at.desc()).limit(limit)
            )
        ).scalars()
    )


async def candidates_for(session: AsyncSession, run_id: str) -> list[ResearchCandidate]:
    return list(
        (
            await session.execute(
                select(ResearchCandidate)
                .where(ResearchCandidate.research_run_id == run_id)
                .order_by(
                    ResearchCandidate.rank.asc().nulls_last(), ResearchCandidate.canonical_topic
                )
            )
        ).scalars()
    )
