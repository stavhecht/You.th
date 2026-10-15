"""Claude-backed implementations of the two generative calls.

Model choices and constraints, for reviewers unfamiliar with this API surface:

* **The two calls run on different models**, because they are different jobs.
  Extraction is a short, mechanical structured response — a small model is
  sufficient. Synthesis is where the product's guarantees live: it must cite
  only supplied handles (invariant #2) and keep its verdict under the evidence
  ceiling (invariant #3), so it gets the stronger model. Both are config, not
  constants — see ``settings.extraction_model`` / ``settings.synthesis_model``.
* Structured outputs via ``messages.parse(output_format=...)``. The response is
  constrained to the Pydantic schema and comes back as a validated instance,
  which eliminates malformed-JSON handling entirely. Supported on every model
  named below; verify before configuring an older one.
* ``temperature`` / ``top_p`` / ``top_k`` are **rejected** on Opus 5 and
  Sonnet 5 — a request carrying them returns a 400. Behaviour is steered by
  prompt only. (Older/smaller models still accept them; this code sends none
  either way, so it is safe across the range.)
* ``max_tokens`` caps thinking **plus** visible output. Synthesis is therefore
  given 8K even though the article is ~300 words; sizing ``max_tokens`` to the
  article would truncate mid-response, and a truncated structured response has
  no parsed output at all — which is why ``_check_truncation`` names the cause
  rather than letting it surface as a schema failure. Both calls always pass an
  explicit cap — never rely on a default.
* **Both calls pass ``thinking`` explicitly.** Whether an omitted ``thinking``
  means "think" or "don't think" varies across the model range (Sonnet 5 and
  Opus 5 think; Opus 4.8 and 4.7 do not), and the model here is config. Passing
  it explicitly is what keeps the token budgets above meaningful regardless of
  ``settings.extraction_model`` / ``settings.synthesis_model``.
* The system prompt is byte-stable across every call and carries the
  ``cache_control`` breakpoint. Anything per-article goes in the user turn,
  after it — a single volatile byte in the prefix would invalidate the cache
  for everything following it.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import TextBlockParam, ThinkingConfigAdaptiveParam

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
from app.llm.base import LLMError, LLMResult, RefusalError, TokenUsage
from app.llm.prompts.appraisal import (
    APPRAISAL_SYSTEM_PROMPT,
    DIRECTION_SYSTEM_PROMPT,
    RELEVANCE_SYSTEM_PROMPT,
    build_appraisal_user_prompt,
    build_direction_user_prompt,
    build_relevance_user_prompt,
)
from app.llm.prompts.extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    build_extraction_user_prompt,
)
from app.llm.prompts.reader_edit import (
    READER_EDIT_SYSTEM_PROMPT,
    build_reader_edit_user_prompt,
)
from app.llm.prompts.synthesis import (
    SYNTHESIS_SYSTEM_PROMPT,
    build_synthesis_user_prompt,
)

logger = logging.getLogger(__name__)

# Defaults only — the orchestrator passes settings.extraction_model /
# settings.synthesis_model. Split by job: extraction is mechanical, synthesis
# carries the grounding and verdict-ceiling guarantees.
#
# Extraction is NOT on a small model, despite being the mechanical call. Both
# default to claude-sonnet-5 because claude-haiku-4-5 left `ingredients` empty
# in 9 of 10 samples (measured; see config.py), and an empty ingredient list
# silently unanchors the PubMed query. Haiku is also pre-adaptive-thinking, so
# the `thinking` argument below would 400 on it.
EXTRACTION_MODEL = "claude-sonnet-5"
SYNTHESIS_MODEL = "claude-sonnet-5"

# Every call passes an explicit cap; there is no unbounded request in this file.
# Both caps size thinking *plus* the visible response — see ADAPTIVE_THINKING.
# Extraction's output is tiny (a product, <=6 claims, <=12 ingredients); the
# budget is what it is to leave room for thinking, not for the JSON.
EXTRACTION_MAX_TOKENS = 4_000
#: 16K rather than 8K since articles grew to a three-to-five minute read
#: (~700-950 words, plus the citations list). Because this cap covers thinking
#: as well, an under-sized budget does not truncate the tail of a finished
#: article — it spends the whole budget thinking and returns nothing parseable,
#: which is why `_check_truncation` names the cause explicitly instead of
#: letting it surface as a schema failure.
SYNTHESIS_MAX_TOKENS = 16_000
#: Twelve short labelled items; like extraction, the budget is for thinking.
APPRAISAL_MAX_TOKENS = 8_000
#: One paragraph; like extraction, the budget is mostly for thinking.
READER_EDIT_MAX_TOKENS = 4_000

#: Passed explicitly on both calls rather than relying on the model default,
#: because that default is not stable across the model range: Sonnet 5 and
#: Opus 5 think when `thinking` is omitted, Opus 4.8 and 4.7 do not. Leaving it
#: implicit means `settings.extraction_model` silently decides whether the
#: token budget above is spent on thinking — which is exactly the kind of
#: config-dependent behaviour this file exists to keep out of the stages.
ADAPTIVE_THINKING: ThinkingConfigAdaptiveParam = {"type": "adaptive"}

#: Prefixed onto every model id this client reports, so the price table can tell
#: `claude-sonnet-5` run here from the same name served anywhere else — and, more
#: to the point, from a local model, which is free. Matches the namespacing
#: `sources.embedding_model` already uses for vectors.
PROVIDER = "anthropic"


def qualified(model: str) -> str:
    """Namespace a bare setting value for the price table."""
    return f"{PROVIDER}/{model}"


def _usage_from_response(response: Any) -> TokenUsage:
    """Map the SDK usage object onto our own record."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


