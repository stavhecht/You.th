"""Console endpoints for social listening, product discovery and their relations.

Reviewer auth, router-level. Starting a collection run queues work for the
worker and spends API quota and local model time — never a generation run.
Promoting a social candidate goes through the same ``_enqueue_run`` every other
promotion uses (origin ``social``), and the draft it produces still needs a
reviewer's approval to publish (invariant #1). Products are review-only.

``GET /social/result`` is snake_case on purpose, like the trend search's
result: it is the module's output schema, not a console record.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.console.pipeline import _enqueue_run, _run_out
from app.api.console.schemas import DismissCandidateRequest, RunOut
from app.api.deps import IdPath, ReviewerDep, SessionDep, require_reviewer
from app.api.schemas_base import UUID_PATTERN, CamelModel
from app.domain.enums import RunOrigin
from app.domain.models import (
    CollectionRun,
    Product,
    ProductObservation,
    SocialCandidate,
    SocialTopic,
)
from app.intel.related import related_to_product, related_to_topic
from app.products.store import source_identity, weekly_mentions
from app.social import service
from app.social.analytics import WINDOW_DAYS, WINDOWS
from app.social.contracts import CollectionKind, SocialListeningResult
from app.social.result import build_result, latest_weekly_run

router = APIRouter(dependencies=[Depends(require_reviewer)], tags=["console:social"])


class CollectionRunRequest(BaseModel):
    kind: CollectionKind


class CollectionRunOut(CamelModel):
    id: str
    label: str
    kind: str
    status: str
    collection_status: dict[str, Any]
    stats: dict[str, Any]
    notes: list[str]
    error: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ProductOut(CamelModel):
    id: str
    brand: str
    name: str
    category: str
    status: str
    launch_status: str
    official_url: str | None
    fda_reference: dict[str, Any] | None
    first_seen_at: datetime
    last_seen_at: datetime
    independent_sources: int
    mentions: int
    weeks: list[dict[str, Any]]
    recent: list[dict[str, Any]]


async def queue_run(session: AsyncSession, kind: CollectionKind) -> CollectionRunOut:
    """Shared by the console and automation routes."""
    try:
        run = await service.start_collection_run(session, kind)
    except service.CollectionAlreadyActive as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return CollectionRunOut.model_validate(run)


@router.post(
    "/collection/runs", response_model=CollectionRunOut, status_code=status.HTTP_202_ACCEPTED
)
async def create_collection_run(
    payload: CollectionRunRequest, session: SessionDep
) -> CollectionRunOut:
    """Queue a collection run now. 409 while one of the same kind is active."""
    return await queue_run(session, payload.kind)


@router.get("/collection/runs", response_model=list[CollectionRunOut])
async def list_collection_runs(
    session: SessionDep,
    kind: Annotated[CollectionKind | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[CollectionRunOut]:
    query = select(CollectionRun).order_by(CollectionRun.created_at.desc()).limit(limit)
    if kind is not None:
        query = query.where(CollectionRun.kind == kind.value)
    return [
        CollectionRunOut.model_validate(r) for r in (await session.execute(query)).scalars()
    ]


@router.get("/social/result", response_model=SocialListeningResult)
async def read_social_result(session: SessionDep) -> SocialListeningResult:
    """The latest completed weekly social run's proposals."""
    run = await latest_weekly_run(session)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no weekly social run found")
    return await build_result(session, run)


async def _proposed(session: AsyncSession, candidate_id: str) -> SocialCandidate:
    candidate = await session.get(SocialCandidate, candidate_id, with_for_update=True)
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "social candidate not found")
    if candidate.status != "proposed":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"candidate is already {candidate.status}"
        )
    return candidate


