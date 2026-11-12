"""What the worker is doing, for the desk's status line.

A queued run used to look the same whether the worker was busy, waiting for the
model server, or not running at all: the only signal was a worker log line.
This reads the three queues the worker claims from and when it last reported.
Nothing here is stored; it is all derived from rows that already exist.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.api.deps import SessionDep, require_reviewer
from app.api.schemas_base import CamelModel
from app.domain.enums import ResearchRunStatus, RunStatus
from app.domain.models import CollectionRun, PipelineRun, ResearchRun
from app.social.contracts import CollectionKind

router = APIRouter(dependencies=[Depends(require_reviewer)], tags=["console"])

#: Queued this long with nothing running means nobody is claiming: the worker is
#: stopped, or it is holding work because the model server is not answering. It
#: polls every few seconds, so two minutes is not a slow poll.
STALLED_AFTER_SECONDS = 120


class RunningOut(CamelModel):
    kind: str
    label: str
    heartbeat_age_seconds: int | None


class WorkerStatusOut(CamelModel):
    queued_articles: int
    queued_research: int
    queued_collection: int
    running: list[RunningOut]
    #: Seconds the oldest claimable article run has waited, or null.
    oldest_queued_seconds: int | None
    #: True when work is waiting, nothing is running, and it has waited too long.
    stalled: bool
    last_retraction_sweep_at: datetime | None


def _age(moment: datetime | None, now: datetime) -> int | None:
    return None if moment is None else max(0, int((now - moment).total_seconds()))


@router.get("/status", response_model=WorkerStatusOut)
async def worker_status(session: SessionDep) -> WorkerStatusOut:
    now = datetime.now(UTC)
    queued_articles, oldest = (
        await session.execute(
            select(func.count(), func.min(PipelineRun.created_at)).where(
                PipelineRun.status == RunStatus.QUEUED,
                # A run in retry backoff is not waiting on the worker.
                PipelineRun.next_attempt_at.is_(None) | (PipelineRun.next_attempt_at <= now),
            )
        )
    ).one()
    queued_research = await session.scalar(
        select(func.count()).where(ResearchRun.status == ResearchRunStatus.QUEUED)
    )
    queued_collection = await session.scalar(
        select(func.count()).where(CollectionRun.status == "queued")
    )

    running = [
        RunningOut(kind=kind, label=label, heartbeat_age_seconds=_age(beat, now))
        for kind, query in (
            (
                "article",
                select(PipelineRun.topic, PipelineRun.heartbeat_at).where(
                    PipelineRun.status == RunStatus.RUNNING
                ),
            ),
            (
                "research",
                select(ResearchRun.label, ResearchRun.heartbeat_at).where(
                    ResearchRun.status == ResearchRunStatus.RUNNING
                ),
            ),
            (
                "collection",
                select(CollectionRun.label, CollectionRun.heartbeat_at).where(
                    CollectionRun.status == "running"
                ),
            ),
        )
        for label, beat in (await session.execute(query)).all()
    ]
    waiting = _age(oldest, now)
    return WorkerStatusOut(
        queued_articles=queued_articles,
        queued_research=queued_research or 0,
        queued_collection=queued_collection or 0,
        running=running,
        oldest_queued_seconds=waiting,
        stalled=not running and waiting is not None and waiting > STALLED_AFTER_SECONDS,
        last_retraction_sweep_at=await session.scalar(
            select(func.max(CollectionRun.finished_at)).where(
                CollectionRun.kind == CollectionKind.RETRACTION_SWEEP.value,
                CollectionRun.status == "completed",
            )
        ),
    )
