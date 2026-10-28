"""Provider-agnostic interfaces for the generative calls.

The pipeline depends on these Protocols, never on a concrete client, so a
provider swap or a fake in tests is a constructor argument rather than a
refactor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.contracts import (
    AppraisalInput,
    AppraisalOutput,
    DirectionOutput,
    ExtractionInput,
    ExtractionOutput,
    ReaderEditInput,
    ReaderEditOutput,
    RelevanceOutput,
    SynthesisInput,
    SynthesisOutput,
)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What one model call consumed, by token class.

    Persisted per stage in ``pipeline_stage_runs`` as four typed columns, and
    rolled up onto ``pipeline_runs``. Deliberately carries no model id and no
    cost: ``__add__`` exists so a run can report a total, and a total is only
    meaningful over tokens. Extraction and synthesis can run on *different*
    models, so summed tokens have no single price — which is why the model id
    lives on ``LLMResult`` and pricing happens per call in ``llm/pricing.py``.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True, slots=True)
class LLMResult[T]:
    """A parsed, schema-valid model output plus what it cost.

    ``model`` is the provider-namespaced id (``anthropic/claude-sonnet-5``),
    not the bare setting value. It is the join key into the price table, and it
    has to come back from the call rather than be read from settings at the
    persistence site: a run in flight when someone edits ``SYNTHESIS_MODEL``
    would otherwise be priced against a model it never used.
    """

    output: T
    usage: TokenUsage
    model: str = ""


class ExtractionClient(Protocol):
    """LLM call 1."""

    async def extract(self, payload: ExtractionInput) -> LLMResult[ExtractionOutput]:
        """Extract product, target claims and ingredients from a topic."""
        ...


class SynthesisClient(Protocol):
    """LLM call 2 — the RAG generation.

    Implementations MUST NOT attempt to validate grounding. Producing the draft
    and checking the draft are separate responsibilities on purpose:
    ``evidence/validation.py`` owns the check, and it runs against the prompt's
    handle set and the database — neither of which the model can influence.
    """

    async def synthesize(
        self, payload: SynthesisInput, *, feedback: str | None = None
    ) -> LLMResult[SynthesisOutput]:
        """Write a grounded article from the retrieved abstracts.

        ``feedback`` re-runs the same payload with an editorial note appended as
        a further user turn — used by ``SynthesizeStage`` to ask once for a
        fuller article when a draft came back short despite plentiful sources.
        It carries no new *sources* and no new rules, so the grounding contract
        is unchanged: the handle set the draft may cite is still exactly the one
        in ``payload``.

        Keyword-only and defaulted so that a caller which does not re-prompt is
        written the same way it always was.
        """
        ...

    async def edit_for_readers(self, payload: ReaderEditInput) -> LLMResult[ReaderEditOutput]:
        """Reword one paragraph of a checked draft for everyday readers.

        The model only proposes. ``pipeline/steps/reader_edit.py`` decides
        whether the rewording changed anything but the wording, and keeps the
        original paragraph if it did.
        """
        ...


class AppraisalClient(Protocol):
    """APPRAISE's calls — which way each ranked source points for one claim.
    ``APPRAISAL_MODE`` decides whether the stage uses ``appraise`` or the
    ``relevance`` and ``direction`` pair.

    Labels only. What a label is allowed to change (today: nothing but a
    reviewer warning) is decided in ``evidence/validation.py``, not here.
    """

    async def appraise(self, payload: AppraisalInput) -> LLMResult[AppraisalOutput]:
        """``one_call`` mode: label every source in ``payload`` against its claim."""
        ...

    async def relevance(self, payload: AppraisalInput) -> LLMResult[RelevanceOutput]:
        """``two_call`` mode, first call: whether each source measured the claim's
        outcome."""
        ...

    async def direction(self, payload: AppraisalInput) -> LLMResult[DirectionOutput]:
        """``two_call`` mode, second call: which way each source points. Given
        only the sources the first call accepted."""
        ...


class LLMError(RuntimeError):
    """Transport, rate-limit, or schema failure from a generative call.

    Carries ``usage`` when the failure happened *after* the provider answered,
    because those tokens are billed exactly like a successful call's. The
    expensive case is truncation: hitting ``max_tokens`` spends the entire
    output budget — 8K on synthesis, the largest single charge the pipeline can
    incur — and produces nothing. A failed run reporting zero cost would make
    the pipeline look cheapest at the moment it is burning the most, so the
    stage records this before converting the error into a ``StageError``.

    Left empty on a transport failure, where nothing was consumed.

    ``retryable`` marks a failure a later attempt can succeed past: the model
    server unreachable, a timeout, a 5xx or a rate limit. The stages pass it on
    to ``StageError`` so the worker requeues the run. Everything else — a
    missing model, output that broke the contract twice, a refusal — stays
    permanent, since a retry sends the identical request.

    ``content`` is the model's last answer when that answer broke the output
    contract, so SYNTHESIZE can fit an overflow instead of paying for a new draft.
    """

    def __init__(
        self,
        message: str,
        *,
        usage: TokenUsage | None = None,
        model: str = "",
        retryable: bool = False,
        content: str = "",
    ) -> None:
        super().__init__(message)
        self.usage = usage or TokenUsage()
        self.model = model
        self.retryable = retryable
        self.content = content


class RefusalError(LLMError):
    """The model declined the request.

    Distinct from a transport failure because retrying identical input will not
    help. Surfaced to the run as a terminal stage error so it is visible in the
    console rather than retried in a loop.
    """