@router.post(
    "/social/candidates/{candidate_id}/promote",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def promote_social_candidate(
    candidate_id: IdPath, session: SessionDep, reviewer: ReviewerDep
) -> RunOut:
    """Turn a social topic into a queued generation run, in one transaction."""
    candidate = await _proposed(session, candidate_id)
    topic = await session.get(SocialTopic, candidate.topic_id)
    assert topic is not None  # FK
    run = await _enqueue_run(
        session,
        topic=topic.question or topic.label,
        blurb=None,
        reviewer_id=reviewer.id,
        origin=RunOrigin.SOCIAL,
    )
    candidate.status = "promoted"
    candidate.pipeline_run_id = run.id
    candidate.decided_by = reviewer.id
    candidate.decided_at = datetime.now(UTC)
    await session.flush()
    return _run_out(run, [])


@router.post(
    "/social/candidates/{candidate_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT
)
async def dismiss_social_candidate(
    candidate_id: IdPath,
    payload: DismissCandidateRequest,
    session: SessionDep,
    reviewer: ReviewerDep,
) -> None:
    """Say no, with a reason. It returns after the cooloff, or sooner if its creators double."""
    candidate = await _proposed(session, candidate_id)
    candidate.status = "dismissed"
    candidate.dismiss_reason = payload.reason
    candidate.decided_by = reviewer.id
    candidate.decided_at = datetime.now(UTC)
    await session.flush()


@router.get("/products", response_model=list[ProductOut])
async def list_products(
    session: SessionDep,
    category: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[ProductOut]:
    """Products, most recently seen first, with their record. Relations are
    ``/intel/related?kind=product``, asked for one product at a time: computing
    them for every row cost hundreds of queries per page."""
    query = select(Product).order_by(Product.last_seen_at.desc()).limit(limit)
    if category:
        query = query.where(Product.category == category)
    products = (await session.execute(query)).scalars().all()
    now = datetime.now(UTC)
    covered = [
        finished
        for finished in (
            await session.execute(
                select(CollectionRun.finished_at).where(
                    CollectionRun.kind.in_(
                        (
                            CollectionKind.PRODUCT_DISCOVERY.value,
                            CollectionKind.SOCIAL_WEEKLY.value,
                        )
                    ),
                    CollectionRun.status == "completed",
                    CollectionRun.finished_at >= now - timedelta(days=WINDOW_DAYS * WINDOWS),
                )
            )
        ).scalars()
        if finished is not None
    ]
    by_product: dict[str, list[ProductObservation]] = defaultdict(list)
    for observation in (
        await session.execute(
            select(ProductObservation)
            .where(ProductObservation.product_id.in_([p.id for p in products]))
            .order_by(ProductObservation.observed_at.desc())
        )
    ).scalars():
        by_product[observation.product_id].append(observation)
    out = []
    for product in products:
        observations = by_product[product.id]
        out.append(
            ProductOut(
                id=product.id,
                brand=product.brand,
                name=product.name,
                category=product.category,
                status=product.status,
                launch_status=product.launch_status,
                official_url=product.official_url,
                fda_reference=product.fda_reference,
                first_seen_at=product.first_seen_at,
                last_seen_at=product.last_seen_at,
                independent_sources=len(
                    {source_identity(o.publisher, o.source_url) for o in observations}
                ),
                mentions=len(observations),
                weeks=weekly_mentions([o.observed_at for o in observations], covered, now=now),
                recent=[
                    {
                        "url": o.source_url,
                        "source_kind": o.source_kind,
                        "publisher": o.publisher,
                        "published_at": o.published_at,
                        "mention_kind": o.mention_kind,
                        "quote": o.quote,
                    }
                    for o in observations[:5]
                ],
            )
        )
    return out


@router.get("/intel/related", response_model=list[dict[str, Any]])
async def read_related(
    session: SessionDep,
    kind: Annotated[Literal["social_topic", "product"], Query()],
    id: Annotated[str, Query(pattern=UUID_PATTERN)],
) -> list[dict[str, Any]]:
    """Items from the other discovery systems related to this one, each with
    its own system's numbers. Read-only: no score anywhere is changed."""
    if kind == "product":
        product = await session.get(Product, id)
        if product is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "product not found")
        return await related_to_product(session, product)
    topic = await session.get(SocialTopic, id)
    if topic is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "social topic not found")
    return await related_to_topic(
        session, subject=topic.subject, outcome=topic.outcome, social_topic_id=topic.id
    )
