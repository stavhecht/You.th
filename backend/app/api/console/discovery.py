"""Trend discovery: the MeSH scan's proposals, promoting and dismissing them."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.console.pipeline import _enqueue_run, _run_out
from app.api.console.schemas import (
    DiscoveryCandidateOut,
    DismissCandidateRequest,
    RunOut,
    SuppressedTopicOut,
    TrendScanOut,
)
from app.api.deps import (
    IdPath,
    ReviewerDep,
    SessionDep,
    SettingsDep,
    require_reviewer,
)
from app.db import get_session_factory
from app.discovery.manual import ScanAlreadyRunning, manual_scan
from app.domain.enums import (
    DiscoveryCandidateStatus,
    DiscoveryScanStatus,
    RunOrigin,
)
from app.domain.models import (
    DiscoveryCandidate,
    DiscoveryDescriptor,
    DiscoveryScan,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_reviewer)], tags=["console"])


#
# The desk half of `scripts/scan_trends.py`. The scan proposes; a reviewer
# decides. Nothing here is reachable by the scan itself, which is the point:
# promoting a candidate spends a full generation run, and that spend is a human
# decision — the same principle as invariant #1, one step earlier in the chain.


def _candidate_out(
    candidate: DiscoveryCandidate, substance: str, outcome: str | None
) -> DiscoveryCandidateOut:
    rationale = candidate.rationale or {}
    return DiscoveryCandidateOut(
        id=candidate.id,
        topic=candidate.topic,
        substance_ui=candidate.substance_ui,
        substance_name=substance,
        outcome_name=outcome,
        score=candidate.score,
        paper_count=candidate.paper_count,
        # Falls back to the substance count for rows written before angles
        # existed, so an old proposal renders as a whole-substance one rather
        # than as an article resting on zero papers.
        angle_paper_count=int(
            rationale.get("angle_paper_count") or candidate.paper_count
        ),
        baseline_count=candidate.baseline_count,
        lift=float(rationale.get("lift", 1.0)),
        study_mix=rationale.get("study_mix", {}),
        top_pmids=rationale.get("top_pmids", []),
        status=candidate.status,
        pipeline_run_id=candidate.pipeline_run_id,
        scanned_at=candidate.created_at,
    )


@router.get("/discovery/candidates", response_model=list[DiscoveryCandidateOut])
async def list_candidates(
    session: SessionDep,
    reviewer: ReviewerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[DiscoveryCandidateOut]:
    """Live proposals, best first.

    Unpaginated with a capped limit, like the contact inbox: the scan writes at
    most `discovery_max_candidates` per run and expires what it no longer ranks,
    so this list is bounded by design rather than by the query.
    """
    substance = aliased(DiscoveryDescriptor)
    outcome = aliased(DiscoveryDescriptor)
    rows = await session.execute(
        select(DiscoveryCandidate, substance.name, outcome.name)
        .join(substance, substance.ui == DiscoveryCandidate.substance_ui)
        .outerjoin(outcome, outcome.ui == DiscoveryCandidate.outcome_ui)
        .where(DiscoveryCandidate.status == DiscoveryCandidateStatus.PROPOSED)
        .order_by(DiscoveryCandidate.score.desc())
        .limit(limit)
    )
    return [
        _candidate_out(candidate, substance_name, outcome_name)
        for candidate, substance_name, outcome_name in rows
    ]


async def _load_proposed(session: AsyncSession, candidate_id: str) -> DiscoveryCandidate:
    candidate = await session.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "candidate not found")
    if candidate.status is not DiscoveryCandidateStatus.PROPOSED:
        # 409, not 404: it exists and the reviewer can see it — someone else
        # already acted on it, or a later scan expired it. Saying "not found"
        # would send them looking for a bug.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"candidate is {candidate.status.value}, not proposed",
        )
    return candidate


@router.post(
    "/discovery/candidates/{candidate_id}/promote",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def promote_candidate(
    candidate_id: IdPath, session: SessionDep, reviewer: ReviewerDep
) -> RunOut:
    """Turn a proposal into a queued generation run.

    Goes through ``_enqueue_run`` — the same insert ``create_run`` uses — rather
    than writing its own row, so there is one definition of what a queued run
    is. The candidate's ``topic`` is taken verbatim rather than recomposed: what
    the reviewer read is what runs.

    Both writes are in the request's transaction, so a run without its candidate
    update (or the reverse) is not a state this can produce.
    """
    candidate = await _load_proposed(session, candidate_id)
    run = await _enqueue_run(
        session,
        topic=candidate.topic,
        blurb=None,
        reviewer_id=reviewer.id,
        origin=RunOrigin.DISCOVERY,
    )
    candidate.status = DiscoveryCandidateStatus.PROMOTED
    candidate.pipeline_run_id = run.id
    candidate.decided_by = reviewer.id
    candidate.decided_at = datetime.now(UTC)
    await session.flush()
    return _run_out(run, [])


@router.post(
    "/discovery/candidates/{candidate_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def dismiss_candidate(
    candidate_id: IdPath,
    payload: DismissCandidateRequest,
    session: SessionDep,
    reviewer: ReviewerDep,
) -> None:
    """Say no, with a reason.

    The reason is stored and shown in the next scan's dry-run output, and the
    substance is suppressed until both the cooloff elapses *and* its literature
    has doubled — see ``discovery/service.py::suppression_reason``. Without both
    gates a dismissed topic returns every fortnight with two more papers, which
    is how a proposal queue teaches people to stop reading it.
    """
    candidate = await _load_proposed(session, candidate_id)
    candidate.status = DiscoveryCandidateStatus.DISMISSED
    candidate.dismiss_reason = payload.reason
    candidate.decided_by = reviewer.id
    candidate.decided_at = datetime.now(UTC)
    await session.flush()


async def _scan_out(session: AsyncSession) -> TrendScanOut:
    """This process's last press, plus when a scan last succeeded anywhere.

    The two halves answer different questions and neither substitutes for the
    other: the runner knows whether the button is busy *now*, and only the
    ledger knows the cron slot ran on Monday.
    """
    last = await session.scalar(
        select(sa_func.max(DiscoveryScan.finished_at)).where(
            DiscoveryScan.status == DiscoveryScanStatus.SUCCEEDED
        )
    )
    state = manual_scan.state
    return TrendScanOut(
        status=state.status,
        started_at=state.started_at,
        finished_at=state.finished_at,
        records_seen=state.records_seen,
        observations_written=state.observations_written,
        candidates_proposed=state.candidates_proposed,
        notes=state.notes,
        suppressed=[
            SuppressedTopicOut(topic=item.topic, reason=item.reason)
            for item in state.suppressed
        ],
        error=state.error,
        last_scan_at=last,
    )


@router.get("/discovery/scan", response_model=TrendScanOut)
async def get_scan(session: SessionDep, reviewer: ReviewerDep) -> TrendScanOut:
    """Poll target for the desk's scan button. Cheap by design — one MAX()."""
    return await _scan_out(session)


@router.post(
    "/discovery/scan",
    response_model=TrendScanOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_scan(
    session: SessionDep, reviewer: ReviewerDep, settings: SettingsDep
) -> TrendScanOut:
    """Run the trend scan now, instead of waiting for the cron slot.

    202 with the state, not 200 with a result: the scan is minutes of PubMed
    requests and the reviewer's connection is not going to be held open for it.
    The desk polls ``GET`` until it stops saying ``running``.

    Safe to expose because a scan **proposes and nothing else** — no model
    calls, no queued run, no spend. Promoting what it finds is still a human
    decision, which is the whole shape of this feature.

    409 rather than a second task when one is already in flight: two concurrent
    scans double this process's draw on NCBI's per-IP ceiling — shared with the
    worker — to compute the same answer twice.
    """
    try:
        manual_scan.start(settings=settings, factory=get_session_factory())
    except ScanAlreadyRunning as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _scan_out(session)
