"""``SourceCache`` against a real database, because the bugs live in the indexes.

A paper's identity in ``sources`` spans two *partial unique* indexes —
``lower(doi)`` and ``pmid`` — and every failure this module covers is a
consequence of that split. A fake session cannot model a unique index, so it
cannot fail any of these tests; the previous fake-backed version passed
throughout the window in which ``upsert_many`` would raise an IntegrityError on
its second sighting of a paper whose DOI it had newly learned.

Skipped automatically when no database is reachable, following
``test_db_invariants.py``. **A skipped run verifies none of this.**

    docker compose up -d db
    python -m scripts.migrate
    pytest tests/unitTest/test_source_cache.py
"""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.domain.contracts import CandidatePaper, SourceApi, StudyType
from app.retrieval.cache import SourceCache
from app.retrieval.chunking import CHUNK_SETTINGS, CHUNK_WORDS, chunk_text

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url

#: Distinctive enough that these rows cannot collide with anything seeded.
_PREFIX = "10.9999/cachetest"


@pytest.fixture
async def session() -> Any:
    """A session whose writes are rolled back after each test.

    Per-test engine for the reason given in ``test_db_invariants.py``: asyncpg
    connections bind to the loop that created them, and pytest-asyncio gives
    each test its own.
    """
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no database at {DATABASE_URL}")

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        try:
            yield db
        finally:
            await db.rollback()
    await engine.dispose()


class _FixedEmbedder:
    """``model_id`` drives the cache-hit flag; the vectors are only stored."""

    model_id = "test-embedder-v1"

    @property
    def dimension(self) -> int:
        return get_settings().embedding_dim

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * get_settings().embedding_dim for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        return [0.0] * get_settings().embedding_dim


def _paper(
    *,
    doi: str | None = None,
    pmid: str | None = None,
    citation_count: int | None = 3,
    source_api: SourceApi = SourceApi.PUBMED,
    abstract: str = "An abstract long enough to be meaningful for the cache test.",
) -> CandidatePaper:
    return CandidatePaper(
        pmid=pmid,
        doi=doi,
        title=f"Paper {doi or pmid}",
        abstract=abstract,
        journal="J. Test",
        year=2024,
        study_type=StudyType.RCT,
        raw_study_type="Randomized Controlled Trial",
        citation_count=citation_count,
        url="https://example.org/paper",
        source_api=source_api,
    )


def _cache(session: AsyncSession) -> SourceCache:
    return SourceCache(session, _FixedEmbedder())


async def _row(session: AsyncSession, source_id: str) -> Any:
    result = await session.execute(
        sa.text("SELECT pmid, doi, citation_count FROM sources WHERE id = :id"),
        {"id": source_id},
    )
    return result.one()


async def _count(session: AsyncSession, *, pmid: str) -> int:
    return await session.scalar(  # type: ignore[return-value]
        sa.text("SELECT count(*) FROM sources WHERE pmid = :pmid"), {"pmid": pmid}
    )


# ---------------------------------------------------------------------------
# The regression: a record arriving more complete than the row it matches.
# ---------------------------------------------------------------------------


async def test_a_paper_seen_again_with_a_new_doi_does_not_collide(
    session: AsyncSession,
) -> None:
    """The bug that made this module DB-backed.

    Run 1 sees the paper through a provider that knows only its PMID. Run 2
    sees it carrying a DOI as well — which the old two-pass writer routed to
    the DOI conflict target, where the PMID-keyed row was invisible, so it
    attempted an INSERT and violated ``sources_pmid_key``. Cross-provider
    merging makes newly-complete records routine, so this is the common path,
    not an edge case.
    """
    cache = _cache(session)

    first = await cache.upsert_many([_paper(pmid="90000001")])
    await session.flush()

    second = await cache.upsert_many([_paper(pmid="90000001", doi=f"{_PREFIX}/a")])
    await session.flush()

    assert [entry.source_id for entry in second] == [first[0].source_id]
    assert await _count(session, pmid="90000001") == 1

    pmid, doi, _ = await _row(session, first[0].source_id)
    assert (pmid, doi) == ("90000001", f"{_PREFIX}/a")


