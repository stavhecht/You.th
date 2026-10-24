"""The re-rank is exact, and the query plan is what proves it.

This module exists because the previous claim in ``rerank.py`` — that the query
needed a raised ``hnsw.ef_search`` — was wrong, and nothing could have caught
it. The setting applied cleanly, the query returned sensible results, and the
plan it was tuning was never chosen. Only ``EXPLAIN`` says so.

What is actually guaranteed: the final score is best-chunk cosine + grade +
recency, so top-k cannot be pushed into SQL, so there is no ``LIMIT``, so
Postgres scores every chunk of every candidate exactly. That is the property
worth pinning — an approximate plan appearing here would silently drop
candidates before the grade bonus ever ran, which reads downstream as thinner
evidence rather than as a bug.

Skipped when no database is reachable. **A skipped run verifies none of it.**
"""

from __future__ import annotations

import math
import os
import random
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.domain.contracts import CachedCandidate, CandidatePaper
from app.domain.enums import SourceApi, StudyType
from app.retrieval.rerank import RerankConfig, SemanticReranker

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url

_PMID_PREFIX = "96000"
_DIM = get_settings().embedding_dim
_SEEDED = 40


def _vector(seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0, 1) for _ in range(_DIM)]


def _vector_at_cosine(target: float, seed: int) -> list[float]:
    """A vector whose cosine to the query vector is ``target``.

    Needed because random high-dimensional vectors are all but orthogonal —
    two independent 1024-dimension draws sit at cosine near zero, a gap of ~1.0.
    The grade bonuses span 0.25, so nothing could reorder across that, and a
    test built on random vectors would be asserting the bonus is powerless.

    Real abstracts on one topic cluster tightly (roughly 0.6–0.85), which is the
    regime the bonuses were sized for. Constructing the gap deliberately tests
    the design at the similarity it was built for instead of at an extreme it
    never meets.
    """
    query = _vector(999)
    norm = math.sqrt(sum(x * x for x in query))
    unit = [x / norm for x in query]

    noise = _vector(seed)
    projection = sum(n * u for n, u in zip(noise, unit, strict=True))
    perpendicular = [n - projection * u for n, u in zip(noise, unit, strict=True)]
    perpendicular_norm = math.sqrt(sum(x * x for x in perpendicular))
    perpendicular = [x / perpendicular_norm for x in perpendicular]

    scale = math.sqrt(1 - target * target)
    return [target * u + scale * p for u, p in zip(unit, perpendicular, strict=True)]


class _FixedEmbedder:
    model_id = "plan-test-v1"

    @property
    def dimension(self) -> int:
        return _DIM

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [_vector(i) for i, _ in enumerate(texts)]

    async def embed_query(self, text: str) -> list[float]:
        return _vector(999)


@pytest.fixture
async def seeded() -> Any:
    """A session holding some embedded sources, rolled back afterwards."""
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no database at {DATABASE_URL}")

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        try:
            ids: list[str] = []
            for index in range(_SEEDED):
                ids.append(
                    await _insert(
                        session,
                        pmid=f"{_PMID_PREFIX}{index:03d}",
                        vector_seed=index,
                        abstract="A randomised trial abstract, " * 20,
                    )
                )
            await session.flush()
            yield session, ids
        finally:
            await session.rollback()
    await engine.dispose()


async def _insert(
    session: AsyncSession,
    *,
    pmid: str,
    vector_seed: int,
    cosine: float | None = None,
    chunk_cosines: list[float] | None = None,
    study_type: str = "rct",
    year: int = 2022,
    abstract: str | None = None,
    embedded: bool = True,
) -> str:
    """One `sources` row plus its chunks, with control over what the filters read.

    ``cosine`` pins the similarity of the row's single chunk to the query
    vector; without it the vector is an independent draw, which in 1024
    dimensions means near-zero. ``chunk_cosines`` gives the row one chunk per
    value instead. ``embedded=False`` writes no chunks at all.
    """
    source_id = str(
        await session.scalar(
            sa.text(
                "INSERT INTO sources (pmid, title, abstract, study_type, source_api,"
                " embedding_model, year, created_at, last_seen_at)"
                " VALUES (:pmid, :title, :abstract, CAST(:study_type AS study_type),"
                " 'pubmed', :model, :year, now(), now())"
                " RETURNING id"
            ),
            {
                "pmid": pmid,
                "title": f"Trial {pmid}",
                "abstract": abstract if abstract is not None else "A trial abstract, " * 20,
                "study_type": study_type,
                "year": year,
                "model": "plan-test-v1" if embedded else None,
            },
        )
    )
    if not embedded:
        return source_id

    if chunk_cosines is not None:
        vectors = [
            _vector_at_cosine(target, vector_seed + ordinal)
            for ordinal, target in enumerate(chunk_cosines)
        ]
    elif cosine is not None:
        vectors = [_vector_at_cosine(cosine, vector_seed)]
    else:
        vectors = [_vector(vector_seed)]
    for ordinal, vector in enumerate(vectors):
        await session.execute(
            sa.text(
                "INSERT INTO source_chunks (source_id, ordinal, embedding)"
                " VALUES (:source_id, :ordinal, CAST(:embedding AS vector))"
            ),
            {"source_id": source_id, "ordinal": ordinal, "embedding": str(vector)},
        )
    return source_id


