"""The ``sources`` cache and the pgvector re-rank, held in memory for one case.

Why not the database: the eval must never write where the product reads. A
pipeline run against the real database leaves ``sources`` rows behind, and its
PERSIST stage writes an article straight into the review queue. A dedicated
eval database would avoid the second problem but still need Postgres to run,
which a CI job should not.

**What this re-implements, and only this:** the SQL in
``retrieval/rerank.py::SemanticReranker.rank_for_claim`` — best-chunk cosine
per paper over *this run's* candidates, the abstract-length and year filters,
the grade floor. The scoring itself (``score``: cosine + grade bonus + recency
bonus), the chunking (``chunk_text``), the top-k cut and the handle assignment
are imported from production unchanged. The SQL's own properties (no ``LIMIT``
reaching the planner, exact scan) are pinned separately by
``tests/db/test_rerank_plan.py`` against a live database.

One deliberate mirror of the real cache: a row keeps the fields it was first
inserted with. ``SourceCache`` refreshes only identifiers and citation counts
on a match, so the abstract and study type are first-seen — and so are these.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import OperationalError

from app.domain.contracts import CachedCandidate, CandidatePaper, RankedSource
from app.domain.enums import EVIDENCE_RANK, StudyType
from app.llm.embeddings.base import EmbeddingProvider
from app.retrieval.cache import CachedSource
from app.retrieval.chunking import chunk_text
from app.retrieval.rerank import RerankConfig, ranking_text, score, select_top
from evaluation.schema import FaultSpec


@dataclass
class SourceRow:
    """The columns of ``sources`` the pipeline reads back."""

    id: str
    pmid: str | None
    doi: str | None
    paper: CandidatePaper

    @property
    def study_type(self) -> str:
        return str(self.paper.study_type)


@dataclass
class MemoryStore:
    rows: dict[str, SourceRow] = field(default_factory=dict)
    by_identifier: dict[str, str] = field(default_factory=dict)
    chunks: dict[str, list[list[float]]] = field(default_factory=dict)

    def find(self, paper: CandidatePaper) -> SourceRow | None:
        """By DOI first, then PMID — ``cache._match``'s precedence."""
        for key in _identity_keys(paper):
            if (row_id := self.by_identifier.get(key)) is not None:
                return self.rows[row_id]
        return None


def _identity_keys(paper: CandidatePaper) -> list[str]:
    keys = []
    if paper.doi:
        keys.append(f"doi:{paper.doi.strip().lower()}")
    if paper.pmid:
        keys.append(f"pmid:{paper.pmid.strip()}")
    return keys or [paper.dedup_key]


class MemorySourceCache:
    """``SourceCache``'s two public methods, over ``MemoryStore``."""

    def __init__(self, store: MemoryStore, embedder: EmbeddingProvider) -> None:
        self._store = store
        self._embedder = embedder

    async def upsert_many(self, papers: list[CandidatePaper]) -> list[CachedSource]:
        unique: dict[str, CandidatePaper] = {}
        for paper in papers:
            unique.setdefault(paper.dedup_key, paper)

        results: list[CachedSource] = []
        for paper in unique.values():
            row = self._store.find(paper)
            if row is None:
                source_id = (
                    "src-" + hashlib.sha1(_identity_keys(paper)[0].encode()).hexdigest()[:16]
                )
                row = SourceRow(id=source_id, pmid=paper.pmid, doi=paper.doi, paper=paper)
                self._store.rows[source_id] = row
            else:
                # Adopt identifiers the row lacked, never overwrite one.
                row.pmid = row.pmid or paper.pmid
                row.doi = row.doi or paper.doi
            for key in _identity_keys(paper):
                self._store.by_identifier.setdefault(key, row.id)
            results.append(
                CachedSource(
                    source_id=row.id, paper=paper, had_embedding=row.id in self._store.chunks
                )
            )
        return results

    async def ensure_embeddings(self, cached: list[CachedSource]) -> None:
        pending = {
            entry.source_id: self._store.rows[entry.source_id].paper.abstract
            for entry in cached
            if not entry.had_embedding
        }
        if not pending:
            return
        pieces = [
            (source_id, text)
            for source_id, abstract in pending.items()
            for text in chunk_text(abstract)
        ]
        vectors = await self._embedder.embed_documents([text for _, text in pieces])
        for source_id in pending:
            self._store.chunks[source_id] = []
        for (source_id, _), vector in zip(pieces, vectors, strict=True):
            self._store.chunks[source_id].append(vector)


class MemoryReranker:
    """``SemanticReranker.rank_for_claim`` without the SQL."""

    def __init__(
        self, store: MemoryStore, embedder: EmbeddingProvider, fault: FaultSpec | None = None
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._fault = fault

    async def rank_for_claim(
        self,
        claim: str,
        candidates: list[CachedCandidate],
        config: RerankConfig | None = None,
        *,
        subject: str | None = None,
    ) -> list[RankedSource]:
        config = config or RerankConfig()
        if not candidates:
            return []
        if self._fault is not None:
            # What asyncpg surfaces through SQLAlchemy when the database is gone.
            raise OperationalError(
                "SELECT sources.id ... FROM sources JOIN source_chunks",
                {},
                ConnectionRefusedError("connection refused (eval fault: vector store)"),
            )

        claim_vector = await self._embedder.embed_query(ranking_text(claim, subject))
        papers = {entry.source_id: entry.paper for entry in candidates}
        floor = EVIDENCE_RANK[config.min_grade]
        ranked: list[RankedSource] = []
        for source_id in dict.fromkeys(entry.source_id for entry in candidates):
            row = self._store.rows.get(source_id)
            chunks = self._store.chunks.get(source_id)
            # The SQL is an inner JOIN on source_chunks: a paper with no chunks
            # is left out, not scored as zero.
            if row is None or not chunks:
                continue
            if len(row.paper.abstract) < config.min_abstract_chars:
                continue
            if config.min_year and (row.paper.year is None or row.paper.year < config.min_year):
                continue
            study_type = StudyType(row.study_type)
            if EVIDENCE_RANK[study_type] < floor:
                continue
            cosine = max(_cosine(claim_vector, chunk) for chunk in chunks)
            ranked.append(
                RankedSource(
                    source_id=source_id,
                    claim=claim,
                    citation_handle="S0",
                    paper=papers[source_id],
                    cosine_similarity=cosine,
                    final_score=score(cosine, study_type, row.paper.year),
                )
            )
        ranked.sort(key=lambda entry: entry.final_score, reverse=True)
        return select_top(ranked, config.top_k)


class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)


class MemorySession:
    """Answers VALIDATE's one query — ``select(Source).where(Source.id.in_(...))``.

    ``evidence/validation.py::check_sources_resolve`` loads the cited rows to
    confirm each still has a PMID or DOI. The ids are read out of the compiled
    ``IN`` clause, the same technique as ``tests/conftest.py::FakeSession``.
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def execute(self, statement: Any) -> _Result:
        wanted: list[str] = []
        for value in statement.compile().params.values():
            if isinstance(value, (list, tuple)):
                wanted = [str(item) for item in value]
                break
        return _Result([self._store.rows[i] for i in wanted if i in self._store.rows])


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norms = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norms if norms else 0.0
