"""Queueing draft generation and reading its runs."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.console.schemas import (
    CreateRunRequest,
    RunOut,
    RunPageOut,
    StageRunOut,
)
from app.api.deps import (
    IdPath,
    ReviewerDep,
    SessionDep,
    require_reviewer,
)
from app.domain.enums import (
    RunOrigin,
    RunStatus,
)
from app.domain.models import (
    PipelineRun,
    PipelineStageRun,
)
from app.llm.base import TokenUsage
from app.llm.pricing import cost_usd, total_cost_usd

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_reviewer)], tags=["console"])


@router.post(
    "/pipeline/runs", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED
)
async def create_run(
    payload: CreateRunRequest, session: SessionDep, reviewer: ReviewerDep
) -> RunOut:
    """Enqueue a topic for draft generation.

    Inserts a ``queued`` run and returns immediately — generation takes
    minutes, so this is 202 and the client polls the runs list. The worker
    picks it up.

    Note that no response from this endpoint can ever produce a published
    article; the run's terminal state is a draft awaiting a human.
    """
    run = await _enqueue_run(
        session,
        topic=payload.topic,
        blurb=payload.blurb,
        reviewer_id=reviewer.id,
        origin=RunOrigin.CONSOLE,
    )
    return _run_out(run, [])


async def _enqueue_run(
    session: AsyncSession,
    *,
    topic: str,
    blurb: str | None,
    reviewer_id: str,
    origin: RunOrigin,
) -> PipelineRun:
    """The one place a pipeline run is created.

    Extracted so promoting a trend candidate goes through the same insert as a
    reviewer typing a topic, rather than becoming a second path that can drift.
    It stays here, next to the route it came from, rather than moving into a
    service: ``create_run`` has no service layer today, and adding one for two
    callers would produce two shapes again with a layer in between.

    Does not commit — the router's session does, on a clean return, so a
    promotion's run and its candidate update land together or not at all.
    """
    run = PipelineRun(
        topic=topic,
        source_blurb=blurb,
        status=RunStatus.QUEUED,
        requested_by=reviewer_id,
        origin=origin,
        # Attempts are counted when a worker claims the run, so a queued one
        # has had none. Set explicitly rather than left to the server default,
        # which is not populated on the instance until it is re-read.
        attempts=0,
        created_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    logger.info("queued pipeline run %s (%s) for topic=%r", run.id, origin.value, topic)
    return run


@router.get("/pipeline/runs", response_model=RunPageOut)
async def list_runs(
    session: SessionDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RunPageOut:
    """Run history with per-stage status, newest first."""
    statement = select(PipelineRun).order_by(
        PipelineRun.created_at.desc(), PipelineRun.id.desc()
    )

    if cursor:
        try:
            anchor = await session.get(PipelineRun, str(uuid.UUID(cursor)))
        except ValueError:  # not a UUID: Postgres would refuse it with a 500
            anchor = None
        if anchor is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed cursor"
            )
        statement = statement.where(
            tuple_(PipelineRun.created_at, PipelineRun.id) < (anchor.created_at, anchor.id)
        )

    result = await session.execute(statement.limit(limit + 1))
    runs = list(result.scalars())

    next_cursor = None
    if len(runs) > limit:
        runs = runs[:limit]
        next_cursor = runs[-1].id

    stages_by_run = await _load_stages(session, [run.id for run in runs])
    return RunPageOut(
        items=[_run_out(run, stages_by_run.get(run.id, [])) for run in runs],
        next_cursor=next_cursor,
    )


@router.get("/pipeline/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: IdPath, session: SessionDep) -> RunOut:
    """One run: every stage, its error payload, timing and token cost.

    This is the answer to "why did last night's batch produce nothing" —
    without it, a validation failure and a crashed provider look identical from
    the outside.
    """
    run = await session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    stages_by_run = await _load_stages(session, [run_id])
    return _run_out(run, stages_by_run.get(run_id, []))


async def _load_stages(
    session: SessionDep, run_ids: list[str]
) -> dict[str, list[PipelineStageRun]]:
    """Stage rows for each run — the latest attempt only.

    A retried run holds one set of rows per attempt (they are kept, not
    overwritten, so a run that succeeded on its third try still shows what the
    first two did). Returning all of them would render the pipeline as six
    stages repeated N times, which reads as a bug. The console's question is
    "where is this run now", and the latest attempt answers it; ``attempts`` on
    the run says how many there were, and the earlier rows stay in the table
    for anyone debugging.
    """
    if not run_ids:
        return {}
    result = await session.execute(
        select(PipelineStageRun)
        .where(PipelineStageRun.run_id.in_(run_ids))
        .order_by(PipelineStageRun.attempt, PipelineStageRun.ordinal)
    )
    grouped: dict[str, list[PipelineStageRun]] = {}
    latest: dict[str, int] = {}
    for stage in result.scalars():
        # Ordered by attempt, so a higher one supersedes what we have so far.
        if stage.attempt > latest.get(stage.run_id, 0):
            latest[stage.run_id] = stage.attempt
            grouped[stage.run_id] = []
        grouped.setdefault(stage.run_id, []).append(stage)
    return grouped


def _stage_usage(stage: PipelineStageRun) -> TokenUsage:
    return TokenUsage(
        input_tokens=stage.input_tokens,
        output_tokens=stage.output_tokens,
        cache_read_tokens=stage.cache_read_tokens,
        cache_write_tokens=stage.cache_write_tokens,
    )


def _run_out(run: PipelineRun, stages: list[PipelineStageRun]) -> RunOut:
    # Cost is derived here rather than stored, so a price correction fixes
    # history instead of leaving it wrong — see app/llm/pricing.py. Summed from
    # the stage rows because the run's own totals span models and cannot be
    # priced; note the stage list is the *latest attempt only*, so a retried
    # run's cost is deliberately lower than its token totals imply.
    stage_costs = total_cost_usd(
        [(stage.model or "", _stage_usage(stage)) for stage in stages]
    )
    return RunOut(
        id=run.id,
        topic=run.topic,
        status=run.status,
        article_id=run.article_id,
        error=run.error,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cache_read_tokens=run.cache_read_tokens,
        cache_write_tokens=run.cache_write_tokens,
        estimated_cost_usd=stage_costs,
        attempts=run.attempts,
        next_attempt_at=run.next_attempt_at,
        heartbeat_at=run.heartbeat_at,
        stages=[
            StageRunOut(
                stage=stage.stage,
                ordinal=stage.ordinal,
                status=stage.status,
                error=stage.error,
                metrics=stage.metrics,
                model=stage.model,
                input_tokens=stage.input_tokens,
                output_tokens=stage.output_tokens,
                cache_read_tokens=stage.cache_read_tokens,
                cache_write_tokens=stage.cache_write_tokens,
                estimated_cost_usd=cost_usd(stage.model or "", _stage_usage(stage)),
                started_at=stage.started_at,
                finished_at=stage.finished_at,
            )
            for stage in stages
        ],
        created_at=run.created_at,
        finished_at=run.finished_at,
    )