def _candidate(source_id: str, index: int) -> CachedCandidate:
    return CachedCandidate(
        source_id=source_id,
        paper=CandidatePaper(
            pmid=f"{_PMID_PREFIX}{index:03d}",
            title=f"Trial {index}",
            abstract="A randomised trial abstract, " * 20,
            study_type=StudyType.RCT,
            year=2022,
            source_api=SourceApi.PUBMED,
        ),
    )


async def test_the_rerank_plan_is_exact_with_no_pushed_down_limit(
    seeded: tuple[AsyncSession, list[str]],
) -> None:
    """The regression this module is named for.

    ``hnsw.ef_search`` was raised before this query on the belief it drove an
    approximate index scan. It never did.

    What the assertions prove and what they do not: no ``Limit`` is the real
    guarantee — every chunk of every candidate is scored and truncation happens
    in Python after the grade bonus. The ``hnsw`` check is a canary rather than
    a proof: no HNSW index exists (the schema declines to create one), and at
    this row count the planner would not choose one if it did. It is here so
    that creating one on ``source_chunks`` without revisiting
    ``rank_for_claim`` shows up as a failing test rather than as a quietly
    approximate ranking.
    """
    session, ids = seeded
    query_vector = str(_vector(999))

    # Mirrors the statement SemanticReranker.rank_for_claim builds.
    statement = sa.text(
        "EXPLAIN SELECT s.id, max(1 - (c.embedding <=> CAST(:q AS vector)))"
        " FROM sources s JOIN source_chunks c ON c.source_id = s.id"
        " WHERE s.id = ANY(:ids)"
        "   AND length(s.abstract) >= 200"
        " GROUP BY s.id"
    ).bindparams(sa.bindparam("ids", type_=ARRAY(PgUUID(as_uuid=False))))

    plan = "\n".join(
        row[0]
        for row in (
            await session.execute(statement, {"q": query_vector, "ids": ids})
        ).all()
    )

    assert "Limit" not in plan, f"top-k must not reach SQL:\n{plan}"
    assert "hnsw" not in plan.lower(), f"an approximate plan appeared:\n{plan}"


async def test_top_k_is_applied_after_the_grade_bonus_not_by_sql(
    seeded: tuple[AsyncSession, list[str]],
) -> None:
    """Why the plan has to stay exact.

    A SQL ``LIMIT`` would cut on cosine alone, discarding precisely the
    systematic reviews the grade bonus exists to promote. Here the whole
    candidate set is scored and only then truncated, so a review sitting
    outside the cosine top-k can still finish inside the final top-k.
    """
    session, ids = seeded
    reranker = SemanticReranker(session, _FixedEmbedder())

    candidates = [_candidate(source_id, i) for i, source_id in enumerate(ids)]
    ranked = await reranker.rank_for_claim(
        "reduces stress", candidates, RerankConfig(top_k=5)
    )

    assert len(ranked) == 5
    # Descending by final score, which is what assign_handles then numbers.
    assert [entry.final_score for entry in ranked] == sorted(
        (entry.final_score for entry in ranked), reverse=True
    )
    # Every kept source came from the candidate set, not from the wider library.
    assert {entry.source_id for entry in ranked} <= set(ids)


async def test_ranking_never_reaches_outside_this_runs_candidates(
    seeded: tuple[AsyncSession, list[str]],
) -> None:
    """The load-bearing restriction (DESIGN.md §4).

    Without it the vector store becomes the primary index — the architecture
    this design exists to rule out — and papers on unrelated topics that happen
    to sit nearby in embedding space start appearing as evidence.
    """
    session, ids = seeded
    reranker = SemanticReranker(session, _FixedEmbedder())

    subset = ids[:5]
    ranked = await reranker.rank_for_claim(
        "reduces stress",
        [_candidate(source_id, i) for i, source_id in enumerate(subset)],
        RerankConfig(top_k=50),
    )

    assert {entry.source_id for entry in ranked} == set(subset)


