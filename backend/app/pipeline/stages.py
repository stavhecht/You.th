"""Stage contract and the context that flows between stages.

Every stage is ``async (PipelineContext) -> PipelineContext``. Two properties
follow from that shape, and both are deliberate:

* **Serialisable context, no shared memory.** Stages communicate only through
  ``PipelineContext``, which is a Pydantic model. That is what makes the AWS
  migration a transport swap rather than a redesign — each stage becomes a
  Lambda, the context becomes the Step Functions state document, and the
  orchestrator calls Invoke instead of the function. Reach for module-level
  state or a shared client cache inside a stage and that property is gone.

* **Room for the agentic loop without changing any stage.** Query refinement,
  when it arrives, is a loop the orchestrator runs around RETRIEVE and RANK:
  re-run with widened queries while the candidate pool is too thin. No stage
  needs to know it is being re-run. Deferred per the brief; the seam is here so
  it stays cheap.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.domain.contracts import (
    CachedCandidate,
    Excerpt,
    ExtractionOutput,
    Illustration,
    RankedSource,
    SynthesisInput,
    SynthesisOutput,
    ValidationReport,
)
from app.domain.enums import Stance
from app.llm.base import TokenUsage


class StageName(StrEnum):
    """Ordered. ``ordinal`` in pipeline_stage_runs is this enum's position.

    One-to-one with the states of the future Step Functions machine.

    That first sentence is true of rows written from now on. ``ILLUSTRATE`` was
    inserted at position 4, so runs recorded before it existed carry
    ``validate`` at ordinal 4 and ``persist`` at 5 rather than 5 and 6.
    ``FULL_TEXT`` was then inserted at position 3 (2026-09-29), shifting every
    later stage up by one again, and ``APPRAISE`` at position 4 (2026-10-02)
    did the same. Nothing joins the number to the name — the
    column only orders a single run's stages for display — so the old rows are
    correct about the pipeline they ran on. Do not backfill them into a claim
    about a pipeline that did not exist yet.

    **PERSIST must stay last.** Its write commits together with the run's
    completion row (``orchestrator._finish_run``); a stage after it reopens the
    window where an article exists whose run still says ``running``, which the
    stale sweep then requeues and writes a second time.
    """

    EXTRACT = "extract"
    RETRIEVE = "retrieve"
    RANK = "rank"
    FULL_TEXT = "full_text"
    APPRAISE = "appraise"
    SYNTHESIZE = "synthesize"
    ILLUSTRATE = "illustrate"
    VALIDATE = "validate"
    #: Inserted 2026-11-12; runs before it carry ``persist`` one ordinal lower.
    VERIFY = "verify"
    PERSIST = "persist"


class StageUsage(BaseModel):
    """One stage's model call: what it consumed and which model consumed it.

    The two travel together because neither is useful alone. Tokens without a
    model cannot be priced, and the model has to be the one the call actually
    used rather than whatever ``SYNTHESIS_MODEL`` says at persistence time.
    """

    #: Provider-namespaced, e.g. ``anthropic/claude-sonnet-5``. Empty for a
    #: stage that made no model call, or one whose call failed in transport.
    model: str = ""
    usage: TokenUsage = Field(default_factory=TokenUsage)

    model_config = {"arbitrary_types_allowed": True}


class PipelineContext(BaseModel):
    """State threaded through the pipeline. Accumulates; never mutated in place.

    Each stage returns a copy with its own outputs filled in
    (``ctx.model_copy(update=...)``), so a failed run's context shows exactly
    how far it got.
    """

    run_id: str
    topic: str
    blurb: str | None = None

    # EXTRACT
    extraction: ExtractionOutput | None = None

    # RETRIEVE — candidates per claim, post-dedup, each already paired with the
    # `sources` row it was cached to. RankStage needs the ids and RetrieveStage
    # is the stage that learns them; carrying them is what lets RankStage skip
    # re-upserting the whole candidate set to look them up again.
    candidates: dict[str, list[CachedCandidate]] = Field(default_factory=dict)

    # RANK — top-k per claim, with handles assigned
    ranked: dict[str, list[RankedSource]] = Field(default_factory=dict)

    # RETRIEVE <-> RANK refinement loop
    #: Which pass through RETRIEVE this is. 0 is the ordinary first pass and is
    #: what every stage sees unless the router below sent the run back.
    refine_round: int = 0
    #: Claims RANK found too little usable evidence for, which RETRIEVE
    #: re-searches with the outcome clause dropped. Empty on the first pass.
    #:
    #: Carried on the context rather than recomputed inside RETRIEVE because the
    #: judgement is RANK's: thinness is a property of what survived scoring and
    #: the grade floor, which RETRIEVE cannot see from its own candidate list.
    thin_claims: list[str] = Field(default_factory=list)

    # FULL_TEXT
    #: Full-text passages for the few open-access papers chosen, keyed by
    #: source id. Empty when none were chosen or Europe PMC could not be
    #: reached — an ordinary outcome: the article is then written from abstracts.
    excerpts: dict[str, list[Excerpt]] = Field(default_factory=dict)

    # APPRAISE
    #: Which way each ranked source points, keyed claim -> source id. A pair
    #: missing here was not appraised (the stage was off, its call failed, or
    #: the model skipped the handle) — never read that as ``unclear``. Built
    #: once, after the refinement loop, and reused unchanged by a revision.
    stances: dict[str, dict[str, Stance]] = Field(default_factory=dict)

    # SYNTHESIZE
    #: The exact payload rendered into the synthesis prompt. Carried forward
    #: rather than rebuilt, because VALIDATE must check against the handle set
    #: the model actually saw. Rebuilding it from `ranked` would look
    #: equivalent and would quietly break invariant #2: any drift between the
    #: two reconstructions creates a gap where a handle validates against a set
    #: that was never in the prompt.
    synthesis_input: SynthesisInput | None = None
    draft: SynthesisOutput | None = None

    # ILLUSTRATE
    #: The two generated frames, or None — which is an ordinary outcome and not
    #: a failure. PersistStage prepends ``lead`` to the document as an image
    #: node like any other; ``cover`` never enters the document and reaches the
    #: feed only through ``articles.generated_imagery`` and the pairing rule in
    #: ``services/card.py``.
    illustration: Illustration | None = None

    # VALIDATE
    validation: ValidationReport | None = None

    # VALIDATE -> SYNTHESIZE revision loop
    #: Which draft this is. 0 is the first; 1 is the one re-written after the
    #: first failed validation, with the failures fed back. SYNTHESIZE tells a
    #: revision pass apart by this and by ``validation`` holding a failed report.
    revise_round: int = 0

    # PERSIST
    article_id: str | None = None

    #: What each stage's model call consumed, keyed by stage name. Mutated in
    #: place for the same reason ``metrics`` is — and for one more: a stage that
    #: raises never returns a context, so anything recorded via ``model_copy``
    #: is lost on exactly the runs whose cost is least visible. Mutation means
    #: the orchestrator still sees the tokens a failed call burned.
    usage_by_stage: dict[str, StageUsage] = Field(default_factory=dict)

    #: Per-stage metrics, written into pipeline_stage_runs.metrics by the
    #: orchestrator. Mutated in place: it is bookkeeping about the run, not
    #: part of the data flowing through it, and threading it through every
    #: model_copy would add noise to each stage for no benefit.
    metrics: dict[str, dict[str, Any]] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def usage(self) -> TokenUsage:
        """Run total, derived rather than accumulated.

        A field would have to be summed by each stage *and* recorded per stage,
        and the two would drift the first time someone added a call and updated
        only one. Deriving it makes ``usage_by_stage`` the single record; the
        total is a view over it. Note the total spans models and so cannot be
        priced — see ``llm/pricing.py``.
        """
        total = TokenUsage()
        for entry in self.usage_by_stage.values():
            total = total + entry.usage
        return total

    def record_metrics(self, stage: StageName, values: dict[str, Any]) -> None:
        """Attach observability data for one stage."""
        self.metrics[str(stage)] = values

    def record_usage(self, stage: StageName, model: str, usage: TokenUsage) -> None:
        """Record what one stage's model call consumed, successful or not.

        Additive, because a stage may call a model more than once — Ollama's
        validation-repair retry already does, and the agentic query loop would.
        Overwriting would report the last attempt as though it were the only one.
        """
        key = str(stage)
        existing = self.usage_by_stage.get(key)
        self.usage_by_stage[key] = StageUsage(
            model=model or (existing.model if existing else ""),
            usage=(existing.usage if existing else TokenUsage()) + usage,
        )


class Stage(Protocol):
    """One pipeline stage."""

    name: StageName

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Transform the context.

        Raise ``StageError`` for a genuine failure. Do not raise to signal an
        empty-but-valid result: zero candidates for a fringe trend is a real
        outcome that must reach synthesis, because ``no_evidence`` is a verdict
        we want to be able to publish honestly, not an error state.
        """
        ...


class StageError(RuntimeError):
    """A stage failed. Recorded on pipeline_stage_runs.error."""

    def __init__(self, stage: StageName, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.retryable = retryable
