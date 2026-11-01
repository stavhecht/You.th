"""Recovering runs whose worker died — the heartbeat and the sweep.

DB-backed: the sweep is a ``FOR UPDATE SKIP LOCKED`` select feeding conditional
updates, and the whole question it answers ("is this row's timestamp older than
the cutoff") is a database question. Skips when no database is reachable, so
**a skipped run verifies none of this.**

Three failures are pinned here, and they are the three the previous
implementation had:

* **Stuck forever.** Detection ran once, at worker boot, against a 30-minute
  ``started_at`` cutoff. A worker killed at 14:00 and restarted at 14:01 saw a
  one-minute-old run, skipped it, and never swept again — the row stayed
  ``running`` for the life of the process.
* **Requeued while alive.** The same cutoff fires on a run that is merely slow.
  Requeueing it starts a second execution of a run already in flight, which is
  the double-article write ``_finish_run`` was made atomic to prevent.
* **Unbounded lives.** Recovery ignored ``attempts``, so a run that kills every
  worker that claims it was resurrected indefinitely.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.domain.enums import RunStatus
from app.domain.models import PipelineRun, PipelineStageRun
from app.pipeline.orchestrator import retry_delay
from app.pipeline.runner import PipelineWorker

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url

_TOPIC_PREFIX = "staletest:"
STALE_AFTER = timedelta(minutes=2)


def _worker(max_attempts: int = 3) -> PipelineWorker:
    return PipelineWorker(
        poll_interval=0.01,
        heartbeat_interval=0.01,
        stale_after=STALE_AFTER,
        sweep_interval=0.0,
        max_attempts=max_attempts,
    )


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> Any:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no database at {DATABASE_URL}")

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # The sweep scans every `running` row in the table, so a dev database with
    # a real run in flight would have these tests requeue somebody's work —
    # and the local worker would then execute it twice. Skip instead.
    async with factory() as session:
        live = await session.scalar(
            sa.text("SELECT count(*) FROM pipeline_runs WHERE status = 'running'")
        )
    if live:
        await engine.dispose()
        pytest.skip(f"{live} run(s) already running; the sweep would touch them")

    monkeypatch.setattr("app.pipeline.runner.get_session_factory", lambda: factory)
    try:
        yield factory
    finally:
        async with factory() as cleanup:
            await cleanup.execute(
                sa.text(
                    "DELETE FROM pipeline_stage_runs WHERE run_id IN "
                    "(SELECT id FROM pipeline_runs WHERE topic LIKE :p)"
                ),
                {"p": f"{_TOPIC_PREFIX}%"},
            )
            await cleanup.execute(
                sa.text("DELETE FROM pipeline_runs WHERE topic LIKE :p"),
                {"p": f"{_TOPIC_PREFIX}%"},
            )
            await cleanup.commit()
        await engine.dispose()


async def _running(
    factory: async_sessionmaker[AsyncSession],
    topic: str,
    *,
    silent_for: timedelta,
    attempts: int = 1,
    heartbeat: bool = True,
    started_ago: timedelta | None = None,
) -> str:
    """Insert a `running` run whose last sign of life was ``silent_for`` ago."""
    now = datetime.now(UTC)
    async with factory() as session:
        run = PipelineRun(
            topic=f"{_TOPIC_PREFIX}{topic}",
            status=RunStatus.RUNNING,
            attempts=attempts,
            started_at=now - (started_ago if started_ago is not None else silent_for),
            heartbeat_at=(now - silent_for) if heartbeat else None,
        )
        session.add(run)
        await session.commit()
        return run.id


async def _row(factory: async_sessionmaker[AsyncSession], run_id: str) -> Any:
    async with factory() as session:
        return (
            await session.execute(
                sa.select(PipelineRun).where(PipelineRun.id == run_id)
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# Slow is not dead.
# ---------------------------------------------------------------------------


async def test_a_live_run_is_left_alone_however_long_it_has_been_going(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The requeue-while-alive bug, stated directly.

    A cold local model plus a provider waiting out its own backoff can put a
    legitimate run well past any fixed duration. Under the old ``started_at``
    cutoff this row was requeued and executed a second time concurrently. A
    fresh heartbeat has to override an ancient start unconditionally, or the
    heartbeat has bought nothing.
    """
    run_id = await _running(
        db, "slow", silent_for=timedelta(seconds=1), started_ago=timedelta(hours=3)
    )

    await _worker()._recover_stale_runs()

    assert (await _row(db, run_id)).status == RunStatus.RUNNING