async def test_a_paper_seen_again_with_a_new_pmid_does_not_collide(
    session: AsyncSession,
) -> None:
    """The mirror case: DOI known first, PMID learned later."""
    cache = _cache(session)

    first = await cache.upsert_many([_paper(doi=f"{_PREFIX}/b")])
    await session.flush()

    second = await cache.upsert_many([_paper(doi=f"{_PREFIX}/b", pmid="90000002")])
    await session.flush()

    assert [entry.source_id for entry in second] == [first[0].source_id]
    pmid, doi, _ = await _row(session, first[0].source_id)
    assert (pmid, doi) == ("90000002", f"{_PREFIX}/b")


async def test_matching_on_either_identifier_reuses_the_row(
    session: AsyncSession,
) -> None:
    """A later sighting carrying only one of the two ids still matches."""
    cache = _cache(session)

    first = await cache.upsert_many([_paper(pmid="90000003", doi=f"{_PREFIX}/c")])
    await session.flush()

    by_pmid = await cache.upsert_many([_paper(pmid="90000003")])
    by_doi = await cache.upsert_many([_paper(doi=f"{_PREFIX}/c")])
    await session.flush()

    assert by_pmid[0].source_id == first[0].source_id
    assert by_doi[0].source_id == first[0].source_id


# ---------------------------------------------------------------------------
# Batch collapsing. Still required, for a different reason than it used to be.
# ---------------------------------------------------------------------------


async def test_a_paper_repeated_across_claims_becomes_one_row(
    session: AsyncSession,
) -> None:
    """Candidates are gathered per claim, so one paper arrives once per claim.

    This used to be about ON CONFLICT rejecting a statement that proposes two
    rows with the same conflict key. There is no ON CONFLICT any more, and the
    requirement survives it: two INSERTs of one paper violate the unique index
    just as surely, and the caller would get two ``CachedSource`` entries for
    one paper either way.
    """
    cache = _cache(session)
    duplicated = _paper(doi=f"{_PREFIX}/d")

    results = await cache.upsert_many(
        [duplicated, duplicated, _paper(doi=f"{_PREFIX}/e")]
    )
    await session.flush()

    assert len({entry.source_id for entry in results}) == 2


async def test_case_differing_dois_are_one_paper(session: AsyncSession) -> None:
    """The unique index is on ``lower(doi)``, so casing cannot split a key."""
    cache = _cache(session)

    results = await cache.upsert_many(
        [_paper(doi=f"{_PREFIX}/F"), _paper(doi=f"{_PREFIX}/f")]
    )
    await session.flush()

    assert len(results) == 1


async def test_a_repeated_pmid_becomes_one_row(session: AsyncSession) -> None:
    """DOI-less papers key on ``pmid`` and need the same collapsing."""
    cache = _cache(session)

    results = await cache.upsert_many([_paper(pmid="90000004"), _paper(pmid="90000004")])
    await session.flush()

    assert len(results) == 1
    assert await _count(session, pmid="90000004") == 1


# ---------------------------------------------------------------------------
# Refresh semantics.
# ---------------------------------------------------------------------------


async def test_a_refresh_never_discards_an_identifier_we_already_had(
    session: AsyncSession,
) -> None:
    """COALESCE runs both ways: adopt what is new, keep what the record lacks."""
    cache = _cache(session)

    first = await cache.upsert_many([_paper(pmid="90000005", doi=f"{_PREFIX}/g")])
    await session.flush()

    # A provider that knows the PMID but not the DOI must not blank the DOI.
    await cache.upsert_many([_paper(pmid="90000005")])
    await session.flush()

    pmid, doi, _ = await _row(session, first[0].source_id)
    assert (pmid, doi) == ("90000005", f"{_PREFIX}/g")


async def test_a_refresh_keeps_a_citation_count_the_new_record_lacks(
    session: AsyncSession,
) -> None:
    cache = _cache(session)

    first = await cache.upsert_many([_paper(pmid="90000006", citation_count=17)])
    await session.flush()

    await cache.upsert_many([_paper(pmid="90000006", citation_count=None)])
    await session.flush()

    _, _, citation_count = await _row(session, first[0].source_id)
    assert citation_count == 17


async def test_a_row_embedded_by_another_model_does_not_count_as_cached(
    session: AsyncSession,
) -> None:
    """A vector from a different model occupies a different space.

    Reusing it would silently rank one claim's candidates in two incompatible
    embedding spaces, which looks like bad relevance rather than a bug.
    """
    cache = _cache(session)
    entries = await cache.upsert_many([_paper(pmid="90000007")])
    await session.flush()

    await session.execute(
        sa.text("UPDATE sources SET embedding_model = 'some-other-model' WHERE id = :id"),
        {"id": entries[0].source_id},
    )

    again = await cache.upsert_many([_paper(pmid="90000007")])
    assert again[0].had_embedding is False


