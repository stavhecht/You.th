"""The weekly retraction sweep flags the articles citing a retracted paper.

Needs ``youth_test``: the sweep commits, and the dev database's sources are
real. The bibliographic databases are a fake that knows one paper.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.retrieval.retraction_sweep import sweep
from app.retrieval.retractions import PaperIdentity, RetractionChecker, RetractionVerdict
from tests.db.test_feed_search import _draft

URL = os.environ.get("SOCIAL_TEST_DATABASE_URL") or make_url(get_settings().database_url).set(
    database="youth_test"
)


class _OnePaperRetracted:
    """Answers for one PMID and omits every other paper, as a source must."""

    name = "fake"

    def __init__(self, pmid: str) -> None:
        self._pmid = pmid

    async def check(self, papers: Sequence[PaperIdentity]) -> dict[str, RetractionVerdict]:
        return {
            paper.source_id: RetractionVerdict(
                checked=True, retracted=True, detail="fake: retracted publication"
            )
            for paper in papers
            if paper.pmid == self._pmid
        }


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(URL, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT correction_draft FROM articles LIMIT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no migrated test database at {URL}")
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


async def test_a_retraction_reaches_the_article_that_cites_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    pmid = f"9{uuid.uuid4().int % 10**7:07d}"
    async with factory() as db:
        article_id = await _draft(db, headline="Retraction sweep test")
        source_id = str(
            await db.scalar(
                sa.text(
                    "INSERT INTO sources (pmid, title, abstract, source_api, created_at,"
                    " last_seen_at) VALUES (:pmid, 'A paper', 'An abstract.', 'pubmed',"
                    " now(), now()) RETURNING id"
                ),
                {"pmid": pmid},
            )
        )
        await db.execute(
            sa.text(
                "INSERT INTO article_sources (article_id, source_id, claim,"
                " citation_handle, was_cited) VALUES (:a, :s, 'a claim', 'S1', true)"
            ),
            {"a": article_id, "s": source_id},
        )
        await db.commit()

    try:
        checker = RetractionChecker([_OnePaperRetracted(pmid)])
        dry = await sweep(factory, checker)
        assert dry.flagged_articles == {article_id: [source_id]}
        async with factory() as db:
            flagged = await db.scalar(
                sa.text("SELECT retraction_flagged_at FROM articles WHERE id = :id"),
                {"id": article_id},
            )
        assert flagged is None  # a dry run writes nothing

        await sweep(factory, checker, apply=True)
        async with factory() as db:
            row = (
                await db.execute(
                    sa.text(
                        "SELECT a.retraction_flagged_at, s.retracted_at, s.retraction_note"
                        " FROM articles a, sources s WHERE a.id = :a AND s.id = :s"
                    ),
                    {"a": article_id, "s": source_id},
                )
            ).one()
        assert row.retraction_flagged_at is not None
        assert row.retracted_at is not None
        assert row.retraction_note == "fake: retracted publication"
    finally:
        async with factory() as db:
            await db.execute(sa.text("DELETE FROM articles WHERE id = :id"), {"id": article_id})
            await db.execute(sa.text("DELETE FROM sources WHERE id = :id"), {"id": source_id})
            await db.commit()