async def test_silence_past_the_cutoff_is_recovered(
    db: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _running(db, "dead", silent_for=STALE_AFTER + timedelta(seconds=30))

    await _worker()._recover_stale_runs()

    row = await _row(db, run_id)
    assert row.status == RunStatus.QUEUED
    assert row.started_at is None
    assert row.heartbeat_at is None


async def test_the_boundary_is_the_cutoff_not_the_run_duration(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Two runs, same age, opposite fates — decided only by the heartbeat."""
    alive = await _running(
        db, "alive", silent_for=timedelta(seconds=5), started_ago=timedelta(hours=1)
    )
    dead = await _running(
        db, "dead", silent_for=timedelta(minutes=10), started_ago=timedelta(hours=1)
    )

    await _worker()._recover_stale_runs()

    assert (await _row(db, alive)).status == RunStatus.RUNNING
    assert (await _row(db, dead)).status == RunStatus.QUEUED


async def test_a_running_row_with_no_heartbeat_falls_back_to_started_at(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Any row that reached `running` without going through a claim, or that
    died before its first beat landed.

    Without the COALESCE the comparison is against NULL, which is neither true
    nor false — the row never matches and is stuck permanently, which is the
    one outcome this whole mechanism exists to rule out.
    """
    run_id = await _running(
        db, "no-beat", silent_for=timedelta(minutes=10), heartbeat=False
    )

    await _worker()._recover_stale_runs()

    assert (await _row(db, run_id)).status == RunStatus.QUEUED


async def test_queued_and_finished_runs_are_never_touched(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The sweep keys on `running`. A queued run waiting out a retry backoff
    has no heartbeat by design, and re-queueing a succeeded run would hand a
    published article's topic back to the pipeline."""
    async with db() as session:
        queued = PipelineRun(
            topic=f"{_TOPIC_PREFIX}queued",
            status=RunStatus.QUEUED,
            attempts=1,
            next_attempt_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        done = PipelineRun(
            topic=f"{_TOPIC_PREFIX}done",
            status=RunStatus.SUCCEEDED,
            attempts=1,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            finished_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add_all([queued, done])
        await session.commit()
        ids = (queued.id, done.id)

    await _worker()._recover_stale_runs()

    assert (await _row(db, ids[0])).next_attempt_at is not None
    assert (await _row(db, ids[0])).status == RunStatus.QUEUED
    assert (await _row(db, ids[1])).status == RunStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# The budget. A crash costs what a failure costs.
# ---------------------------------------------------------------------------


async def test_recovery_spends_the_same_budget_as_a_retryable_failure(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """``attempts`` is incremented at claim time exactly so this works: the
    crashed attempt is already counted, so the sweep only has to read it."""
    run_id = await _running(db, "budget", silent_for=timedelta(minutes=10), attempts=2)

    await _worker(max_attempts=3)._recover_stale_runs()

    row = await _row(db, run_id)
    assert row.status == RunStatus.QUEUED
    assert row.attempts == 2  # unchanged here; the next claim spends the third


async def test_an_exhausted_run_is_failed_rather_than_resurrected(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The unbounded-lives bug. A run that reliably kills its worker never
    reaches ``_handle_failure``, so the sweep is the only place that can stop
    it — and it terminates in the same state a retryable failure does."""
    run_id = await _running(db, "poison", silent_for=timedelta(minutes=10), attempts=3)

    await _worker(max_attempts=3)._recover_stale_runs()

    row = await _row(db, run_id)
    assert row.status == RunStatus.FAILED
    assert row.finished_at is not None
    assert row.error["attempts_exhausted"] is True


async def test_a_recovered_run_waits_out_a_backoff_before_it_can_be_claimed(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Without this the requeued run is claimed on the next five-second poll,
    so a deterministic crash burns the whole budget in fifteen seconds and the
    console shows three dead attempts before anyone can look at it."""
    run_id = await _running(db, "backoff", silent_for=timedelta(minutes=10), attempts=1)

    before = datetime.now(UTC)
    await _worker()._recover_stale_runs()

    row = await _row(db, run_id)
    expected = before + retry_delay(1)
    assert row.next_attempt_at is not None
    assert abs((row.next_attempt_at - expected).total_seconds()) < 5


async def test_the_backoff_lengthens_with_the_attempt(
    db: async_sessionmaker[AsyncSession],
) -> None:
    first = await _running(db, "first", silent_for=timedelta(minutes=10), attempts=1)
    second = await _running(db, "second", silent_for=timedelta(minutes=10), attempts=2)

    await _worker(max_attempts=4)._recover_stale_runs()

    one = await _row(db, first)
    two = await _row(db, second)
    assert one.next_attempt_at is not None and two.next_attempt_at is not None
    assert two.next_attempt_at > one.next_attempt_at


# ---------------------------------------------------------------------------
# What the console is left with.
# ---------------------------------------------------------------------------


async def test_the_stage_that_was_running_is_closed_out_too(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The run row is not the only thing stuck. A stage row still claiming
    ``running`` under a run that is now queued is a worse lie than either
    alone — the console would show the same run executing two stages at once
    on its next attempt."""
    run_id = await _running(db, "stages", silent_for=timedelta(minutes=10))
    async with db() as session:
        session.add_all(
            [
                PipelineStageRun(
                    run_id=run_id,
                    stage="retrieve",
                    ordinal=1,
                    attempt=1,
                    status=RunStatus.SUCCEEDED,
                    finished_at=datetime.now(UTC),
                ),
                PipelineStageRun(
                    run_id=run_id,
                    stage="synthesize",
                    ordinal=3,
                    attempt=1,
                    status=RunStatus.RUNNING,
                    started_at=datetime.now(UTC),
                ),
            ]
        )
        await session.commit()

    await _worker()._recover_stale_runs()

    async with db() as session:
        rows = (
            await session.execute(
                sa.select(PipelineStageRun.stage, PipelineStageRun.status)
                .where(PipelineStageRun.run_id == run_id)
                .order_by(PipelineStageRun.ordinal)
            )
        ).all()
    assert rows == [("retrieve", RunStatus.SUCCEEDED), ("synthesize", RunStatus.FAILED)]


async def test_the_error_names_the_stage_the_worker_died_in(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The one piece of diagnosis a hard kill otherwise destroys. 'died during
    synthesize' points at a model timeout; 'died during retrieve' points at a
    provider. Without it every crash reads the same."""
    run_id = await _running(db, "named", silent_for=timedelta(minutes=10))
    async with db() as session:
        session.add(
            PipelineStageRun(
                run_id=run_id,
                stage="synthesize",
                ordinal=3,
                attempt=1,
                status=RunStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
        )
        await session.commit()

    await _worker()._recover_stale_runs()

    error = (await _row(db, run_id)).error
    assert error["stage"] == "synthesize"
    assert "synthesize" in error["message"]
    assert error["retrying"] is True


async def test_a_crash_with_no_open_stage_still_records_why_it_was_requeued(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Killed between stages, or before the first one started. The console must
    still distinguish this from a run that was never picked up."""
    run_id = await _running(db, "no-stage", silent_for=timedelta(minutes=10))

    await _worker()._recover_stale_runs()

    error = (await _row(db, run_id)).error
    assert error["stage"] is None
    assert "worker stopped reporting" in error["message"]


# ---------------------------------------------------------------------------
# The heartbeat itself.
# ---------------------------------------------------------------------------


async def test_claiming_a_run_seeds_its_heartbeat(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """So a row is never ``running`` with nothing to compare against — the
    window between the claim and the first beat is otherwise a NULL the sweep
    has to guess at."""
    async with db() as session:
        run = PipelineRun(topic=f"{_TOPIC_PREFIX}claim", status=RunStatus.QUEUED)
        session.add(run)
        await session.commit()
        run_id = run.id

    claimed = await _worker()._claim_next_run()

    assert claimed is not None and claimed[0] == run_id
    row = await _row(db, run_id)
    assert row.heartbeat_at is not None
    assert (datetime.now(UTC) - row.heartbeat_at) < timedelta(seconds=10)


async def test_the_heartbeat_keeps_advancing_while_the_run_is_in_flight(
    db: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _running(db, "beating", silent_for=timedelta(minutes=10))
    original = (await _row(db, run_id)).heartbeat_at

    beat = asyncio.create_task(_worker()._beat(run_id))
    await asyncio.sleep(0.1)
    beat.cancel()

    updated = (await _row(db, run_id)).heartbeat_at
    assert updated is not None and original is not None
    assert updated > original


async def test_a_failed_beat_does_not_kill_the_heartbeat(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heartbeat that dies while its run continues is worse than no
    heartbeat: the run looks abandoned and the next sweep requeues work that
    is still executing. One transient database error must not cause that.
    """
    run_id = await _running(db, "flaky", silent_for=timedelta(minutes=10))
    original = (await _row(db, run_id)).heartbeat_at
    calls = {"n": 0}

    def flaky_get_factory() -> Any:
        """Fails the first beat outright — including the factory lookup, which
        is where the first version of this raised out of the task."""
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection reset")
        return db

    monkeypatch.setattr("app.pipeline.runner.get_session_factory", flaky_get_factory)

    beat = asyncio.create_task(_worker()._beat(run_id))
    await asyncio.sleep(0.15)
    still_running = not beat.done()
    beat.cancel()

    assert still_running
    updated = (await _row(db, run_id)).heartbeat_at
    assert updated is not None and original is not None
    assert updated > original


# ---------------------------------------------------------------------------
# The sweep runs more than once.
# ---------------------------------------------------------------------------


async def test_the_sweep_repeats_rather_than_running_only_at_boot(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The stuck-forever bug.

    Boot-only detection combined with a duration cutoff has a hole it can
    never climb out of: a worker restarted promptly sees a run too young to
    look stale, skips it, and never checks again. The sweep has to recur on a
    timer for a crash to be recoverable at all.
    """
    worker = PipelineWorker(
        poll_interval=0.01, stale_after=STALE_AFTER, sweep_interval=0.0
    )
    sweeps = 0
    original = worker._recover_stale_runs

    async def counting() -> None:
        nonlocal sweeps
        sweeps += 1
        if sweeps >= 3:
            worker.request_shutdown()
        await original()

    worker._recover_stale_runs = counting  # type: ignore[method-assign]

    await asyncio.wait_for(worker.run_forever(), timeout=5)

    assert sweeps >= 3


# ---------------------------------------------------------------------------
# Configuration that would break the mechanism.
# ---------------------------------------------------------------------------


def test_a_staleness_window_too_close_to_the_heartbeat_is_rejected() -> None:
    """Set within one beat of each other, a single slow UPDATE declares a live
    run dead. Caught at startup rather than as an occasional double write."""
    with pytest.raises(ValueError, match="worker_stale_after_seconds"):
        Settings(worker_heartbeat_seconds=15.0, worker_stale_after_seconds=20.0)


def test_the_shipped_defaults_leave_room_for_missed_beats() -> None:
    settings = Settings()

    assert settings.worker_stale_after_seconds >= settings.worker_heartbeat_seconds * 3
