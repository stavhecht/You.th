"""The sources cache: every paper we fetch is stored once and reused forever.

Two steps, always in this order:

1. ``upsert_many`` saves the papers. Each paper is matched to an existing row
   by DOI *or* PMID; matched rows are refreshed, new papers are inserted.
2. ``ensure_embeddings`` chunks and embeds only the papers that have no
   vectors from the current model yet.

Upserting first is what keeps a warm cache cheap: it reports which papers are
already embedded, so only new abstracts are sent to the embedding provider.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Integer, Table, Text, column, delete, func, or_, select, update, values
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.contracts import CandidatePaper
from app.domain.models import Source, SourceChunk
from app.llm.embeddings.base import EmbeddingProvider
from app.retrieval.chunking import CHUNK_SETTINGS, chunk_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CachedSource:
    source_id: str
    paper: CandidatePaper
    #: True when the row's chunks were cut with the current settings and
    #: embedded by the live model, so they can be reused as they are.
    had_embedding: bool


@dataclass(frozen=True, slots=True)
class _ExistingRow:
    id: str
    pmid: str | None
    doi: str | None
    embedding_model: str | None
    chunk_settings: str | None


@dataclass(frozen=True, slots=True)
class _Match:
    row: _ExistingRow
    #: The paper's DOI and PMID point at two *different* rows: the cache holds
    #: this paper twice. We use the DOI row but must not copy the PMID onto it,
    #: because the other row already owns that PMID.
    split: bool


class SourceCache:
    def __init__(self, session: AsyncSession, embedder: EmbeddingProvider) -> None:
        self._session = session
        self._embedder = embedder

    async def upsert_many(self, papers: list[CandidatePaper]) -> list[CachedSource]:
        """Save papers to ``sources``; return one entry per distinct paper.

        Why not a single ``INSERT ... ON CONFLICT``: a paper is identified by
        two unique indexes (DOI and PMID) and Postgres checks only one per
        statement. A paper first cached with a PMID only, and seen again with a
        DOI too, would miss on DOI, get inserted, and violate the PMID index.
        So we read the matching rows first, update them by primary key, and
        insert only the papers that matched nothing.

        Two workers can insert the same new paper between our read and our
        write. That raises ``IntegrityError``; we retry once inside a savepoint,
        and the retry finds the row the other worker wrote. The savepoint undoes
        only this batch, not the rest of the stage's transaction.
        """
        if not papers:
            return []

        # One paper often answers several claims, so it can arrive twice.
        unique: dict[str, CandidatePaper] = {}
        for paper in papers:
            unique.setdefault(paper.dedup_key, paper)
        papers = list(unique.values())

        results: list[CachedSource] = []
        for attempt in range(2):
            try:
                async with self._session.begin_nested():
                    results = await self._write(papers)
                break
            except IntegrityError:
                if attempt:
                    raise
                logger.warning("source cache: concurrent insert; retrying once")

        logger.info(
            "source cache: %d papers -> %d rows (%d already embedded)",
            len(papers),
            len(results),
            sum(1 for entry in results if entry.had_embedding),
        )
        return results

    async def ensure_embeddings(self, cached: list[CachedSource]) -> None:
        """Chunk and embed every source whose chunks are missing or out of date.

        Only the abstract is embedded, not the title: titles carry marketing
        phrasing that pulls the vector toward the claim's wording rather than
        toward what the study found.
        """
        pending = {
            entry.source_id: entry.paper.abstract for entry in cached if not entry.had_embedding
        }
        if pending:
            count = await write_chunks(self._session, self._embedder, pending)
            logger.info(
                "embedded %d new abstracts as %d chunks (%s)",
                len(pending),
                count,
                self._embedder.model_id,
            )

    async def _write(self, papers: list[CandidatePaper]) -> list[CachedSource]:
        """Match each paper to a cached row, then refresh or insert."""
        existing = await self._load_existing(papers)

        results: list[CachedSource] = []
        new_papers: list[CandidatePaper] = []
        adopting: list[tuple[_ExistingRow, CandidatePaper]] = []
        contested: list[tuple[_ExistingRow, CandidatePaper]] = []

        for paper in papers:
            match = _match(paper, existing)
            if match is None:
                new_papers.append(paper)
                continue
            (contested if match.split else adopting).append((match.row, paper))
            results.append(
                CachedSource(
                    source_id=match.row.id,
                    paper=paper,
                    # Vectors from another model are in a different space, and
                    # chunks cut another way no longer match chunk_text.
                    had_embedding=(
                        match.row.embedding_model == self._embedder.model_id
                        and match.row.chunk_settings == CHUNK_SETTINGS
                    ),
                )
            )

        await self._refresh_many(adopting, adopt_identifiers=True)
        await self._refresh_many(contested, adopt_identifiers=False)
        results.extend(await self._insert(new_papers))
        return results

    async def _load_existing(self, papers: list[CandidatePaper]) -> dict[str, _ExistingRow]:
        """Every cached row matching any DOI or PMID in the batch, in one query.

        Keyed ``doi:<doi>`` and ``pmid:<pmid>``, so a row is findable by either.
        """
        dois = [paper.doi.strip().lower() for paper in papers if paper.doi]
        pmids = [paper.pmid.strip() for paper in papers if paper.pmid]

        conditions = []
        if dois:
            conditions.append(func.lower(Source.doi).in_(dois))
        if pmids:
            conditions.append(Source.pmid.in_(pmids))
        if not conditions:
            return {}

        result = await self._session.execute(
            select(
                Source.id,
                Source.pmid,
                Source.doi,
                Source.embedding_model,
                Source.chunk_settings,
            ).where(or_(*conditions))
        )

        index: dict[str, _ExistingRow] = {}
        for row in result.all():
            entry = _ExistingRow(
                id=str(row.id),
                pmid=row.pmid,
                doi=row.doi,
                embedding_model=row.embedding_model,
                chunk_settings=row.chunk_settings,
            )
            if entry.doi:
                index[f"doi:{entry.doi.strip().lower()}"] = entry
            if entry.pmid:
                index[f"pmid:{entry.pmid.strip()}"] = entry
        return index

    async def _refresh_many(
        self,
        matched: list[tuple[_ExistingRow, CandidatePaper]],
        *,
        adopt_identifiers: bool,
    ) -> None:
        """Refresh every matched row in one ``UPDATE ... FROM (VALUES ...)``.

        Updates ``last_seen_at`` and the citation count. With
        ``adopt_identifiers`` it also fills in a DOI or PMID the row was
        missing; it never overwrites one the row already has. The abstract and
        the vectors are never touched, since reusing them is the point of the
        cache.
        """
        if not matched:
            return

        incoming = values(
            column("id", UUID(as_uuid=False)),
            column("pmid", Text),
            column("doi", Text),
            column("citation_count", Integer),
            name="incoming",
        ).data(
            [(row.id, paper.pmid, paper.doi, paper.citation_count) for row, paper in matched]
        )

        assignments: dict[str, Any] = {
            "last_seen_at": datetime.now(UTC),
            # The cast matters: a batch where no paper has a citation count is
            # a column of bare NULLs, which Postgres types as text, and text
            # does not COALESCE with an integer column.
            "citation_count": func.coalesce(
                incoming.c.citation_count.cast(Integer), Source.citation_count
            ),
        }
        if adopt_identifiers:
            assignments["pmid"] = func.coalesce(Source.pmid, incoming.c.pmid)
            assignments["doi"] = func.coalesce(Source.doi, incoming.c.doi)

        await self._session.execute(
            update(Source).where(Source.id == incoming.c.id).values(**assignments)
        )

    async def _insert(self, papers: list[CandidatePaper]) -> list[CachedSource]:
        """Insert papers that matched nothing. A new row has no vectors yet."""
        if not papers:
            return []

        now = datetime.now(UTC)
        result = await self._session.execute(
            cast(Table, Source.__table__)
            .insert()
            .values(
                [
                    {
                        "pmid": paper.pmid,
                        "doi": paper.doi,
                        "title": paper.title,
                        "abstract": paper.abstract,
                        "journal": paper.journal,
                        "year": paper.year,
                        "study_type": paper.study_type,
                        "raw_study_type": paper.raw_study_type,
                        "citation_count": paper.citation_count,
                        "url": paper.url,
                        "source_api": paper.source_api.value,
                        "created_at": now,
                        "last_seen_at": now,
                    }
                    for paper in papers
                ]
            )
            .returning(Source.id)
        )

        # RETURNING keeps the order of the VALUES list for a plain INSERT.
        return [
            CachedSource(source_id=str(row.id), paper=paper, had_embedding=False)
            for row, paper in zip(result.all(), papers, strict=True)
        ]


async def write_chunks(
    session: AsyncSession, embedder: EmbeddingProvider, abstracts: dict[str, str]
) -> int:
    """Chunk, embed and store each source's abstract, keyed by source id.

    Replaces any chunks the source already had (they may come from another
    model, or from a different chunk size), then records the model and
    ``CHUNK_SETTINGS`` on the source. Returns how many chunks were written.
    Shared with ``scripts/reembed_sources.py``, so both write chunks the same way.
    """
    chunks = [
        (source_id, ordinal, text)
        for source_id, abstract in abstracts.items()
        for ordinal, text in enumerate(chunk_text(abstract))
    ]
    vectors = await embedder.embed_documents([text for _, _, text in chunks])
    source_ids = list(abstracts)

    await session.execute(delete(SourceChunk).where(SourceChunk.source_id.in_(source_ids)))
    if chunks:
        statement = pg_insert(SourceChunk).values(
            [
                {"source_id": source_id, "ordinal": ordinal, "embedding": vector}
                for (source_id, ordinal, _), vector in zip(chunks, vectors, strict=True)
            ]
        )
        # Another worker may be embedding the same new paper at the same time.
        statement = statement.on_conflict_do_update(
            index_elements=[SourceChunk.source_id, SourceChunk.ordinal],
            set_={"embedding": statement.excluded.embedding},
        )
        await session.execute(statement)
    await session.execute(
        update(Source)
        .where(Source.id.in_(source_ids))
        .values(embedding_model=embedder.model_id, chunk_settings=CHUNK_SETTINGS)
    )
    return len(chunks)


def _match(paper: CandidatePaper, existing: dict[str, _ExistingRow]) -> _Match | None:
    """The cached row this paper already has, looked up by DOI and by PMID.

    When the two lookups find two different rows, the cache holds this paper
    twice. That is logged, not repaired here: merging rows would mean
    rewriting ``article_sources`` for published articles, which is a
    maintenance job, not something a cache refresh should do on the side. The
    DOI row wins, so the choice is stable from run to run.
    """
    by_doi = existing.get(f"doi:{paper.doi.strip().lower()}") if paper.doi else None
    by_pmid = existing.get(f"pmid:{paper.pmid.strip()}") if paper.pmid else None

    if by_doi is not None and by_pmid is not None and by_doi.id != by_pmid.id:
        logger.warning(
            "sources %s (doi=%s) and %s (pmid=%s) are the same paper held twice; "
            "using the DOI row. The duplicate needs merging out of band.",
            by_doi.id,
            by_doi.doi,
            by_pmid.id,
            by_pmid.pmid,
        )
        return _Match(row=by_doi, split=True)

    row = by_doi or by_pmid
    return None if row is None else _Match(row=row, split=False)
