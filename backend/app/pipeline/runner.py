"""The local pipeline worker.

A separate process that polls ``pipeline_runs`` for queued work and drives the
orchestrator. Separate from the API process on purpose: a generation run takes
minutes, and a request-scoped background task ties that lifetime to a web
worker that may be recycled mid-run.

Production path (DESIGN.md §10): this file goes away. EventBridge triggers a
Step Functions execution, each stage becomes a Lambda, and the polling loop
becomes the state machine. The orchestrator and the stages are unchanged —
only this dispatch shell is local-only.

Run with:  python -m app.pipeline.runner
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.domain.enums import RunStatus
from app.domain.models import PipelineRun, PipelineStageRun
from app.logging_setup import configure_logging
from app.pipeline.orchestrator import (
    PipelineOrchestrator,
    build_default_pipeline,
    retry_delay,
)
from app.research import service as research_service
from app.research.runner import execute_research_run

logger = logging.getLogger(__name__)


class PipelineWorker:
    """Claims queued runs, drives them, and reports that it is still alive.

    The liveness half is not decoration. A run row says ``running`` because a
    worker said so, and nothing retracts that if the worker dies — the console
    then reports work no process is doing, forever. Recovering it needs a way
    to tell a dead worker from a slow one, which is what the heartbeat is for
    (``pipeline_runs.heartbeat_at``).
    """

    def __init__(
        self,
        poll_interval: float = 5.0,
        *,
        heartbeat_interval: float = 15.0,
        stale_after: timedelta = timedelta(minutes=2),
        sweep_interval: float = 60.0,
        max_attempts: int = 3,
    ) -> None:
        self._poll_interval = poll_interval
        self._heartbeat_interval = heartbeat_interval
        self._stale_after = stale_after
        self._sweep_interval = sweep_interval
        self._max_attempts = max_attempts
        self._shutdown = asyncio.Event()

    def request_shutdown(self) -> None:
        """Signal handler target. The in-flight run is allowed to finish."""
        logger.info("shutdown requested; finishing in-flight run")
        self._shutdown.set()

    async def run_forever(self) -> None:
        """Poll for queued runs and execute them until shut down."""
        settings = get_settings()
        last_sweep = float("-inf")  # the first pass sweeps, before any claim

        logger.info("worker started (poll interval %.1fs)", self._poll_interval)
        while not self._shutdown.is_set():
            # Between polls, never during a run — so this worker cannot sweep
            # its own in-flight run, whatever its heartbeat is doing. That is a
            # property of where the call sits, and the reason it is not a
            # background task: a concurrent sweep would race the heartbeat and
            # could requeue a run that is still executing.
            #
            # Every database call in this loop is guarded: a Postgres restart
            # used to raise out of here and end the process, leaving queued
            # runs waiting on a worker that was gone.
            if time.monotonic() - last_sweep >= self._sweep_interval:
                last_sweep = time.monotonic()
                try:
                    await self._recover_stale_runs()
                except Exception:
                    logger.exception("could not sweep stale runs; retrying next sweep")

            # Research runs first, and in this process rather than their own:
            # both kinds count papers against NCBI's per-IP ceiling, and one
            # process running one thing at a time is what keeps the throttle's
            # budget true. See research/runner.py.
            research = await self._claim_research_run()
            if research is not None:
                await self._run_research(*research, settings=settings)
                continue

            try:
                run = await self._claim_next_run()
            except Exception:
                logger.exception(
                    "could not claim a queued run; retrying in %.0fs", self._poll_interval
                )
                run = None
            if run is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._shutdown.wait(), timeout=self._poll_interval
                    )
                continue

            run_id, topic, blurb, attempt = run
            logger.info("claimed run %s (topic=%r, attempt %d)", run_id, topic, attempt)

            factory = get_session_factory()
            heartbeat = asyncio.create_task(self._beat(run_id))
            try:
                async with factory() as session:
                    stages = build_default_pipeline(session, settings)
                    orchestrator = PipelineOrchestrator(
                        session, stages, factory, settings.pipeline_max_attempts
                    )
                    # No commit here: the orchestrator owns every boundary and
                    # commits per stage. A blanket commit at this level would
                    # also commit the partial writes of a stage that failed —
                    # the orchestrator rolls those back deliberately.
                    await orchestrator.run(run_id, topic, blurb, attempt)
            except Exception:
                # The orchestrator records stage-level failures itself; this
                # catches anything that escapes it (a broken pipeline build, a
                # dead database) so one bad run cannot kill the worker.
                logger.exception("run %s crashed outside the orchestrator", run_id)
                try:
                    await self._mark_failed(run_id, "worker crashed outside the orchestrator")
                except Exception:
                    # Its heartbeat has stopped, so the stale sweep recovers it.
                    logger.exception("could not mark run %s failed", run_id)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

        logger.info("worker stopped")

    async def _claim_research_run(self) -> tuple[str, str] | None:
        try:
            return await research_service.claim_next(get_session_factory())
        except Exception:
            # A missing table (0005 not applied) or a transient error must not
            # stop article runs from being claimed.
            logger.exception("could not check for queued research runs")
            return None

    async def _run_research(self, run_id: str, label: str, *, settings: Any) -> None:
        logger.info("claimed research run %s", label)
        factory = get_session_factory()
        heartbeat = asyncio.create_task(self._beat_research(run_id))
        try:
            await execute_research_run(run_id, settings, factory)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _beat_research(self, run_id: str) -> None:
        """The research run's heartbeat. Same rule as ``_beat``: never raises."""
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            try:
                await research_service.beat(get_session_factory(), run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("heartbeat for research run %s failed", run_id, exc_info=True)

    async def _beat(self, run_id: str) -> None:
        """Report the run alive until cancelled.

        On a timer rather than between stages: a single synthesis call against
        a cold local model can run for minutes, so a per-stage ping would need
        a staleness window longer than the slowest stage — which is the
        coarse timeout this replaced.

        **Failures here are logged and retried, never raised.** A heartbeat
        that dies while its run continues leaves the run looking abandoned,
        and the next sweep requeues work that is still executing. A transient
        database error must not be able to cause that, so the loop keeps
        beating; if the database is genuinely gone, the run's own next write
        fails and the orchestrator handles it.
        """
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            try:
                # Resolved per beat, inside the try, so that even acquiring the
                # factory is covered: raising out of this task stops the pings
                # while the run continues, and a run that stops reporting is
                # requeued and executed twice. Nothing here may escape.
                factory = get_session_factory()
                beat_at = datetime.now(UTC)
                async with factory() as session:
                    await session.execute(
                        update(PipelineRun)
                        .where(PipelineRun.id == run_id)
                        .values(heartbeat_at=beat_at)
                    )
                    await session.commit()
                # Logged after the commit, so the line is evidence the write
                # landed rather than that the attempt was made — the failure
                # this exists to make visible is a heartbeat that stops while
                # its run continues, and a pre-commit log would still print on
                # the beat that did not reach the database. INFO because a beat
                # only fires under an in-flight run, so an idle worker stays
                # quiet; the interval bounds the volume at one line per
                # `WORKER_HEARTBEAT_SECONDS` of work.
                logger.info(
                    "heartbeat for run %s at %s (next in %.0fs)",
                    run_id,
                    beat_at.isoformat(timespec="seconds"),
                    self._heartbeat_interval,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "heartbeat for run %s failed; retrying in %.0fs",
                    run_id,
                    self._heartbeat_interval,
                    exc_info=True,
                )

    async def _claim_next_run(self) -> tuple[str, str, str | None, int] | None:
        """Atomically claim one queued run, returning its attempt number.

        ``FOR UPDATE SKIP LOCKED`` is correct from the start even though one
        worker runs one run at a time: it costs nothing, and it means a second
        worker process is a deployment change rather than a correctness change.

        ``next_attempt_at`` holds back a run the orchestrator requeued after a
        retryable failure. Without it the next poll — five seconds later —
        re-claims a run whose provider asked us to wait a minute, spending the
        attempt budget on the same 429 three times in fifteen seconds.

        ``attempts`` is incremented *here*, when the attempt begins, so a
        worker killed mid-run has still consumed its try. Counting on
        completion instead would let a run that reliably kills its worker cycle
        forever.
        """
        factory = get_session_factory()
        now = datetime.now(UTC)
        async with factory() as session:
            result = await session.execute(
                select(PipelineRun)
                .where(
                    PipelineRun.status == RunStatus.QUEUED,
                    or_(
                        PipelineRun.next_attempt_at.is_(None),
                        PipelineRun.next_attempt_at <= now,
                    ),
                )
                .order_by(PipelineRun.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            run = result.scalar_one_or_none()
            if run is None:
                await session.commit()
                return None

            run.status = RunStatus.RUNNING
            run.started_at = now
            run.attempts += 1
            # Seeded here so the row is never `running` with no heartbeat: the
            # sweep would otherwise have to decide what a NULL means, and both
            # readings are wrong (fresh forever, or dead on arrival).
            run.heartbeat_at = now
            claimed = (run.id, run.topic, run.source_blurb, run.attempts)
            await session.commit()
            return claimed

    async def _recover_stale_runs(self) -> None:
        """Requeue — or finally fail — runs whose worker stopped reporting.

        A hard kill leaves the row in ``running`` with nobody driving it. Left
        alone that is a lie the console displays forever; requeued blindly it
        is an infinite supply of lives for a run that kills every worker that
        touches it. So this consults ``attempts``, which is incremented when a
        run is *claimed* precisely so a crash costs the same as a failure:

        * budget left → requeued behind ``retry_delay``, so a run that just
          killed a worker is not re-claimed on the next five-second poll;
        * budget spent → failed with ``attempts_exhausted``, the same terminal
          state a retryable failure reaches, and for the same reason.

        ``FOR UPDATE SKIP LOCKED`` because a second worker may be sweeping the
        same rows; whoever gets there first recovers them.
        """
        cutoff = datetime.now(UTC) - self._stale_after
        factory = get_session_factory()
        try:
            await research_service.recover_stale(factory, self._stale_after)
        except Exception:
            logger.exception("could not sweep stale research runs")
        async with factory() as session:
            stale = (
                await session.execute(
                    select(PipelineRun.id, PipelineRun.topic, PipelineRun.attempts)
                    .where(
                        PipelineRun.status == RunStatus.RUNNING,
                        # started_at and created_at are fallbacks for a row that
                        # somehow reached `running` without a claim. Without
                        # them a NULL heartbeat compares as NULL, the row never
                        # matches, and it is stuck forever — the exact failure
                        # this method exists to end.
                        func.coalesce(
                            PipelineRun.heartbeat_at,
                            PipelineRun.started_at,
                            PipelineRun.created_at,
                        )
                        < cutoff,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()

            for run_id, topic, attempts in stale:
                stage = await self._abandon_stage_rows(session, run_id)
                where = "" if stage is None else f" during {stage}"
                if attempts >= self._max_attempts:
                    logger.error(
                        "run %s (topic=%r) abandoned%s on attempt %d/%d; no budget "
                        "left, failing it",
                        run_id,
                        topic,
                        where,
                        attempts,
                        self._max_attempts,
                    )
                    await session.execute(
                        update(PipelineRun)
                        .where(PipelineRun.id == run_id)
                        .values(
                            status=RunStatus.FAILED,
                            error={
                                "stage": stage,
                                "message": (
                                    f"worker stopped reporting{where}; no attempts left"
                                ),
                                "attempt": attempts,
                                "attempts_exhausted": True,
                            },
                            finished_at=datetime.now(UTC),
                        )
                    )
                    continue

                delay = retry_delay(attempts)
                logger.warning(
                    "run %s (topic=%r) abandoned%s on attempt %d/%d; requeued for "
                    "%.0fs from now",
                    run_id,
                    topic,
                    where,
                    attempts,
                    self._max_attempts,
                    delay.total_seconds(),
                )
                await session.execute(
                    update(PipelineRun)
                    .where(PipelineRun.id == run_id)
                    .values(
                        status=RunStatus.QUEUED,
                        error={
                            "stage": stage,
                            "message": f"worker stopped reporting{where}",
                            "attempt": attempts,
                            "retrying": True,
                        },
                        started_at=None,
                        heartbeat_at=None,
                        next_attempt_at=datetime.now(UTC) + delay,
                    )
                )

            await session.commit()

    async def _abandon_stage_rows(self, session: AsyncSession, run_id: str) -> str | None:
        """Close out the stage rows the dead worker left open.

        The run row is not the only thing stuck saying ``running`` — so is
        whichever stage was executing, and a stage that claims to be running
        under a run that is queued is worse than either lie alone. Returns the
        stage name so the run's error can say where it died, which is the one
        piece of diagnosis a crash otherwise destroys.
        """
        abandoned = (
            await session.execute(
                update(PipelineStageRun)
                .where(
                    PipelineStageRun.run_id == run_id,
                    PipelineStageRun.status == RunStatus.RUNNING,
                )
                .values(
                    status=RunStatus.FAILED,
                    error={
                        "type": "WorkerAbandoned",
                        "message": "worker stopped reporting while this stage was running",
                    },
                    finished_at=datetime.now(UTC),
                )
                .returning(PipelineStageRun.stage)
            )
        ).scalars().all()
        return abandoned[-1] if abandoned else None

    async def _mark_failed(self, run_id: str, message: str) -> None:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(
                update(PipelineRun)
                .where(PipelineRun.id == run_id)
                .values(
                    status=RunStatus.FAILED,
                    error={"message": message},
                    finished_at=datetime.now(UTC),
                )
            )
            await session.commit()


async def main() -> None:
    configure_logging(logging.INFO)
    settings = get_settings()
    worker = PipelineWorker(
        poll_interval=settings.worker_poll_interval_seconds,
        heartbeat_interval=settings.worker_heartbeat_seconds,
        stale_after=timedelta(seconds=settings.worker_stale_after_seconds),
        sweep_interval=settings.worker_sweep_interval_seconds,
        max_attempts=settings.pipeline_max_attempts,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.request_shutdown)

    try:
        await worker.run_forever()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