# ---------------------------------------------------------------------------
# Chunked embeddings.
# ---------------------------------------------------------------------------


async def _chunks(session: AsyncSession, source_id: str) -> list[int]:
    """The stored chunk ordinals. The text itself is not stored — it is re-cut
    from the abstract when needed — so the ordinals are what can be checked."""
    result = await session.execute(
        sa.text("SELECT ordinal FROM source_chunks WHERE source_id = :id ORDER BY ordinal"),
        {"id": source_id},
    )
    return list(result.scalars())


#: Long enough for three chunks at the current size; a typical abstract is one.
_LONG_ABSTRACT = " ".join(f"word{index}" for index in range(2 * CHUNK_WORDS + 100))


async def test_embedding_writes_overlapping_chunks_and_records_how(
    session: AsyncSession,
) -> None:
    cache = _cache(session)
    entries = await cache.upsert_many([_paper(pmid="90000020", abstract=_LONG_ABSTRACT)])

    await cache.ensure_embeddings(entries)

    assert await _chunks(session, entries[0].source_id) == list(
        range(len(chunk_text(_LONG_ABSTRACT)))
    )
    assert len(chunk_text(_LONG_ABSTRACT)) == 3
    recorded = (
        await session.execute(
            sa.text("SELECT embedding_model, chunk_settings FROM sources WHERE id = :id"),
            {"id": entries[0].source_id},
        )
    ).one()
    assert tuple(recorded) == ("test-embedder-v1", CHUNK_SETTINGS)
    again = await cache.upsert_many([_paper(pmid="90000020", abstract=_LONG_ABSTRACT)])
    assert again[0].had_embedding is True


async def test_chunks_cut_with_other_settings_do_not_count_as_cached(
    session: AsyncSession,
) -> None:
    """A new chunk size must re-chunk stored papers, even under the same model."""
    cache = _cache(session)
    entries = await cache.upsert_many([_paper(pmid="90000022", abstract=_LONG_ABSTRACT)])
    await cache.ensure_embeddings(entries)
    await session.execute(
        sa.text("UPDATE sources SET chunk_settings = '120/30 words' WHERE id = :id"),
        {"id": entries[0].source_id},
    )

    again = await cache.upsert_many([_paper(pmid="90000022", abstract=_LONG_ABSTRACT)])

    assert again[0].had_embedding is False


async def test_re_embedding_replaces_old_chunks_rather_than_adding_to_them(
    session: AsyncSession,
) -> None:
    """Chunks from another model must not survive beside the new ones — here
    the new abstract is one chunk long, so stale ordinals 1 and 2 would show."""
    cache = _cache(session)
    entries = await cache.upsert_many([_paper(pmid="90000021", abstract=_LONG_ABSTRACT)])
    await cache.ensure_embeddings(entries)
    await session.execute(
        sa.text("UPDATE sources SET embedding_model = 'some-other-model' WHERE id = :id"),
        {"id": entries[0].source_id},
    )

    short = _paper(pmid="90000021", abstract="A short abstract that fits in one chunk.")
    again = await cache.upsert_many([short])
    await cache.ensure_embeddings(again)

    assert await _chunks(session, entries[0].source_id) == [0]


# ---------------------------------------------------------------------------
# Split rows: reported, not repaired.
# ---------------------------------------------------------------------------