# ---------------------------------------------------------------------------
# The filters the exact plan makes possible.
#
# Each of these drops a row *before* scoring. A SQL LIMIT on cosine would have
# had to run first, so any of them could have emptied the kept set after the
# fact — which is the second reason top-k stays in Python.
# ---------------------------------------------------------------------------


async def _rank(
    session: AsyncSession, ids: list[str], config: RerankConfig
) -> list[str]:
    reranker = SemanticReranker(session, _FixedEmbedder())
    ranked = await reranker.rank_for_claim(
        "reduces stress",
        [_candidate(source_id, i) for i, source_id in enumerate(ids)],
        config,
    )
    return [entry.source_id for entry in ranked]


async def test_a_source_with_no_chunks_is_skipped_not_scored_as_zero(
    seeded: tuple[AsyncSession, list[str]],
) -> None:
    """A paper with no chunks has no distance. Treating it as 0.0 would rank an
    un-embedded paper as maximally dissimilar rather than as absent, quietly
    filling a prompt slot decision with a row nobody could compare."""
    session, _ = seeded
    embedded = await _insert(session, pmid="96001001", vector_seed=1)
    bare = await _insert(session, pmid="96001002", vector_seed=2, embedded=False)
    await session.flush()

    kept = await _rank(session, [embedded, bare], RerankConfig(top_k=10))

    assert kept == [embedded]


async def test_the_grade_floor_drops_weaker_designs_before_scoring(
    seeded: tuple[AsyncSession, list[str]],
) -> None:
    """Default config sets no floor on purpose — filtering hard can empty the
    pool for a fringe topic, and 'no evidence' should be reachable honestly
    rather than engineered around. When a floor *is* set it must bite."""
    session, _ = seeded
    strong = await _insert(session, pmid="96002001", vector_seed=1, study_type="rct")
    weak = await _insert(session, pmid="96002002", vector_seed=2, study_type="in_vitro")
    await session.flush()

    both = await _rank(session, [strong, weak], RerankConfig(top_k=10))
    floored = await _rank(
        session, [strong, weak], RerankConfig(top_k=10, min_grade=StudyType.RCT)
    )

    assert set(both) == {strong, weak}
    assert floored == [strong]


async def test_the_grade_floor_is_inclusive_of_its_own_tier(
    seeded: tuple[AsyncSession, list[str]],
) -> None:
    """An off-by-one here silently discards the exact tier that was asked for."""
    session, _ = seeded
    at_floor = await _insert(session, pmid="96003001", vector_seed=1, study_type="rct")
    await session.flush()

    kept = await _rank(session, [at_floor], RerankConfig(min_grade=StudyType.RCT))

    assert kept == [at_floor]


async def test_a_short_abstract_is_dropped(
    seeded: tuple[AsyncSession, list[str]],
) -> None:
    """Below the floor there is not enough text to embed meaningfully, and a
    near-empty abstract gives the model nothing to ground a sentence on."""
    session, _ = seeded
    full = await _insert(session, pmid="96004001", vector_seed=1)
    stub = await _insert(session, pmid="96004002", vector_seed=2, abstract="Too short.")
    await session.flush()

    kept = await _rank(session, [full, stub], RerankConfig(top_k=10))

    assert kept == [full]


async def test_the_recency_window_excludes_older_work_when_set(
    seeded: tuple[AsyncSession, list[str]],
) -> None:
    session, _ = seeded
    recent = await _insert(session, pmid="96005001", vector_seed=1, year=2024)
    old = await _insert(session, pmid="96005002", vector_seed=2, year=1998)
    await session.flush()

    unfiltered = await _rank(session, [recent, old], RerankConfig(top_k=10))
    windowed = await _rank(session, [recent, old], RerankConfig(top_k=10, min_year=2010))

    assert set(unfiltered) == {recent, old}
    assert windowed == [recent]