def _system_blocks(prompt: str) -> list[TextBlockParam]:
    """The stable system prefix, marked as the cache breakpoint.

    **The breakpoint is a no-op below the model's minimum cacheable prefix**
    (1024 tokens on Sonnet 5 / Opus 4.8, 512 on Opus 5). There are no tools on
    these calls, so the cacheable prefix is this prompt alone — and the
    extraction prompt is ~350 tokens, well under. It caches nothing, silently:
    no error, just ``cache_creation_input_tokens: 0``. Left in place because it
    costs nothing and starts working if the prompt grows, but do not count the
    saving on extraction. Synthesis (~1.1K tokens) sits just over the line and
    should be confirmed against ``usage.cache_read_input_tokens`` on a real
    call rather than assumed.
    """
    return [
        {
            "type": "text",
            "text": prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _check_truncation(response: Any, call: str, model: str) -> None:
    """Raise a diagnosable error when the token budget ran out.

    Worth its own branch because the symptom is indistinguishable from a
    schema failure: a truncated response has no parsed output, so it would
    otherwise surface as "returned no parsed output" and send you looking at
    the contract instead of at ``max_tokens``. Thinking and the visible
    response share one budget, so this is what a thinking-heavy turn on a
    tight cap looks like.
    """
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise LLMError(
            f"{call}: hit max_tokens before completing the structured response "
            f"— thinking and output share this budget, so raise the cap",
            usage=_usage_from_response(response),
            model=qualified(model),
        )


def _check_refusal(response: Any, call: str, model: str) -> None:
    """Raise before any attempt to read parsed output.

    On a refusal there is no parsed output to read, so this has to run first —
    reading ``parsed_output`` on a refused response is an AttributeError that
    obscures what actually happened.
    """
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise RefusalError(
            f"{call}: model declined the request"
            + (f" (category={category})" if category else ""),
            usage=_usage_from_response(response),
            model=qualified(model),
        )


class AnthropicExtractionClient:
    """LLM call 1 — claim and ingredient extraction."""

    def __init__(self, client: AsyncAnthropic, model: str = EXTRACTION_MODEL) -> None:
        self._client = client
        self._model = model

    async def extract(self, payload: ExtractionInput) -> LLMResult[ExtractionOutput]:
        """Extract product, claims and ingredients from a topic + optional blurb.

        Raises:
            RefusalError: the model declined; retrying identical input won't help.
            LLMError: transport, rate limit, or schema failure.
        """
        user_prompt = build_extraction_user_prompt(payload)
        logger.info(
            "extraction prompt -> %s:\n--- system ---\n%s\n--- user ---\n%s",
            self._model, EXTRACTION_SYSTEM_PROMPT, user_prompt,
        )
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=EXTRACTION_MAX_TOKENS,
                thinking=ADAPTIVE_THINKING,
                system=_system_blocks(EXTRACTION_SYSTEM_PROMPT),
                messages=[{"role": "user", "content": user_prompt}],
                output_format=ExtractionOutput,
            )
        except anthropic.APIError as exc:
            raise LLMError(f"extraction call failed: {exc}", retryable=_transient(exc)) from exc

        _check_refusal(response, "extraction", self._model)
        _check_truncation(response, "extraction", self._model)

        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(
                "extraction returned no parsed output",
                usage=_usage_from_response(response),
                model=qualified(self._model),
            )

        logger.info(
            "extracted %d claims for topic=%r", len(parsed.target_claims), payload.topic
        )
        return LLMResult(
            output=parsed,
            usage=_usage_from_response(response),
            model=qualified(self._model),
        )


class AnthropicSynthesisClient:
    """LLM call 2 — the RAG generation."""

    def __init__(self, client: AsyncAnthropic, model: str = SYNTHESIS_MODEL) -> None:
        self._client = client
        self._model = model

    async def synthesize(
        self, payload: SynthesisInput, *, feedback: str | None = None
    ) -> LLMResult[SynthesisOutput]:
        """Write a grounded article from the retrieved abstracts.

        Returns whatever the model produced: schema-valid, but **unverified**.
        This method deliberately does not check grounding, citation resolution,
        or the verdict cap — that is ``validate_draft()``'s job, and keeping the
        two apart is what stops "the thing that generates" from also being "the
        thing that approves".

        Raises:
            RefusalError: the model declined.
            LLMError: transport, rate limit, or schema failure.
        """
        user_prompt = build_synthesis_user_prompt(payload, feedback=feedback)
        logger.info(
            "synthesis prompt -> %s:\n--- system ---\n%s\n--- user ---\n%s",
            self._model, SYNTHESIS_SYSTEM_PROMPT, user_prompt,
        )
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=SYNTHESIS_MAX_TOKENS,
                thinking=ADAPTIVE_THINKING,
                system=_system_blocks(SYNTHESIS_SYSTEM_PROMPT),
                messages=[{"role": "user", "content": user_prompt}],
                output_format=SynthesisOutput,
            )
        except anthropic.APIError as exc:
            raise LLMError(f"synthesis call failed: {exc}", retryable=_transient(exc)) from exc

        _check_refusal(response, "synthesis", self._model)
        _check_truncation(response, "synthesis", self._model)

        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(
                "synthesis returned no parsed output",
                usage=_usage_from_response(response),
                model=qualified(self._model),
            )

        logger.info(
            "synthesised article verdict=%s citing %d/%d sources",
            parsed.verdict,
            len(parsed.all_cited_handles()),
            len(payload.sources),
        )
        return LLMResult(
            output=parsed,
            usage=_usage_from_response(response),
            model=qualified(self._model),
        )

    async def edit_for_readers(self, payload: ReaderEditInput) -> LLMResult[ReaderEditOutput]:
        """Reword one paragraph for everyday readers; see ``prompts/reader_edit.py``.

        Raises:
            RefusalError: the model declined.
            LLMError: transport, rate limit, or schema failure.
        """
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=READER_EDIT_MAX_TOKENS,
                thinking=ADAPTIVE_THINKING,
                system=_system_blocks(READER_EDIT_SYSTEM_PROMPT),
                messages=[
                    {"role": "user", "content": build_reader_edit_user_prompt(payload)}
                ],
                output_format=ReaderEditOutput,
            )
        except anthropic.APIError as exc:
            raise LLMError(
                f"reader_edit call failed: {exc}", retryable=_transient(exc)
            ) from exc

        _check_refusal(response, "reader_edit", self._model)
        _check_truncation(response, "reader_edit", self._model)
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(
                "reader_edit returned no parsed output",
                usage=_usage_from_response(response),
                model=qualified(self._model),
            )
        return LLMResult(
            output=parsed,
            usage=_usage_from_response(response),
            model=qualified(self._model),
        )


