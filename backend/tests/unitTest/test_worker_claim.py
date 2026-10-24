"""``PipelineWorker._claim_next_run`` — attempt counting and retry backoff.

DB-backed for the same reason as the orchestrator's boundary tests: the claim
is a ``FOR UPDATE SKIP LOCKED`` statement, and skipping locked rows is not
something a fake session has. Skips when no database is reachable, so **a
skipped run verifies none of this.**
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.domain.enums import RunStatus
from app.domain.models import PipelineRun
from app.pipeline.runner import PipelineWorker

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url

_TOPIC_PREFIX = "claimtest:"


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the worker's module-level factory at a per-test engine.

    ``_claim_next_run`` reaches for ``get_session_factory()`` itself rather
    than taking one, so the factory is substituted rather than injected.
    """
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no database at {DATABASE_URL}")

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # ``_claim_next_run`` takes the oldest queued run in the whole table, so a
    # dev database with real work waiting would hand these tests somebody
    # else's run. Skip rather than claim it — claiming flips it to `running`
    # and burns an attempt on a run no worker is about to execute.
    async with factory() as session:
        queued = await session.scalar(
            sa.text("SELECT count(*) FROM pipeline_runs WHERE status = 'queued'")
        )
    if queued:
        await engine.dispose()
        pytest.skip(f"{queued} run(s) already queued; these tests need an empty queue")

    monkeypatch.setattr("app.pipeline.runner.get_session_factory", lambda: factory)
    try:
        yield factory
    finally:
        async with factory() as cleanup:
            await cleanup.execute(
                sa.text("DELETE FROM pipeline_runs WHERE topic LIKE :p"),
                {"p": f"{_TOPIC_PREFIX}%"},
            )
            await cleanup.commit()
        await engine.dispose()


async def _queue(
    factory: async_sessionmaker[AsyncSession],
    topic: str,
    *,
    attempts: int = 0,
    next_attempt_at: datetime | None = None,
) -> str:
    async with factory() as session:
        run = PipelineRun(
            topic=f"{_TOPIC_PREFIX}{topic}",
            status=RunStatus.QUEUED,
            attempts=attempts,
            next_attempt_at=next_attempt_at,
        )
        session.add(run)
        await session.commit()
        return run.id


async def test_claiming_counts_the_attempt(db: async_sessionmaker[AsyncSession]) -> None:
    """Counted when the attempt *begins*, not when it ends.

    On completion instead, a run that reliably kills its worker never records
    an attempt and cycles forever.
    """
    run_id = await _queue(db, "counts")

    claimed = await PipelineWorker()._claim_next_run()

    assert claimed is not None
    assert (claimed[0], claimed[3]) == (run_id, 1)

    async with db() as session:
        status, attempts = (
            await session.execute(
                sa.text("SELECT status, attempts FROM pipeline_runs WHERE id = :id"),
                {"id": run_id},
            )
        ).one()
    assert (status, attempts) == (RunStatus.RUNNING, 1)


async def test_a_run_waiting_out_its_backoff_is_not_claimed(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The other half of the retry.

    Requeueing without a delay is not a retry: the next poll is five seconds
    away, so a run held back by a provider asking for a minute would spend its
    whole attempt budget on the same 429 inside fifteen seconds.
    """
    await _queue(
        db, "waiting", attempts=1, next_attempt_at=datetime.now(UTC) + timedelta(minutes=5)
    )

    assert await PipelineWorker()._claim_next_run() is None


async def test_a_run_whose_backoff_has_elapsed_is_claimed(
    db: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _queue(
        db, "elapsed", attempts=1, next_attempt_at=datetime.now(UTC) - timedelta(seconds=1)
    )

    claimed = await PipelineWorker()._claim_next_run()

    assert claimed is not None
    assert (claimed[0], claimed[3]) == (run_id, 2)


async def test_a_waiting_run_does_not_block_a_ready_one(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The claim takes the oldest queued run. A run in backoff must be skipped
    rather than selected and rejected, or one throttled topic stalls the queue
    behind it for five minutes."""
    await _queue(
        db, "older-waiting", next_attempt_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    ready_id = await _queue(db, "newer-ready")

    claimed = await PipelineWorker()._claim_next_run()

    assert claimed is not None
    assert claimed[0] == ready_id


async def test_nothing_queued_returns_none(db: async_sessionmaker[AsyncSession]) -> None:
    assert await PipelineWorker()._claim_next_run() is None