async def test_a_stronger_design_outranks_a_closer_match(
    seeded: tuple[AsyncSession, list[str]],
) -> None:
    """End to end against real vectors: the property the exact sort protects.

    The in-vitro abstract matches the claim's wording better (cosine 0.85) than
    the meta-analysis does (0.72) — the realistic case, since a petri-dish
    result phrased in the marketing's own vocabulary out-similarities a review
    written clinically. A SQL ``LIMIT 1`` on cosine returns the petri dish.
    Scoring the whole set and truncating afterwards returns the meta-analysis.
    """
    session, _ = seeded
    closer_in_vitro = await _insert(
        session, pmid="96006001", vector_seed=11, cosine=0.85, study_type="in_vitro"
    )
    stronger_meta = await _insert(
        session, pmid="96006002", vector_seed=12, cosine=0.72, study_type="meta_analysis"
    )
    await session.flush()

    reranker = SemanticReranker(session, _FixedEmbedder())
    ranked = await reranker.rank_for_claim(
        "reduces stress",
        [_candidate(closer_in_vitro, 0), _candidate(stronger_meta, 1)],
        RerankConfig(top_k=2),
    )

    by_id = {entry.source_id: entry for entry in ranked}
    # The in-vitro row really is the closer match…
    assert (
        by_id[closer_in_vitro].cosine_similarity
        > by_id[stronger_meta].cosine_similarity
    )
    # …and still loses on final score.
    assert ranked[0].source_id == stronger_meta


async def test_the_bonuses_cannot_rescue_an_arbitrarily_distant_source(
    seeded: tuple[AsyncSession, list[str]],
) -> None:
    """The documented reach of the grade bonus, asserted rather than assumed.

    Meta-analysis (+0.15) against in-vitro (-0.10) is a 0.25 swing, so grading
    reorders across a similarity gap up to 0.25 and no further. That is a
    deliberate ceiling — past it, the "review" is about a different subject and
    promoting it would ground the article in something off-topic. Worth pinning
    because it is also why a test built on random high-dimensional vectors
    (cosine ≈ 0, gap ≈ 1.0) would wrongly conclude the bonus does nothing.
    """
    session, _ = seeded
    on_topic_in_vitro = await _insert(
        session, pmid="96007001", vector_seed=13, cosine=0.90, study_type="in_vitro"
    )
    off_topic_meta = await _insert(
        session, pmid="96007002", vector_seed=14, cosine=0.30, study_type="meta_analysis"
    )
    await session.flush()

    reranker = SemanticReranker(session, _FixedEmbedder())
    ranked = await reranker.rank_for_claim(
        "reduces stress",
        [_candidate(on_topic_in_vitro, 0), _candidate(off_topic_meta, 1)],
        RerankConfig(top_k=2),
    )

    assert ranked[0].source_id == on_topic_in_vitro


async def test_a_paper_scores_as_its_best_chunk(
    seeded: tuple[AsyncSession, list[str]],
) -> None:
    """The point of chunking: one passage that answers the claim is enough.

    Averaging instead would let the rest of a long abstract dilute it, and
    summing would reward a paper for being long.
    """
    session, _ = seeded
    mostly_off_topic = await _insert(
        session, pmid="96009001", vector_seed=31, chunk_cosines=[0.20, 0.85, 0.25]
    )
    evenly_close = await _insert(
        session, pmid="96009002", vector_seed=41, chunk_cosines=[0.60, 0.60]
    )
    await session.flush()

    reranker = SemanticReranker(session, _FixedEmbedder())
    ranked = await reranker.rank_for_claim(
        "reduces stress",
        [_candidate(mostly_off_topic, 0), _candidate(evenly_close, 1)],
        RerankConfig(top_k=2),
    )

    by_id = {entry.source_id: entry for entry in ranked}
    assert by_id[mostly_off_topic].cosine_similarity == pytest.approx(0.85, abs=1e-6)
    assert by_id[evenly_close].cosine_similarity == pytest.approx(0.60, abs=1e-6)
    assert ranked[0].source_id == mostly_off_topic


@pytest.mark.parametrize("target", [0.30, 0.72, 0.85, 0.90])
async def test_the_helper_really_produces_the_cosine_it_claims(
    seeded: tuple[AsyncSession, list[str]], target: float
) -> None:
    """The similarity-gap tests above are only meaningful if this holds.

    Checked against pgvector's own operator rather than recomputed in Python,
    so a disagreement between the construction and the database shows up here
    instead of as a confusing ranking result.
    """
    session, _ = seeded
    source_id = await _insert(
        session, pmid=f"96008{int(target * 100):03d}", vector_seed=21, cosine=target
    )
    await session.flush()

    measured = await session.scalar(
        sa.text(
            "SELECT 1 - (embedding <=> CAST(:q AS vector))"
            " FROM source_chunks WHERE source_id = :id"
        ),
        {"q": str(_vector(999)), "id": source_id},
    )

    assert measured == pytest.approx(target, abs=1e-6)
