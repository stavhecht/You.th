"""Re-check cited sources against the retraction record, and flag what breaks.

The sweep itself, in ``app/`` so the worker can run it weekly as a collection
run (``CollectionKind.RETRACTION_SWEEP``): ``scripts/`` is outside the deployed
image, and until 2026-11-11 the only schedule was a launchd job on one laptop.
``scripts/check_retractions.py`` is the same sweep with a dry run and a printout.

The ingest screen in ``RetrieveStage`` only catches papers that were already
retracted when we first saw them. Retractions land *after* publication, usually
by months or years, so a source cached clean in August and retracted in November
stays cited by a live article and nothing ever looks at it again.

**Nothing is deleted and nothing is unpublished.** A retracted source has its
row marked, and every article citing it gets ``retraction_flagged_at`` set so it
raises a banner and appears on the desk. A human decides what happens next:
correct the article or withdraw it (``ReviewService``).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.enums import ArticleStatus
from app.domain.models import Article, ArticleSource, Source
from app.retrieval.factory import PUBMED_ANONYMOUS_RPS, PUBMED_KEYED_RPS, throttled_client
from app.retrieval.retractions import (
    CrossrefRetractionSource,
    PaperIdentity,
    PubMedRetractionSource,
    RetractionChecker,
    RetractionVerdict,
)

#: Crossref asks for one request per second from the polite pool and is more
#: forgiving than that in practice. Kept at 3 because the sweep has no deadline
#: and being blocked by a bibliographic database is a bad way to find out where
#: the real ceiling is.
CROSSREF_RPS = 3.0

#: Articles a reader can currently reach, or a reviewer is about to approve.
#: `rejected` and `validation_failed` are excluded: nothing points at them, and
#: flagging a rejected draft creates review work with no reader at the end of it.
LIVE_STATUSES = (ArticleStatus.PUBLISHED, ArticleStatus.PENDING_REVIEW)


def build_checker(settings: Settings, http: httpx.AsyncClient) -> RetractionChecker:
    """Both sources, each behind its own throttle.

    Reuses ``retrieval/factory.py``'s pacing rather than opening raw clients,
    because the PubMed ceiling is per IP and shared with the pipeline — a sweep
    running alongside a worker must not be the thing that gets the address
    blocked.
    """
    api_key = settings.pubmed_api_key or None
    return RetractionChecker(
        [
            PubMedRetractionSource(
                throttled_client(
                    settings,
                    http,
                    "pubmed",
                    PUBMED_KEYED_RPS if api_key else PUBMED_ANONYMOUS_RPS,
                ),
                api_key,
            ),
            CrossrefRetractionSource(
                throttled_client(settings, http, "crossref", CROSSREF_RPS),
                # Crossref's polite pool keys on a contact address, and
                # OPENALEX_MAILTO is the one this deployment already has. Both
                # want the same thing — someone to email before blocking you —
                # so a second setting for the same address would be config for
                # its own sake. Rename it if a third API ever wants one.
                settings.openalex_mailto or None,
            ),
        ]
    )


async def load_papers(
    session: AsyncSession, *, scope: str
) -> list[tuple[Source, list[str]]]:
    """Sources to check, each with the article ids citing it.

    A source with no citing article still gets checked under ``--scope all``;
    it simply has nothing to flag if it turns out to be retracted.
    """
    cited_stmt = (
        select(ArticleSource.source_id, ArticleSource.article_id)
        .join(Article, Article.id == ArticleSource.article_id)
        .where(Article.status.in_(LIVE_STATUSES), ArticleSource.was_cited.is_(True))
    )
    citations: dict[str, list[str]] = {}
    for source_id, article_id in (await session.execute(cited_stmt)).all():
        citations.setdefault(source_id, []).append(article_id)

    stmt = select(Source)
    if scope == "cited":
        if not citations:
            return []
        stmt = stmt.where(Source.id.in_(citations.keys()))
    # Already-known retractions are not re-checked: a retraction is not
    # un-issued, and re-asking wastes the request budget the unchecked rows
    # need. Matches the partial index the migration creates.
    stmt = stmt.where(Source.retracted_at.is_(None)).order_by(
        Source.retraction_checked_at.asc().nullsfirst()
    )

    rows = (await session.execute(stmt)).scalars().all()
    return [(row, citations.get(row.id, [])) for row in rows]


def summarise(verdicts: dict[str, RetractionVerdict]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for verdict in verdicts.values():
        if not verdict.checked:
            counts["unchecked"] += 1
        elif verdict.retracted:
            counts["retracted"] += 1
        elif verdict.concern:
            counts["concern"] += 1
        else:
            counts["clean"] += 1
    return counts


@dataclass(frozen=True)
class SweepResult:
    """What one sweep found. ``written`` is False for a dry run."""

    papers: list[tuple[Source, list[str]]]
    verdicts: dict[str, RetractionVerdict]
    #: article id -> the retracted source ids it cites.
    flagged_articles: dict[str, list[str]]
    counts: Counter[str]
    written: bool


async def sweep(
    factory: async_sessionmaker[AsyncSession],
    checker: RetractionChecker,
    *,
    scope: Literal["cited", "all"] = "cited",
    limit: int = 0,
    apply: bool = False,
) -> SweepResult:
    """Check every source in scope and, with ``apply``, record what was found."""
    async with factory() as session:
        papers = await load_papers(session, scope=scope)
    if limit:
        papers = papers[:limit]
    if not papers:
        return SweepResult([], {}, {}, Counter(), written=False)

    verdicts = await checker.check(
        [PaperIdentity(source_id=row.id, pmid=row.pmid, doi=row.doi) for row, _ in papers]
    )
    flagged_articles: dict[str, list[str]] = {}
    for row, article_ids in papers:
        if verdicts.get(row.id, RetractionVerdict()).retracted:
            for article_id in article_ids:
                flagged_articles.setdefault(article_id, []).append(row.id)

    result = SweepResult(papers, verdicts, flagged_articles, summarise(verdicts), written=apply)
    if not apply:
        return result

    now = datetime.now(UTC)
    async with factory() as session:
        for row, _ in papers:
            verdict = verdicts.get(row.id, RetractionVerdict())
            if not verdict.checked:
                # No timestamp written. Recording a check that did not happen
                # would suppress the next sweep too, so the error compounds
                # instead of being retried.
                continue
            await session.execute(
                update(Source)
                .where(Source.id == row.id)
                .values(
                    retraction_checked_at=now,
                    retracted_at=now if verdict.retracted else None,
                    concern_at=now if verdict.concern else None,
                    retraction_note=verdict.detail or None,
                )
            )
        for article_id, source_ids in flagged_articles.items():
            await session.execute(
                update(Article)
                .where(Article.id == article_id)
                .values(
                    retraction_flagged_at=now,
                    retraction_detail={
                        "source_ids": source_ids,
                        "detected_at": now.isoformat(),
                    },
                )
            )
        await session.commit()
    return result
