"""Research runs against real Postgres: constraints, single-flight, claim, checkpoint,
and the trend history (topics, observations, feed state).

Needs a live database with 0005 applied, and skips silently without one — see
the note in ``test_db_invariants.py``. **A skipped run verifies none of this.**

The checkpoint and claim tests commit through a session factory, as the worker
does, so they delete their own rows afterwards: a queued research run left
behind would be claimed by the next worker that starts.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.domain.enums import ResearchCandidateStatus, ResearchRunMode, ResearchRunStatus
from app.domain.models import ResearchCandidate, ResearchRun
from app.research import service
from app.research.contracts import (
    CandidateTopic,
    Category,
    ResearchConfig,
    ResearchParams,
    ResearchStage,
    ResearchState,
)

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1 FROM research_runs LIMIT 1"))
            await connection.execute(sa.text("SELECT 1 FROM research_topics LIMIT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no database with research_runs and 0005 applied at {DATABASE_URL}")
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest.fixture
async def session(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as db:
        try:
            yield db
        finally:
            await db.rollback()


async def _active(db: AsyncSession) -> bool:
    return bool(
        await db.scalar(
            sa.select(sa.func.count())
            .select_from(ResearchRun)
            .where(ResearchRun.status.in_(("queued", "running")))
        )
    )


def _config() -> dict[str, Any]:
    return ResearchConfig.resolve(Settings(), ResearchParams()).model_dump(mode="json")


async def test_a_second_run_is_refused_while_one_is_queued(session: AsyncSession) -> None:
    if await _active(session):
        pytest.skip("a real research run is queued or running")
    first = await service.start_research_run(
        session,
        mode=ResearchRunMode.MANUAL,
        params=ResearchParams(),
        requested_by=None,
        settings=Settings(),
    )
    assert first.label.startswith("research_")
    assert first.config["weights"]["momentum"] == 30

    with pytest.raises(service.ResearchAlreadyActive):
        await service.start_research_run(
            session,
            mode=ResearchRunMode.WEEKLY,
            params=ResearchParams(),
            requested_by=None,
            settings=Settings(),
        )


async def _run_row(db: AsyncSession, status: str = "completed") -> str:
    run_id = str(uuid.uuid4())
    await db.execute(
        sa.text(
            "INSERT INTO research_runs (id, label, mode, status, params, config) "
            "VALUES (:id, :label, 'manual', CAST(:status AS research_run_status), "
            "'{}'::jsonb, '{}'::jsonb)"
        ),
        {"id": run_id, "label": f"research_test_{run_id[:8]}", "status": status},
    )
    return run_id


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        ("promoted", {}),  # no run, no reviewer
        ("dismissed", {"decided_at": datetime.now(UTC)}),  # no reviewer, no reason
    ],
)
async def test_decisions_need_their_evidence(
    session: AsyncSession, status: str, extra: dict[str, Any]
) -> None:
    run_id = await _run_row(session)
    session.add(
        ResearchCandidate(
            id=str(uuid.uuid4()),
            research_run_id=run_id,
            canonical_topic="Creatine before bed",
            queries=["creatine before bed"],
            signals={},
            scores={},
            status=ResearchCandidateStatus(status),
            **extra,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_claim_checkpoint_and_finish(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as db:
        if await _active(db):
            pytest.skip("a real research run is queued or running")
        run_id = await _run_row(db, status="queued")
        await db.execute(
            sa.update(ResearchRun).where(ResearchRun.id == run_id).values(config=_config())
        )
        await db.commit()
    try:
        claimed = await service.claim_next(factory)
        assert claimed is not None and claimed[0] == run_id

        state = await service.load_state(factory, run_id, today=date(2026, 9, 30))
        candidate = CandidateTopic(
            topic_id=str(uuid.uuid4()),
            canonical_topic="Creatine before bed",
            category=Category.SUPPLEMENTS,
            subject="creatine",
            outcome="sleep",
            queries=["creatine before bed"],
        )
        state.candidates = [candidate]
        state.stage = ResearchStage.SCORING_TOPICS
        state.stage_log.append({"stage": "scoring_topics", "metrics": {"scored": 1}})
        await service.checkpoint(factory, state)

        # A later checkpoint rewrites the same row by primary key.
        candidate.status = ResearchCandidateStatus.SELECTED
        candidate.rank = 1
        state.stage = ResearchStage.COMPLETED
        await service.finish(factory, state)

        async with factory() as db:
            run = await db.get(ResearchRun, run_id)
            rows = (
                (
                    await db.execute(
                        sa.select(ResearchCandidate).where(
                            ResearchCandidate.research_run_id == run_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert run is not None
        assert run.status is ResearchRunStatus.COMPLETED
        assert run.attempts == 1
        assert run.stage_log[0]["metrics"] == {"scored": 1}
        assert len(rows) == 1
        assert rows[0].status is ResearchCandidateStatus.SELECTED
        assert rows[0].signals["subject"] == "creatine"
        assert rows[0].rank == 1
    finally:
        async with factory() as db:
            await db.execute(sa.delete(ResearchRun).where(ResearchRun.id == run_id))
            await db.commit()


async def test_a_failed_state_is_recorded_as_failed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as db:
        if await _active(db):
            pytest.skip("a real research run is queued or running")
        run_id = await _run_row(db, status="running")
        await db.execute(
            sa.update(ResearchRun).where(ResearchRun.id == run_id).values(config=_config())
        )
        await db.commit()
    try:
        state = ResearchState(
            run_id=run_id,
            label="research_test",
            mode=ResearchRunMode.MANUAL,
            config=ResearchConfig.model_validate(_config()),
            today=date(2026, 9, 30),
            failed="no_trend_data: google trends 429",
            stage=ResearchStage.FAILED,
        )
        await service.finish(factory, state)
        async with factory() as db:
            run = await db.get(ResearchRun, run_id)
        assert run is not None
        assert run.status is ResearchRunStatus.FAILED
        assert run.error == {"message": "no_trend_data: google trends 429"}
    finally:
        async with factory() as db:
            await db.execute(sa.delete(ResearchRun).where(ResearchRun.id == run_id))
            await db.commit()


async def test_undecided_candidates_of_old_runs_are_pruned_and_decisions_kept(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Novelty reads only promoted and dismissed candidates, so those survive."""
    from datetime import timedelta

    async with factory() as db:
        run_id = await _run_row(db, status="completed")
        await db.execute(
            sa.update(ResearchRun)
            .where(ResearchRun.id == run_id)
            .values(finished_at=datetime.now(UTC) - timedelta(days=90))
        )
        user_id = await db.scalar(sa.text("SELECT id FROM users LIMIT 1"))
        if user_id is None:
            await db.rollback()
            pytest.skip("no reviewer row to attribute a dismissal to")
        undecided, dismissed = str(uuid.uuid4()), str(uuid.uuid4())
        for candidate_id, status, extra in (
            (undecided, ResearchCandidateStatus.SELECTED, {}),
            (
                dismissed,
                ResearchCandidateStatus.DISMISSED,
                {
                    "decided_by": str(user_id),
                    "decided_at": datetime.now(UTC),
                    "dismiss_reason": "covered last month",
                },
            ),
        ):
            db.add(
                ResearchCandidate(
                    id=candidate_id,
                    research_run_id=run_id,
                    canonical_topic="Creatine before bed",
                    queries=["creatine before bed"],
                    signals={},
                    scores={},
                    status=status,
                    **extra,
                )
            )
        await db.commit()
    try:
        await service.prune_undecided(factory, timedelta(days=60))
        async with factory() as db:
            left = (
                (
                    await db.execute(
                        sa.select(ResearchCandidate.id).where(
                            ResearchCandidate.research_run_id == run_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert left == [dismissed]
    finally:
        async with factory() as db:
            await db.execute(sa.delete(ResearchRun).where(ResearchRun.id == run_id))
            await db.commit()


# --- trend search (0005) -------------------------------------------------------


async def test_two_active_runs_are_refused_by_the_index_itself(session: AsyncSession) -> None:
    """The SELECT check names the run in the way; the index is what holds a race."""
    if await _active(session):
        pytest.skip("a real research run is queued or running")
    await _run_row(session, status="queued")
    with pytest.raises(IntegrityError):
        await _run_row(session, status="running")
        await session.flush()


async def test_topics_resolve_idempotently_and_observations_upsert(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import timedelta

    from app.research.trends.contracts import TopicMetrics, TrendScores
    from app.research.trends.history import TopicRef

    key = f"test-topic-{uuid.uuid4().hex[:8]}|sleep"
    store = service.PostgresTopicStore(factory)
    now = datetime.now(UTC)
    ref = TopicRef(topic_key=key, label="Test topic for sleep", subject="test topic",
                   outcome="sleep", category=Category.SLEEP_RECOVERY)
    async with factory() as db:
        run_id = await _run_row(db, status="completed")
        await db.execute(
            sa.update(ResearchRun).where(ResearchRun.id == run_id).values(config=_config())
        )
        await db.commit()
    try:
        first = await store.resolve([ref], now=now - timedelta(days=7), exclude_run=run_id)
        again = await store.resolve([ref], now=now, exclude_run=run_id)
        topic_id = first[key].id
        assert again[key].id == topic_id
        assert again[key].first_seen_at == first[key].first_seen_at

        state = ResearchState(
            run_id=run_id, label="research_test", mode=ResearchRunMode.MANUAL,
            config=ResearchConfig.model_validate(_config()), today=now.date(),
            window_start=now - timedelta(days=7), window_end=now,
        )
        candidate = CandidateTopic(
            topic_id=str(uuid.uuid4()), canonical_topic="Test topic for sleep",
            category=Category.SLEEP_RECOVERY, subject="test topic", outcome="sleep",
            queries=["test topic sleep"], topic_ref_id=topic_id, in_enrich_pool=True,
            metrics=TopicMetrics(window_days=7, observed_at=now, stories=5,
                                 independent_publishers=4, coverage_measured=True),
            scores=TrendScores(overall=61.5, data_coverage=0.8),
            status=ResearchCandidateStatus.SELECTED,
        )
        state.candidates = [candidate]
        assert await service.record_observations(factory, state) == 1
        assert await service.record_observations(factory, state) == 1  # idempotent

        async with factory() as db:
            rows = (await db.execute(
                sa.select(sa.text("stories, proposed, trend_score"))
                .select_from(sa.text("research_topic_observations"))
                .where(sa.text("topic_id = :t")).params(t=topic_id)
            )).all()
        assert rows == [(5, True, 61.5)]

        nobody = "00000000-0000-0000-0000-000000000000"
        later = await store.resolve([ref], now=now, exclude_run=nobody)
        assert [o.stories for o in later[key].observations] == [5]
        assert run_id in await store.recent_runs(now - timedelta(days=1), exclude_run=nobody)
    finally:
        async with factory() as db:
            await db.execute(sa.delete(ResearchRun).where(ResearchRun.id == run_id))
            await db.execute(
                sa.text("DELETE FROM research_topics WHERE topic_key = :k"), {"k": key}
            )
            await db.commit()


async def test_history_pruning_keeps_what_is_still_referenced(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import timedelta

    old, kept = f"prune-old-{uuid.uuid4().hex[:8]}|", f"prune-kept-{uuid.uuid4().hex[:8]}|"
    long_ago = datetime.now(UTC) - timedelta(days=400)
    async with factory() as db:
        run_id = await _run_row(db, status="completed")
        ids = {}
        for key in (old, kept):
            ids[key] = await db.scalar(sa.text(
                "INSERT INTO research_topics (topic_key, label, subject, category, "
                "first_seen_at, last_seen_at) "
                "VALUES (:k, 'x', 'x', 'other', :t, :t) RETURNING id"
            ), {"k": key, "t": long_ago})
        await db.execute(sa.text(
            "INSERT INTO research_topic_observations (topic_id, research_run_id, observed_at, "
            "window_start, window_end, stories, independent_publishers, data_coverage, "
            "scoring_version, metrics, scores) VALUES (:t, :r, :at, :at, :at, 1, 1, 0.7, "
            "'trend-v1', '{}', '{}')"
        ), {"t": ids[old], "r": run_id, "at": long_ago})
        db.add(ResearchCandidate(
            id=str(uuid.uuid4()), research_run_id=run_id, canonical_topic="x", queries=[],
            signals={}, scores={}, topic_ref_id=str(ids[kept]),
        ))
        await db.commit()
    try:
        observations, _ = await service.prune_history(factory, timedelta(days=180))
        assert observations >= 1
        async with factory() as db:
            left = set((await db.execute(sa.text(
                "SELECT topic_key FROM research_topics WHERE topic_key IN (:a, :b)"
            ), {"a": old, "b": kept})).scalars())
        assert left == {kept}  # the old one lost its last observation; kept is referenced
    finally:
        async with factory() as db:
            await db.execute(sa.delete(ResearchRun).where(ResearchRun.id == run_id))
            await db.execute(sa.text("DELETE FROM research_topics WHERE topic_key IN (:a, :b)"),
                             {"a": old, "b": kept})
            await db.commit()


async def test_feed_state_round_trips_and_stores_sql_null_for_no_items(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.research.trends.sources.rss import FeedState

    url = f"https://feeds.test/{uuid.uuid4().hex}.xml"
    store = service.PostgresFeedStore(factory)
    try:
        await store.save(FeedState(feed_url=url, last_error="503", consecutive_failures=2))
        async with factory() as db:
            is_null = await db.scalar(sa.text(
                "SELECT items IS NULL FROM research_feed_state WHERE feed_url = :u"
            ), {"u": url})
        assert is_null is True  # SQL NULL, not the JSON literal null

        now = datetime.now(UTC)
        await store.save(FeedState(
            feed_url=url, etag='"v2"', last_success_at=now, last_fetched_at=now,
            items=[{"title": "t", "link": "https://a.test/x"}],
        ))
        loaded = (await store.load([url]))[url]
        assert loaded.etag == '"v2"'
        assert loaded.consecutive_failures == 0
        assert loaded.items == [{"title": "t", "link": "https://a.test/x"}]
        assert loaded.last_success_at is not None
    finally:
        async with factory() as db:
            await db.execute(sa.text("DELETE FROM research_feed_state WHERE feed_url = :u"),
                             {"u": url})
            await db.commit()


async def test_a_checkpoint_writes_the_summary_and_the_topic_reference(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.research.trends.history import TopicRef

    key = f"checkpoint-{uuid.uuid4().hex[:8]}|"
    async with factory() as db:
        run_id = await _run_row(db, status="completed")
        await db.execute(
            sa.update(ResearchRun).where(ResearchRun.id == run_id).values(config=_config())
        )
        await db.commit()
    try:
        resolved = await service.PostgresTopicStore(factory).resolve(
            [
                TopicRef(
                    topic_key=key, label="x", subject="x", outcome="", category=Category.OTHER
                )
            ],
            now=datetime.now(UTC), exclude_run=run_id,
        )
        state = ResearchState(
            run_id=run_id, label="research_test", mode=ResearchRunMode.MANUAL,
            config=ResearchConfig.model_validate(_config()), today=date(2026, 10, 12),
        )
        state.counts = {"items_collected": 3}
        state.candidates = [CandidateTopic(
            topic_id=str(uuid.uuid4()), canonical_topic="x", category=Category.OTHER,
            subject="x", queries=["x"], topic_ref_id=resolved[key].id,
        )]
        await service.checkpoint(factory, state)
        async with factory() as db:
            run = await db.get(ResearchRun, run_id)
            ref = await db.scalar(sa.select(ResearchCandidate.topic_ref_id).where(
                ResearchCandidate.research_run_id == run_id))
        assert run is not None
        assert run.summary == {"trend": None, "counts": {"items_collected": 3}}
        assert ref == resolved[key].id
    finally:
        async with factory() as db:
            await db.execute(sa.delete(ResearchRun).where(ResearchRun.id == run_id))
            await db.execute(
                sa.text("DELETE FROM research_topics WHERE topic_key = :k"), {"k": key}
            )
            await db.commit()