async def test_a_paper_split_across_two_rows_is_reported_and_resolved_to_the_doi_row(
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two rows, one paper — the state the old keying could leave behind.

    Repair is deliberately out of scope here: it means re-pointing
    ``article_sources`` and deleting a row that ``ON DELETE RESTRICT`` protects,
    which would let a cache refresh rewrite the provenance of published
    articles as a side effect of fetching abstracts. So the run resolves to the
    DOI row — stably, since that is what the next run resolves to as well — and
    says so loudly enough to be actioned.
    """
    cache = _cache(session)

    doi_row = await cache.upsert_many([_paper(doi=f"{_PREFIX}/h")])
    pmid_row = await cache.upsert_many([_paper(pmid="90000008")])
    await session.flush()
    assert doi_row[0].source_id != pmid_row[0].source_id

    with caplog.at_level(logging.WARNING, logger="app.retrieval.cache"):
        merged = await cache.upsert_many(
            [_paper(doi=f"{_PREFIX}/h", pmid="90000008")]
        )

    assert merged[0].source_id == doi_row[0].source_id
    assert "same paper held twice" in caplog.text

    # Neither row is deleted, and neither is silently mangled.
    assert await _count(session, pmid="90000008") == 1


# ---------------------------------------------------------------------------
# Batched refresh: one statement, unchanged semantics.
# ---------------------------------------------------------------------------


async def test_a_batch_of_refreshes_is_a_single_statement(
    session: AsyncSession,
) -> None:
    """One UPDATE per matched row is a hundred round trips on a warm cache —
    and warm is the common case, since the cache exists so a repeat topic
    re-matches everything. Counted rather than asserted in prose, because the
    obvious way to write ``_refresh_many`` is the per-row loop it replaced."""
    cache = _cache(session)
    papers = [_paper(pmid=f"9000010{i}") for i in range(8)]

    await cache.upsert_many(papers)
    await session.flush()

    statements: list[str] = []

    def _record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        statements.append(statement)

    engine = (await session.connection()).sync_connection.engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        again = await cache.upsert_many(papers)
        await session.flush()
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(again) == 8
    updates = [s for s in statements if s.lstrip().upper().startswith("UPDATE")]
    assert len(updates) == 1, f"expected one batched UPDATE, got {len(updates)}"
    assert "VALUES" in updates[0]


async def test_a_batched_refresh_updates_each_row_with_its_own_values(
    session: AsyncSession,
) -> None:
    """The failure a batch invites: every row updated with the same paper's
    data. Distinct citation counts per row are what catches it."""
    cache = _cache(session)
    first = _paper(pmid="90000110", citation_count=None)
    second = _paper(pmid="90000111", citation_count=None)

    written = await cache.upsert_many([first, second])
    await session.flush()
    by_pmid = {entry.paper.pmid: entry.source_id for entry in written}

    await cache.upsert_many(
        [
            _paper(pmid="90000110", citation_count=11),
            _paper(pmid="90000111", citation_count=22),
        ]
    )
    await session.flush()

    assert (await _row(session, by_pmid["90000110"]))[2] == 11
    assert (await _row(session, by_pmid["90000111"]))[2] == 22


async def test_a_batch_with_no_citation_counts_at_all_still_refreshes(
    session: AsyncSession,
) -> None:
    """Postgres types a VALUES column from its literals, so a batch where no
    paper carries a count is a column of bare NULLs — inferred as ``text``,
    which will not COALESCE with an integer column. This is that regression."""
    cache = _cache(session)
    papers = [_paper(pmid=f"9000012{i}", citation_count=None) for i in range(3)]

    written = await cache.upsert_many(papers)
    await session.flush()

    again = await cache.upsert_many(papers)
    await session.flush()

    assert [e.source_id for e in again] == [e.source_id for e in written]


async def test_contested_and_adoptable_rows_refresh_in_the_same_batch(
    session: AsyncSession,
) -> None:
    """Split rows need a different SET clause — they must not adopt the
    contested identifier — so they are a second statement. A batch containing
    both kinds has to handle each correctly rather than picking one rule."""
    cache = _cache(session)

    doi_row = await cache.upsert_many([_paper(doi=f"{_PREFIX}/split")])
    pmid_row = await cache.upsert_many([_paper(pmid="90000130")])
    plain = await cache.upsert_many([_paper(pmid="90000131", doi=f"{_PREFIX}/plain")])
    await session.flush()

    results = await cache.upsert_many(
        [
            # Contested: its two ids belong to two different rows.
            _paper(doi=f"{_PREFIX}/split", pmid="90000130", citation_count=5),
            # Adoptable: one row, both ids already its own.
            _paper(pmid="90000131", doi=f"{_PREFIX}/plain", citation_count=9),
        ]
    )
    await session.flush()

    assert {e.source_id for e in results} == {doi_row[0].source_id, plain[0].source_id}
    # The contested row kept its own pmid; the other row still owns 90000130.
    pmid, doi, citation_count = await _row(session, doi_row[0].source_id)
    assert (pmid, doi, citation_count) == (None, f"{_PREFIX}/split", 5)
    assert (await _row(session, pmid_row[0].source_id))[0] == "90000130"
    assert (await _row(session, plain[0].source_id))[2] == 9
