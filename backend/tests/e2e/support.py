"""Fakes and a real-stage pipeline for the end-to-end suite.

Only what talks to the outside world is faked: the extraction and synthesis
models, the paper search, and the embedder. Everything that decides what gets
written — the cache, the pgvector re-rank, validation, persistence, the
orchestrator and the worker — is the production code, against a real database.

The embedder hashes words into a fixed-width vector, so cosine similarity is
real (shared words score higher) and ranking behaves like ranking, without a
model.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.domain.contracts import (
    ArticleBody,
    CandidatePaper,
    CitationGroup,
    ExtractionInput,
    ExtractionOutput,
    ReaderEditInput,
    ReaderEditOutput,
    SynthesisInput,
    SynthesisOutput,
)
from app.domain.enums import SourceApi, StudyType, Verdict
from app.llm.base import LLMError, LLMResult, TokenUsage
from app.pipeline.stages import Stage
from app.pipeline.steps.appraise import AppraiseStage
from app.pipeline.steps.extract import ExtractStage
from app.pipeline.steps.full_text import FullTextStage
from app.pipeline.steps.illustrate import IllustrateStage, IllustrationConfig
from app.pipeline.steps.persist import PersistStage, ValidateStage
from app.pipeline.steps.rank import RankStage
from app.pipeline.steps.retrieve import RetrieveStage
from app.pipeline.steps.synthesize import SynthesizeStage
from app.retrieval.base import ProviderError, SearchQuery
from app.retrieval.cache import SourceCache
from app.retrieval.query_builder import TemplateQueryStrategy
from app.retrieval.rerank import RerankConfig, SemanticReranker

#: The end-to-end database: ``E2E_DATABASE_URL``, else the dev URL with the
#: database renamed ``evidwell_test``. Never the dev database itself.
E2E_URL = os.environ.get("E2E_DATABASE_URL") or make_url(get_settings().database_url).set(
    database="evidwell_test"
).render_as_string(hide_password=False)

USAGE = TokenUsage(input_tokens=100, output_tokens=50)


class HashEmbedder:
    """Bag of words hashed into ``EMBEDDING_DIM`` buckets, unit length."""

    model_id = "test/hash-embed"

    def __init__(self) -> None:
        self.dimension = get_settings().embedding_dim

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for word in re.findall(r"[a-z]+", text.lower()):
            bucket = int(hashlib.sha1(word.encode()).hexdigest(), 16) % self.dimension
            vector[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def paper(
    n: int, *, subject: str = "magnesium", study: StudyType = StudyType.RCT
) -> CandidatePaper:
    """A distinct, on-subject paper. Identifiers are synthetic and never real."""
    return CandidatePaper(
        pmid=f"9{n:07d}",
        doi=f"10.9999/e2e.{n}",
        title=f"{subject.capitalize()} and sleep quality: trial {n}",
        # Over MIN_ABSTRACT_CHARS (200): RANK drops shorter abstracts as unusable.
        abstract=(
            f"In 40 adults, {subject} supplementation improved sleep quality compared "
            f"with placebo. Participants in trial {n} also reported fewer night wakings "
            f"and felt more rested on waking. The authors call for larger and longer "
            f"trials before any firm recommendation is made about {subject} for sleep."
        ),
        journal="Journal of Test Evidence",
        year=2020 + n % 5,
        study_type=study,
        source_api=SourceApi.PUBMED,
    )


@dataclass
class FakeProvider:
    """Answers every query with the same papers, or with an outage."""

    papers: list[CandidatePaper]
    down: bool = False
    queries: list[SearchQuery] = field(default_factory=list)

    @property
    def source_api(self) -> SourceApi:
        return SourceApi.PUBMED

    async def search(self, query: SearchQuery) -> list[CandidatePaper]:
        self.queries.append(query)
        if self.down:
            raise ProviderError("pubmed: 503 (scripted outage)")
        return [p.model_copy() for p in self.papers]


@dataclass
class FakeExtraction:
    product: str = "Magnesium glycinate"
    claims: tuple[str, ...] = ("improves sleep quality",)
    ingredients: tuple[str, ...] = ("magnesium",)

    async def extract(self, payload: ExtractionInput) -> LLMResult[ExtractionOutput]:
        output = ExtractionOutput(
            product=self.product,
            target_claims=list(self.claims),
            ingredients=list(self.ingredients),
        )
        return LLMResult(output=output, usage=USAGE, model="test/extract")


@dataclass
class ScriptedWriter:
    """A grounded writer: every evidence sentence is lifted from the source it
    cites. ``invent_handle`` makes it cite a source it was never shown — the
    hallucination VALIDATE exists to stop. ``fail`` makes every call an outage."""

    invent_handle: bool = False
    fail: LLMError | None = None
    calls: int = 0

    async def synthesize(
        self, payload: SynthesisInput, *, feedback: str | None = None
    ) -> LLMResult[SynthesisOutput]:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        sources = payload.sources[:3]
        handles = [s.handle for s in sources]
        if self.invent_handle:
            handles = ["S99"]
        evidence = " ".join(
            f"{s.abstract.split('. ')[0].rstrip('.')} [{h}]."
            for s, h in zip(sources, handles, strict=False)
        )
        output = SynthesisOutput(
            headline=f"Does {payload.product} help you sleep?",
            verdict=Verdict.WEAK,
            summary="A few small trials point the same way. They are too small to settle it.",
            body=ArticleBody(
                beat_1_claim=f"The claim: {payload.product} {payload.target_claims[0]}.",
                beat_2_evidence=evidence,
                beat_3_bottom_line="The trials are small, so treat this as a hint.",
            ),
            citations=[CitationGroup(claim=payload.target_claims[0], source_ids=handles)],
        )
        return LLMResult(output=output, usage=USAGE, model="test/synth")

    async def edit_for_readers(self, payload: ReaderEditInput) -> LLMResult[ReaderEditOutput]:
        return LLMResult(
            output=ReaderEditOutput(sentences=[payload.paragraph]),
            usage=USAGE,
            model="test/synth",
        )


class NoFullText:
    """Never consulted: the stage is built with ``max_papers=0`` (disabled)."""


def build_stages(
    session: Any,
    *,
    provider: FakeProvider,
    writer: ScriptedWriter | None = None,
    extraction: FakeExtraction | None = None,
) -> list[Stage]:
    """``build_default_pipeline``, stage for stage, with the outside world faked."""
    settings = get_settings()
    embedder = HashEmbedder()
    return [
        ExtractStage(extraction or FakeExtraction()),
        RetrieveStage(
            [provider],  # type: ignore[list-item]
            TemplateQueryStrategy(min_year=settings.retrieval_min_year),
            SourceCache(session, embedder),
            settings.retrieval_max_candidates_per_claim,
        ),
        RankStage(
            SemanticReranker(session, embedder),
            RerankConfig(top_k=settings.retrieval_top_k, min_year=settings.retrieval_min_year),
        ),
        FullTextStage(NoFullText(), embedder, 0),  # type: ignore[arg-type]
        AppraiseStage(None),
        SynthesizeStage(writer or ScriptedWriter(), relevance_filter=False, reader_edit=True),
        IllustrateStage(
            None,
            IllustrationConfig(
                max_bytes=settings.media_max_bytes,
                lead_size=(settings.image_lead_width, settings.image_lead_height),
                cover_size=(settings.image_cover_width, settings.image_cover_height),
                enabled=False,
                cover_from_lead=True,
            ),
            session,
        ),
        ValidateStage(session),
        PersistStage(session),
    ]


async def seed_published(
    factory: Any,
    reviewer_id: str,
    *,
    headline: str,
    verdict: Verdict = Verdict.MIXED,
    subject: str | None = None,
    status: str = "published",
) -> tuple[str, str]:
    """An article inserted as a draft and published through the real
    ``ReviewService.approve`` (card derivation, search indexing). Returns
    (id, slug). ``status`` other than published leaves it at that status."""
    import json
    import uuid

    import sqlalchemy as sa

    from app.services.review import ReviewService

    article_id = str(uuid.uuid4())
    slug = f"seeded-{article_id[:8]}"
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "attrs": {"beat": 1},
                "content": [{"type": "text", "text": f"{headline}. A seeded claim."}],
            },
            {
                "type": "paragraph",
                "attrs": {"beat": 2},
                "content": [{"type": "text", "text": "A seeded finding."}],
            },
            {
                "type": "paragraph",
                "attrs": {"beat": 3},
                "content": [{"type": "text", "text": "A seeded bottom line."}],
            },
        ],
    }
    async with factory() as db:
        await db.execute(
            sa.text(
                """
                INSERT INTO articles (id, slug, status, topic, product, headline, summary,
                    verdict, subject, original_content, evidence_grade, rejection_reason,
                    created_at, updated_at)
                VALUES (:id, :slug, :status, :topic, 'Seeded product', :headline,
                    'A seeded summary.', :verdict, :subject, CAST(:doc AS jsonb), 'rct',
                    :reason, now(), now())
                """
            ),
            {
                "id": article_id,
                "slug": slug,
                "topic": headline,
                "headline": headline,
                "verdict": verdict.value,
                "subject": subject,
                "doc": json.dumps(doc),
                "status": "pending_review" if status == "published" else status,
                "reason": "Seeded rejection." if status == "rejected" else None,
            },
        )
        if status == "published":
            await ReviewService(db).approve(article_id, reviewer_id)
        await db.commit()
    return article_id, slug
