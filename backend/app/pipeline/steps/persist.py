"""Stages 5 and 6 — validate the draft, then persist it.

Kept in one module because they are two halves of one decision: validation
determines the status the article is written with, and neither half is
meaningful without the other.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.contracts import SynthesisInput, ValidationFailure
from app.domain.enums import ArticleStatus
from app.domain.models import Article, ArticleSource
from app.evidence.validation import (
    STANCE_WARNING_CODES,
    summarise_failures,
    validate_draft,
)
from app.pipeline.graph import MAX_REVISE_ROUNDS
from app.pipeline.stages import PipelineContext, StageError, StageName
from app.services.media import image_node
from app.services.tiptap import MalformedBodyError, body_text_to_doc

logger = logging.getLogger(__name__)


class ValidateStage:
    """Stage 5 — enforce invariants #2 and #3 before anything is written."""

    name = StageName.VALIDATE

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Run ``validate_draft`` and attach the report to the context.

        **A failing report is not a stage error.** The stage succeeds; the
        article is simply written as ``validation_failed`` instead of
        ``pending_review``. Raising here would lose the report, and the report
        is the entire diagnostic value of catching the problem — "the model
        cited S9, which was never provided" is actionable; "stage 5 failed" is
        not.
        """
        if ctx.draft is None or ctx.synthesis_input is None:
            raise StageError(self.name, "synthesis stage did not run")

        # The last draft this run will write: a soft check becomes a warning
        # rather than a failure (see ``check_no_evidence_cites_nothing``).
        report = await validate_draft(
            self._session,
            ctx.draft,
            ctx.synthesis_input,
            final_round=ctx.revise_round >= MAX_REVISE_ROUNDS,
        )

        ctx.record_metrics(
            self.name,
            {
                "passed": report.passed,
                "citations_resolved": report.citations_resolved,
                "citations_total": report.citations_total,
                "best_evidence_grade": str(report.best_evidence_grade),
                "failure_codes": sorted({f.code for f in report.failures}),
                "warning_codes": sorted({w.code for w in report.warnings}),
                # Whether the draft's verdict disagreed with how APPRAISE read
                # its sources. None when nothing was appraised.
                "stance_disagrees": (
                    None
                    if report.stance_tally is None
                    else bool({w.code for w in report.warnings} & STANCE_WARNING_CODES)
                ),
                "summary": summarise_failures(report),
            },
        )

        return ctx.model_copy(update={"validation": report})


class PersistStage:
    """Stage 6 — write the article and its provenance.

    This is the pipeline's terminal write, and the only place it touches
    ``articles``. Note what it cannot do: there is no code path from here to
    ``published``. That status is reachable only through
    ``ReviewService.approve()`` with an authenticated reviewer, and the DB
    CHECK constraint refuses it without one. Invariant #1 is structural, not a
    convention this stage is politely observing.
    """

    name = StageName.PERSIST

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Write the article, its provenance rows, and the validation report."""
        if ctx.draft is None or ctx.validation is None or ctx.extraction is None:
            raise StageError(self.name, "earlier stages did not complete")

        report = ctx.validation

        # A body we cannot render is a draft problem, not a system problem —
        # record it as a validation failure rather than raising. VALIDATE
        # already runs this parse (`check_body_parses`), so the report normally
        # carries the failure already; this is the backstop, not the check.
        try:
            content = body_text_to_doc(
                ctx.draft.body,
                # Above beat 1, inside `original_content`, as an ordinary image
                # node. So the picture is part of the immutable draft the
                # reviewer is shown, and removing it is an edit like any other
                # rather than a setting somewhere. Beats stay addressable —
                # `beat_text` reads `attrs.beat`, never position.
                lead_image=(
                    image_node(ctx.illustration.lead.src, ctx.illustration.lead.alt)
                    if ctx.illustration is not None
                    else None
                ),
            )
        except MalformedBodyError as exc:
            content = {"type": "doc", "content": []}
            if not any(failure.code == "malformed_body" for failure in report.failures):
                report = report.model_copy(
                    update={
                        "passed": False,
                        "failures": [
                            *report.failures,
                            ValidationFailure(
                                code="malformed_body",
                                message=str(exc),
                                detail={"where": exc.where, "text": exc.text},
                            ),
                        ],
                    }
                )

        article_status = (
            ArticleStatus.PENDING_REVIEW if report.passed else ArticleStatus.VALIDATION_FAILED
        )

        article = Article(
            slug=_slugify(ctx.draft.headline),
            status=article_status,
            topic=ctx.topic,
            product=ctx.extraction.product,
            target_claims=ctx.extraction.target_claims,
            ingredients=ctx.extraction.ingredients,
            headline=ctx.draft.headline,
            summary=ctx.draft.summary,
            verdict=ctx.draft.verdict,
            verdict_qualifier=ctx.draft.verdict_qualifier,
            # Written once. A DB trigger rejects any later UPDATE that changes
            # it — human edits go to edited_content.
            original_content=content,
            edited_content=None,
            # Both frames plus the prompt, model and seed that made them. The
            # cover is not in the document, so this column is the only record
            # of it — and the only place `derive_card` can find it at publish
            # time. NULL whenever ILLUSTRATE produced nothing, which is normal.
            generated_imagery=(
                ctx.illustration.model_dump(mode="json")
                if ctx.illustration is not None
                else None
            ),
            evidence_grade=report.best_evidence_grade,
            validation_report=report.model_dump(mode="json"),
            pipeline_run_id=ctx.run_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self._session.add(article)
        await self._session.flush()

        # The *body*, not ``all_cited_handles()``. That union includes handles
        # the model named only in its ``citations`` list, and this flag is what
        # ``FeedService.article`` filters the public source list on — so a
        # handle listed but never written about was shown to the reader as a
        # source the article cites, beside prose that never refers to it. The
        # union is still right where it is used: hallucination and resolution
        # checks must see every handle the model emitted anywhere.
        cited = ctx.draft.body.cited_handles()
        excerpts = excerpts_shown(ctx.synthesis_input)
        seen: set[tuple[str, str]] = set()
        for claim, ranked in ctx.ranked.items():
            for entry in ranked:
                key = (entry.source_id, claim)
                if key in seen:
                    continue
                seen.add(key)
                self._session.add(
                    ArticleSource(
                        article_id=article.id,
                        source_id=entry.source_id,
                        claim=claim,
                        citation_handle=entry.citation_handle,
                        # Uncited sources are recorded deliberately: the review
                        # panel shows what was retrieved and not used, because
                        # omission is the failure mode human review exists to
                        # catch.
                        was_cited=entry.citation_handle in cited,
                        relevance_score=entry.final_score,
                        excerpts=excerpts.get(entry.source_id),
                        # NULL when not appraised; see 0001_initial.sql.
                        stance=ctx.stances.get(claim, {}).get(entry.source_id),
                    )
                )

        await self._session.flush()

        logger.info(
            "persisted article %s as %s (%s)",
            article.id,
            article_status,
            report.badge,
        )

        ctx.record_metrics(
            self.name,
            {
                "article_id": article.id,
                "status": str(article_status),
                "source_links": len(seen),
                "cited_sources": len(cited),
                "illustrated": ctx.illustration is not None,
            },
        )

        return ctx.model_copy(update={"article_id": article.id, "validation": report})


def excerpts_shown(
    synthesis_input: SynthesisInput | None,
) -> dict[str, list[dict[str, str | None]]]:
    """The full-text excerpts each source carried in the prompt, by source id.

    Read from ``synthesis_input`` — exactly what the model saw — rather than
    from ``ctx.excerpts``, for the same reason VALIDATE checks handles against
    it: a second reconstruction is free to drift from the first. Sources shown
    without excerpts are absent, so their column stays NULL.
    """
    if synthesis_input is None:
        return {}
    return {
        source.source_id: [excerpt.model_dump() for excerpt in source.excerpts]
        for source in synthesis_input.sources
        if source.excerpts
    }


def _slugify(headline: str) -> str:
    """Kebab-case headline plus a short random suffix.

    The suffix guarantees uniqueness without a retry loop — two articles on the
    same topic will produce near-identical headlines, and a unique-index
    violation at the end of a multi-minute pipeline run is an expensive way to
    discover that.
    """
    base = re.sub(r"[^a-z0-9]+", "-", headline.lower()).strip("-")[:60].strip("-")
    return f"{base or 'article'}-{secrets.token_hex(3)}"
