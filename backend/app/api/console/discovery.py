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
    ManualScanStatus,
    RunOut,
    SuppressedTopicOut,
    TrendScanOut,
)
from app.api.deps import (
    IdPath,
    ReviewerDep,
    SessionDep,
    require_reviewer,
)
from app.discovery.topics import topic_question
from app.domain.enums import (
    DiscoveryCandidateStatus,
    DiscoveryScanStatus,
    RunOrigin,
)
from app.domain.models import (
    CollectionRun,
    DiscoveryCandidate,
    DiscoveryDescriptor,
    DiscoveryScan,
)
from app.social.contracts import CollectionKind
from app.social.service import CollectionAlreadyActive, start_collection_run

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
        question=topic_question(substance, outcome),
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
    # Locked, like the review writes: two presses both read `proposed` and
    # each queued a run for the same topic.
    candidate = await session.get(
        DiscoveryCandidate, candidate_id, with_for_update=True, populate_existing=True
    )
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


#: The collection run's status, in the words the desk already reads.
_SCAN_STATUS = {
    "queued": ManualScanStatus.RUNNING,
    "running": ManualScanStatus.RUNNING,
    "completed": ManualScanStatus.SUCCEEDED,
    "failed": ManualScanStatus.FAILED,
}


def scan_out(run: CollectionRun | None, last: datetime | None) -> TrendScanOut:
    """The latest ``mesh_scan`` run as the desk's scan state. Pure.

    ``last`` is when a scan last succeeded anywhere (the window ledger), which
    also covers one run from the command line. A queued run reads as running:
    to the reviewer both mean "asked for, not finished".
    """
    if run is None:
        return TrendScanOut(
            status=ManualScanStatus.IDLE,
            started_at=None,
            finished_at=None,
            records_seen=0,
            observations_written=0,
            candidates_proposed=0,
            notes=[],
            suppressed=[],
            error=None,
            last_scan_at=last,
        )
    stats = run.stats or {}
    return TrendScanOut(
        status=_SCAN_STATUS.get(run.status, ManualScanStatus.FAILED),
        started_at=run.started_at or run.created_at,
        finished_at=run.finished_at,
        records_seen=stats.get("records_seen", 0),
        observations_written=stats.get("observations_written", 0),
        candidates_proposed=stats.get("candidates_proposed", 0),
        notes=list(run.notes or []),
        suppressed=[
            SuppressedTopicOut(topic=topic, reason=reason)
            for topic, reason in stats.get("suppressed", [])
        ],
        error=(run.error or {}).get("message"),
        last_scan_at=last,
    )


async def _scan_out(session: AsyncSession) -> TrendScanOut:
    last = await session.scalar(
        select(sa_func.max(DiscoveryScan.finished_at)).where(
            DiscoveryScan.status == DiscoveryScanStatus.SUCCEEDED
        )
    )
    run = await session.scalar(
        select(CollectionRun)
        .where(CollectionRun.kind == CollectionKind.MESH_SCAN.value)
        .order_by(CollectionRun.created_at.desc())
        .limit(1)
    )
    return scan_out(run, last)


@router.get("/discovery/scan", response_model=TrendScanOut)
async def get_scan(session: SessionDep, reviewer: ReviewerDep) -> TrendScanOut:
    """Poll target for the desk's scan button. Two small reads."""
    return await _scan_out(session)


@router.post(
    "/discovery/scan",
    response_model=TrendScanOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_scan(session: SessionDep, reviewer: ReviewerDep) -> TrendScanOut:
    """Queue the trend scan now, instead of waiting for its slot.

    202 with the state: the worker runs it (minutes of PubMed requests), and
    the desk polls ``GET`` until it stops saying ``running``. In the worker so
    it shares the worker's PubMed rate limit; see ``app/discovery/CLAUDE.md``.

    Safe to expose because a scan **proposes and nothing else** — no model
    calls, no queued article run, no spend.

    409 while one is already queued or running.
    """
    try:
        await start_collection_run(session, CollectionKind.MESH_SCAN)
    except CollectionAlreadyActive as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _scan_out(session)
