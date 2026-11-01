"""Console endpoints for the research agent. Reviewer auth, router-level.

The desk's "Find trending topics" button and n8n's weekly call both end in
``research/service.py::start_research_run`` — this module and
``api/automation.py`` differ only in who is asking.

A research run proposes. Promoting one of its topics goes through the same
``_enqueue_run`` every other run uses, so a research topic becomes a queued
generation run in exactly the way a typed one does, and the resulting draft
still needs a reviewer's approval to publish (invariant #1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.console.pipeline import _enqueue_run, _run_out
from app.api.console.schemas import (
    DismissCandidateRequest,
    ResearchCandidateOut,
    ResearchRunOut,
    ResearchRunRequest,
    RunOut,
)
from app.api.deps import IdPath, ReviewerDep, SessionDep, SettingsDep, require_reviewer
from app.domain.enums import ResearchCandidateStatus, ResearchRunMode, RunOrigin
from app.domain.models import ResearchCandidate, ResearchRun
from app.research import service
from app.research.service import ResearchAlreadyActive
from app.research.trends.contracts import TrendSearchResult

router = APIRouter(dependencies=[Depends(require_reviewer)], tags=["console:research"])

#: Statuses a reviewer may promote from. Selected is the agent's proposal;
#: shortlisted got the same deep research and lost only on score, which a
#: reviewer is entitled to overrule.
_PROMOTABLE = (ResearchCandidateStatus.SELECTED, ResearchCandidateStatus.SHORTLISTED)


def research_run_out(
    run: ResearchRun, candidates: list[ResearchCandidate] | None = None
) -> ResearchRunOut:
    return ResearchRunOut(
        id=run.id,
        label=run.label,
        mode=run.mode,
        status=run.status,
        stage=run.stage,
        params=run.params,
        provider_status=run.provider_status,
        stage_log=run.stage_log,
        notes=run.notes,
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        candidates=[candidate_out(c) for c in candidates or []],
    )


def candidate_out(candidate: ResearchCandidate) -> ResearchCandidateOut:
    return ResearchCandidateOut(
        id=candidate.id,
        canonical_topic=candidate.canonical_topic,
        category=candidate.category,
        queries=candidate.queries,
        status=candidate.status,
        discard_reason=candidate.discard_reason,
        evidence_status=candidate.evidence_status,
        overall=candidate.overall,
        rank=candidate.rank,
        scores=candidate.scores,
        signals=candidate.signals,
        pipeline_run_id=candidate.pipeline_run_id,
        decided_at=candidate.decided_at,
        dismiss_reason=candidate.dismiss_reason,
        topic_ref_id=candidate.topic_ref_id,
    )


async def start_run(
    session: AsyncSession,
    *,
    mode: ResearchRunMode,
    payload: ResearchRunRequest,
    requested_by: str | None,
    settings: SettingsDep,
) -> ResearchRunOut:
    """Shared by the console and automation routes: one way to start a run."""
    try:
        run = await service.start_research_run(
            session,
            mode=mode,
            params=payload.to_params(),
            requested_by=requested_by,
            settings=settings,
        )
    except ResearchAlreadyActive as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return research_run_out(run)


@router.post(
    "/research/runs", response_model=ResearchRunOut, status_code=status.HTTP_202_ACCEPTED
)
async def create_research_run(
    session: SessionDep,
    reviewer: ReviewerDep,
    settings: SettingsDep,
    payload: ResearchRunRequest | None = None,
) -> ResearchRunOut:
    """Queue a research run now. 202: it takes minutes; poll ``GET``.

    Spends trend-source requests (feeds, Trends, news, web, GDELT, and YouTube
    when keyed), local triage calls, and PubMed/Europe PMC requests
    for the evidence check — no generation run. 409 while another research run
    is queued or running.
    """
    return await start_run(
        session,
        mode=ResearchRunMode.MANUAL,
        payload=payload or ResearchRunRequest(),
        requested_by=reviewer.id,
        settings=settings,
    )


@router.get("/research/runs", response_model=list[ResearchRunOut])
async def list_research_runs(
    session: SessionDep,
    reviewer: ReviewerDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[ResearchRunOut]:
    return [research_run_out(run) for run in await service.list_runs(session, limit)]


@router.get("/research/runs/{run_id}", response_model=ResearchRunOut)
async def get_research_run(
    run_id: IdPath, session: SessionDep, reviewer: ReviewerDep
) -> ResearchRunOut:
    run = await session.get(ResearchRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "research run not found")
    return research_run_out(run, await service.candidates_for(session, run_id))


@router.get("/research/runs/{run_id}/result", response_model=TrendSearchResult)
async def get_research_result(
    run_id: IdPath, session: SessionDep, reviewer: ReviewerDep
) -> TrendSearchResult:
    """The Trend Search Agent's output for a run (snake_case, ``TrendSearchResult``)."""
    return await read_result(session, run_id)


async def read_result(session: AsyncSession, run_id: str) -> TrendSearchResult:
    """Shared by the console and automation routes."""
    run = await session.get(ResearchRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "research run not found")
    return service.build_result(run, await service.candidates_for(session, run_id))


async def _load_promotable(session: AsyncSession, candidate_id: str) -> ResearchCandidate:
    candidate = await session.get(ResearchCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "candidate not found")
    if candidate.status not in _PROMOTABLE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"candidate is {candidate.status.value}; only selected or shortlisted "
            "topics can be promoted or dismissed",
        )
    return candidate


@router.post(
    "/research/candidates/{candidate_id}/promote",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def promote_research_candidate(
    candidate_id: IdPath, session: SessionDep, reviewer: ReviewerDep
) -> RunOut:
    """Turn a proposed topic into a queued generation run.

    The run and the candidate update share the request's transaction, as in
    the MeSH scan's promote: one cannot land without the other.
    """
    candidate = await _load_promotable(session, candidate_id)
    run = await _enqueue_run(
        session,
        topic=candidate.canonical_topic,
        blurb=None,
        reviewer_id=reviewer.id,
        origin=RunOrigin.RESEARCH,
    )
    candidate.status = ResearchCandidateStatus.PROMOTED
    candidate.pipeline_run_id = run.id
    candidate.decided_by = reviewer.id
    candidate.decided_at = datetime.now(UTC)
    await session.flush()
    return _run_out(run, [])


@router.post(
    "/research/candidates/{candidate_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def dismiss_research_candidate(
    candidate_id: IdPath,
    payload: DismissCandidateRequest,
    session: SessionDep,
    reviewer: ReviewerDep,
) -> None:
    """Say no, with a reason. A later run will not re-propose a close match
    to it for ``research_novelty_lookback_days``."""
    candidate = await _load_promotable(session, candidate_id)
    candidate.status = ResearchCandidateStatus.DISMISSED
    candidate.dismiss_reason = payload.reason
    candidate.decided_by = reviewer.id
    candidate.decided_at = datetime.now(UTC)
    await session.flush()
