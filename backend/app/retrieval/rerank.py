"""Rank one claim's candidate papers, then number them S1..Sn for the article.

    score = best chunk similarity + grade bonus + recency bonus

Similarity alone answers "is this paper about the claim?", not "is it good
evidence?". A cell-culture study written in the claim's own words would beat a
systematic review written clinically, so stronger study designs get a bonus.
The bonus sizes are starting values to tune against real output, not settled
numbers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Float, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.contracts import CachedCandidate, RankedSource
from app.domain.enums import EVIDENCE_RANK, StudyType
from app.domain.models import Source, SourceChunk
from app.llm.embeddings.base import EmbeddingProvider

logger = logging.getLogger(__name__)

#: Added to the similarity by study design. Meta-analysis to in-vitro spans
#: 0.25, enough to reorder papers across a realistic similarity gap.
GRADE_BONUS: dict[StudyType, float] = {
    StudyType.META_ANALYSIS: 0.15,
    StudyType.SYSTEMATIC_REVIEW: 0.15,
    StudyType.RCT: 0.08,
    StudyType.OBSERVATIONAL: 0.0,
    # Narrative reviews read like systematic ones to an embedding model, so
    # they need a push down or they crowd out primary evidence.
    StudyType.NARRATIVE_REVIEW: -0.03,
    StudyType.CASE_REPORT: -0.05,
    StudyType.ANIMAL: -0.10,
    StudyType.IN_VITRO: -0.10,
    # An unclassified paper cannot raise the verdict ceiling, so it should not
    # take a prompt slot a gradeable paper could fill.
    StudyType.UNKNOWN: -0.08,
}

RECENCY_BONUS_MAX = 0.03
RECENCY_FULL_CREDIT_YEARS = 5
RECENCY_ZERO_CREDIT_YEARS = 15

DEFAULT_TOP_K = 8
MIN_ABSTRACT_CHARS = 200


@dataclass(frozen=True, slots=True)
class RerankConfig:
    top_k: int = DEFAULT_TOP_K
    min_year: int | None = None
    #: No floor by default: filtering hard can empty the pool for a fringe
    #: topic, and "no evidence" should be reachable honestly.
    min_grade: StudyType = StudyType.UNKNOWN
    min_abstract_chars: int = MIN_ABSTRACT_CHARS


#: The papers matching the claim best are kept whatever the bonuses say. A review
#: earns 0.15 + up to 0.03 recency to an older trial's 0.08, so on wt_087 (2026-11-07)
#: the melanoma trial, second best of 51 by similarity (0.863), lost its place to
#: sun-behaviour reviews matching 0.78. Three, so the bonuses still decide the rest.
#: Only trials and systematic reviews: on conf_004 a narrative review titled like the
#: question pushed out a 2024 meta-analysis, and a narrative review caps at `weak`.
KEEP_BEST_MATCHES = 3
PROTECTED_TYPES = frozenset(
    {StudyType.RCT, StudyType.SYSTEMATIC_REVIEW, StudyType.META_ANALYSIS}
)


def select_top(ranked: list[RankedSource], top_k: int) -> list[RankedSource]:
    """The top ``top_k`` of ``ranked`` (sorted by score, best first), with the
    ``KEEP_BEST_MATCHES`` most similar trials and systematic reviews always kept;
    the lowest-scoring others make room for them."""
    keep = sorted(
        (entry for entry in ranked if entry.paper.study_type in PROTECTED_TYPES),
        key=lambda entry: entry.cosine_similarity,
        reverse=True,
    )[: min(KEEP_BEST_MATCHES, top_k)]
    ids = {entry.source_id for entry in keep}
    rest = [entry for entry in ranked if entry.source_id not in ids][: top_k - len(keep)]
    return sorted(keep + rest, key=lambda entry: entry.final_score, reverse=True)


def recency_bonus(year: int | None, *, current_year: int | None = None) -> float:
    """Full credit up to 5 years old, falling linearly to zero at 15.

    An unknown year scores zero rather than a penalty.
    """
    if year is None:
        return 0.0
    age = (current_year or datetime.now(UTC).year) - year
    if age <= RECENCY_FULL_CREDIT_YEARS:
        return RECENCY_BONUS_MAX
    if age >= RECENCY_ZERO_CREDIT_YEARS:
        return 0.0
    span = RECENCY_ZERO_CREDIT_YEARS - RECENCY_FULL_CREDIT_YEARS
    return RECENCY_BONUS_MAX * (1 - (age - RECENCY_FULL_CREDIT_YEARS) / span)


def score(cosine: float, study_type: StudyType, year: int | None) -> float:
    """Similarity plus the grade and recency bonuses."""
    return cosine + GRADE_BONUS.get(study_type, 0.0) + recency_bonus(year)


class SemanticReranker:
    def __init__(self, session: AsyncSession, embedder: EmbeddingProvider) -> None:
        self._session = session
        self._embedder = embedder

    async def rank_for_claim(
        self,
        claim: str,
        candidates: list[CachedCandidate],
        config: RerankConfig | None = None,
        *,
        subject: str | None = None,
    ) -> list[RankedSource]:
        """Score this run's candidates against one claim and keep the top k.

        ``subject`` is what the claim is about ("magnesium"); the papers are
        scored against ``ranking_text(claim, subject)``. See that function for
        why the bare claim is not enough.

        Three rules:

        * **Only this run's candidates are ranked**, never the whole cache.
          The scholarly APIs decide what is relevant; the vector store only
          re-orders their results. Searching the whole cache would surface
          papers on other topics that happen to sit nearby.
        * **A paper's similarity is its best chunk's.** A paper with no chunks
          is not embedded yet, and is left out rather than scored as zero.
        * **Top-k is cut in Python, after the bonuses.** A SQL ``LIMIT`` on
          similarity alone would drop exactly the reviews the grade bonus
          exists to promote. So the query has no ``LIMIT``, and Postgres
          scores every candidate exactly (``tests/unitTest/test_rerank_plan.py``).

        Handles are placeholders here; ``assign_handles`` numbers them across
        the whole article.
        """
        config = config or RerankConfig()
        if not candidates:
            return []

        claim_vector = await self._embedder.embed_query(ranking_text(claim, subject))
        similarity = (1 - SourceChunk.embedding.cosine_distance(claim_vector)).cast(Float)
        statement = (
            select(
                Source.id,
                Source.study_type,
                Source.year,
                func.max(similarity).label("cosine_similarity"),
            )
            .join(SourceChunk, SourceChunk.source_id == Source.id)
            .where(
                Source.id.in_([entry.source_id for entry in candidates]),
                func.length(Source.abstract) >= config.min_abstract_chars,
            )
            .group_by(Source.id)
        )
        if config.min_year:
            statement = statement.where(Source.year >= config.min_year)

        rows = (await self._session.execute(statement)).all()

        papers = {entry.source_id: entry.paper for entry in candidates}
        floor = EVIDENCE_RANK[config.min_grade]
        ranked: list[RankedSource] = []
        for row in rows:
            study_type = StudyType(row.study_type)
            if EVIDENCE_RANK[study_type] < floor:
                continue
            cosine = float(row.cosine_similarity)
            ranked.append(
                RankedSource(
                    source_id=row.id,
                    claim=claim,
                    citation_handle="S0",  # placeholder; see assign_handles
                    paper=papers[row.id],
                    cosine_similarity=cosine,
                    final_score=score(cosine, study_type, row.year),
                )
            )

        ranked.sort(key=lambda entry: entry.final_score, reverse=True)
        top = select_top(ranked, config.top_k)

        logger.info(
            "rerank claim=%r: %d candidates -> %d kept (top score %.3f)",
            claim,
            len(candidates),
            len(top),
            top[0].final_score if top else 0.0,
        )
        return top


def ranking_text(claim: str, subject: str | None) -> str:
    """What a claim's candidates are scored against: the subject, then the claim.

    The bare claim ("improves sleep quality", "treats depression") names the
    outcome and not the substance, so every paper about the outcome scores the
    same whichever substance it studied. Measured 2026-10-01 over 143 cases:
    13 known-relevant papers reached a claim's candidate pool and still missed
    its top 12, each beaten by papers on the outcome alone — a herbal-medicine
    trial outranking the magnesium trial for magnesium and sleep, general
    depression reviews outranking the vitamin D meta-analysis. The search had
    the subject in it; providers that match loosely (OpenAlex, Europe PMC's
    full-text fallback) let off-subject papers into the pool, and ranking on
    the bare claim could not tell them apart.

    ``FULL_TEXT`` deliberately keeps the bare claim when it picks excerpts: there
    every passage already comes from an on-subject paper, and the subject name
    was measured pulling literature-search boilerplate up instead.
    """
    return f"{subject.strip()} {claim.strip()}" if subject and subject.strip() else claim


def claim_subject(product: str, ingredients: list[str]) -> str:
    """The subject a claim is about, chosen the way the search query chose it.

    Ingredients first, since they name the actives and a multi-ingredient
    product's name is a brand; the product when there are none.
    """
    names = [name.strip() for name in ingredients if name.strip()]
    return " and ".join(names[:3]) if names else product.strip()


RankedByClaim = dict[str, list[RankedSource]]


def assign_handles(ranked_by_claim: RankedByClaim) -> RankedByClaim:
    """Number sources S1..Sn once, across the whole article.

    * A source shared by two claims keeps **one** handle. Two handles for one
      paper would let the model cite it as two independent findings.
    * S1 is the highest-scoring source, since the model leans on early handles.
    """
    best_score: dict[str, float] = {}
    for ranked in ranked_by_claim.values():
        for entry in ranked:
            best_score[entry.source_id] = max(
                entry.final_score, best_score.get(entry.source_id, entry.final_score)
            )

    ordered = sorted(best_score, key=lambda source_id: best_score[source_id], reverse=True)
    handles = {source_id: f"S{index}" for index, source_id in enumerate(ordered, start=1)}

    return {
        claim: [
            entry.model_copy(update={"citation_handle": handles[entry.source_id]})
            for entry in ranked
        ]
        for claim, ranked in ranked_by_claim.items()
    }
