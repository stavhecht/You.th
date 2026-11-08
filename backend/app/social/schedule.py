"""Enqueues collection runs when they are due. Runs nothing itself.

The same shape as ``discovery/schedule.py``, and for the same reasons:
due-ness is read from the ledger (the last *completed* run of each kind), so a
restart or a deploy loop does not trigger a run, and a slot missed while the
API was down heals on the next tick. The loop never dies on a bad tick.

Two differences. It only inserts a ``collection_runs`` row — the worker
executes it, because the reader's model calls must not share the GPU with a
pipeline run. And a failed run is retried no sooner than ``RETRY_AFTER`` after
it was created, so a broken source is not hammered every tick.

Enqueueing collection is not enqueueing an article: every run here proposes,
and a reviewer promoting a candidate is still the only way a token is spent on
writing.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.discovery.schedule import is_due
from app.domain.models import CollectionRun
from app.social.contracts import CollectionKind
from app.social.service import CollectionAlreadyActive, last_completed_at, start_collection_run

logger = logging.getLogger(__name__)

TICK_SECONDS = 15 * 60
#: An hour short of the nominal interval, so a run that finished a little late
#: yesterday does not push today's a tick later every day.
INTERVALS = {
    CollectionKind.SOCIAL_DAILY: timedelta(hours=23),
    CollectionKind.SOCIAL_WEEKLY: timedelta(days=7) - timedelta(hours=1),
    CollectionKind.PRODUCT_DISCOVERY: timedelta(days=7) - timedelta(hours=1),
}
RETRY_AFTER = timedelta(hours=2)


async def enqueue_due(session: AsyncSession, *, now: datetime) -> list[CollectionKind]:
    """Queue every kind that is due and not already active. Commits nothing."""
    queued = []
    for kind, interval in INTERVALS.items():
        if not is_due(await last_completed_at(session, kind), interval, now=now):
            continue
        last_try = await session.scalar(
            select(func.max(CollectionRun.created_at)).where(CollectionRun.kind == kind.value)
        )
        if last_try is not None and now - last_try < RETRY_AFTER:
            continue
        try:
            await start_collection_run(session, kind)
        except CollectionAlreadyActive:
            continue
        queued.append(kind)
    return queued


class CollectionScheduler:
    def __init__(
        self,
        settings: Settings,
        factory: async_sessionmaker[AsyncSession],
        *,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._tick = tick_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if not self._settings.collection_schedule_enabled:
            logger.info("collection scheduler disabled (collection_schedule_enabled=false)")
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                async with self._factory() as session:
                    queued = await enqueue_due(session, now=datetime.now(UTC))
                    await session.commit()
                if queued:
                    logger.info("queued collection runs: %s", [k.value for k in queued])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("collection scheduler tick failed; continuing")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._tick)