class AnthropicAppraisalClient:
    """APPRAISE's calls on the hosted model."""

    def __init__(self, client: AsyncAnthropic, model: str = SYNTHESIS_MODEL) -> None:
        self._client = client
        self._model = model

    async def appraise(self, payload: AppraisalInput) -> LLMResult[AppraisalOutput]:
        """``one_call``: label each source in ``payload`` against its claim.

        Raises:
            RefusalError: the model declined.
            LLMError: transport, rate limit, or schema failure.
        """
        return await self._call(
            "appraisal",
            APPRAISAL_SYSTEM_PROMPT,
            build_appraisal_user_prompt(payload),
            AppraisalOutput,
        )

    async def relevance(self, payload: AppraisalInput) -> LLMResult[RelevanceOutput]:
        """``two_call``, first call. Raises as ``appraise``."""
        return await self._call(
            "appraisal relevance",
            RELEVANCE_SYSTEM_PROMPT,
            build_relevance_user_prompt(payload),
            RelevanceOutput,
        )

    async def direction(self, payload: AppraisalInput) -> LLMResult[DirectionOutput]:
        """``two_call``, second call. Raises as ``appraise``."""
        return await self._call(
            "appraisal direction",
            DIRECTION_SYSTEM_PROMPT,
            build_direction_user_prompt(payload),
            DirectionOutput,
        )

    async def _call(
        self, call: str, system: str, user: str, output_format: type[Any]
    ) -> LLMResult[Any]:
        logger.info(
            "%s prompt -> %s:\n--- system ---\n%s\n--- user ---\n%s",
            call, self._model, system, user,
        )
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=APPRAISAL_MAX_TOKENS,
                thinking=ADAPTIVE_THINKING,
                system=_system_blocks(system),
                messages=[{"role": "user", "content": user}],
                output_format=output_format,
            )
        except anthropic.APIError as exc:
            raise LLMError(f"{call} call failed: {exc}", retryable=_transient(exc)) from exc

        _check_refusal(response, call, self._model)
        _check_truncation(response, call, self._model)

        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(
                f"{call} returned no parsed output",
                usage=_usage_from_response(response),
                model=qualified(self._model),
            )
        return LLMResult(
            output=parsed,
            usage=_usage_from_response(response),
            model=qualified(self._model),
        )


def build_anthropic_client(api_key: str) -> AsyncAnthropic:
    """Shared transport for both call sites.

    ``max_retries`` covers 429s and 5xx with backoff. The timeout is generous
    because synthesis runs with thinking on, and a thinking-heavy turn on a
    long source block is genuinely slow.
    """
    return AsyncAnthropic(api_key=api_key, max_retries=3, timeout=180.0)


def _transient(exc: anthropic.APIError) -> bool:
    """Connection failures, timeouts, rate limits and 5xx can succeed later."""
    if isinstance(exc, anthropic.APIConnectionError | anthropic.RateLimitError):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status >= 500
