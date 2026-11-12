"""The collection-run queue: start, claim, heartbeat, recover, finish.

The same shape as ``research/service.py``. ``start_collection_run`` is the one
way a row is created — the scheduler, the desk button and n8n all call it —
and one run may be active per kind, held by the partial unique index
``collection_runs_one_active_per_kind`` when two triggers race.

A collection run never enqueues a pipeline run. Like the trend search and the
MeSH scan, it proposes; a reviewer promoting a candidate is what spends tokens
on an article.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import CollectionRun
from app.social.contracts import CollectionKind

logger = logging.getLogger(__name__)

_ACTIVE = ("queued", "running")


class CollectionAlreadyActive(RuntimeError):
    """A run of this kind is already queued or running."""


async def start_collection_run(session: AsyncSession, kind: CollectionKind) -> CollectionRun:
    """Queue a run. Does not commit — the caller's session does.

    Raises:
        CollectionAlreadyActive: naming the run in the way.
    """
    active = await session.scalar(
        select(CollectionRun.label)
        .where(CollectionRun.kind == kind.value, CollectionRun.status.in_(_ACTIVE))
        .limit(1)
    )
    if active is not None:
        raise CollectionAlreadyActive(f"{active} is already queued or running")
    now = datetime.now(UTC)
    run = CollectionRun(
        label=f"{kind.value}_{now:%Y_%m_%d}_{secrets.token_hex(2)}",
        kind=kind.value,
        status="queued",
        collection_status={},
        stats={},
        notes=[],
        created_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
    except IntegrityError as exc:
        raise CollectionAlreadyActive(
            f"a {kind.value} run is already queued or running (started at the same moment)"
        ) from exc
    logger.info("queued collection run %s", run.label)
    return run


#: Kinds that make no model calls. The daily run is one, and a day it misses is a
#: day of coverage lost, so a model outage must not hold it back.
MODEL_FREE_KINDS = (
    CollectionKind.SOCIAL_DAILY.value,
    CollectionKind.RETRACTION_SWEEP.value,
    CollectionKind.MESH_SCAN.value,
)


async def claim_next(
    factory: async_sessionmaker[AsyncSession], *, model_free_only: bool = False
) -> tuple[str, str, str] | None:
    """Claim the oldest queued run: ``(id, label, kind)``, or None.

    ``model_free_only`` is the worker's choice during a model outage.
    """
    now = datetime.now(UTC)
    query = select(CollectionRun).where(CollectionRun.status == "queued")
    if model_free_only:
        query = query.where(CollectionRun.kind.in_(MODEL_FREE_KINDS))
    async with factory() as session:
        run = (
            await session.execute(
                query
                .order_by(CollectionRun.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if run is None:
            await session.commit()
            return None
        run.status = "running"
        run.started_at = now
        run.heartbeat_at = now
        claimed = (run.id, run.label, run.kind)
        await session.commit()
        return claimed


async def beat(factory: async_sessionmaker[AsyncSession], run_id: str) -> None:
    async with factory() as session:
        await session.execute(
            update(CollectionRun)
            .where(CollectionRun.id == run_id)
            .values(heartbeat_at=datetime.now(UTC))
        )
        await session.commit()


async def recover_stale(
    factory: async_sessionmaker[AsyncSession], stale_after: timedelta
) -> int:
    """Fail runs whose worker stopped reporting. Never requeued: the next
    scheduled run starts fresh, and what this one wrote is already committed."""
    now = datetime.now(UTC)
    async with factory() as session:
        result = await session.execute(
            update(CollectionRun)
            .where(
                CollectionRun.status == "running",
                func.coalesce(CollectionRun.heartbeat_at, CollectionRun.started_at)
                < now - stale_after,
            )
            .values(
                status="failed",
                error={"message": "worker stopped reporting"},
                finished_at=now,
            )
            .returning(CollectionRun.label)
        )
        labels = result.scalars().all()
        await session.commit()
    for label in labels:
        logger.error("collection run %s abandoned by its worker; marked failed", label)
    return len(labels)


async def finish(
    factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    collection_status: dict[str, Any],
    stats: dict[str, Any],
    notes: list[str],
    error: str | None,
) -> None:
    async with factory() as session:
        await session.execute(
            update(CollectionRun)
            .where(CollectionRun.id == run_id)
            .values(
                status="failed" if error else "completed",
                collection_status=collection_status,
                stats=stats,
                notes=notes,
                error={"message": error} if error else None,
                finished_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def last_completed_at(session: AsyncSession, kind: CollectionKind) -> datetime | None:
    return await session.scalar(
        select(func.max(CollectionRun.finished_at)).where(
            CollectionRun.kind == kind.value, CollectionRun.status == "completed"
        )
    )
