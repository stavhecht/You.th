"""The generative calls through a local ``llama-server``, for models Ollama cannot load.

Selected with ``LLM_PROVIDER=llamacpp``: extraction, appraisal and synthesis all
run on the one model ``llama-server`` was started with, and only embeddings stay
on Ollama. It exists because Ternary-Bonsai-2-27B ships in ternary packings
(``PTQ1_0``, ``PQ2_0``) that only PrismML's llama.cpp fork can run — stock
Ollama and stock llama.cpp reject them.

**Why every call, not just synthesis.** Measured 2026-10-15 on the 16 GB M2 Pro:
Bonsai (~7.4 GB with its 32K KV cache) and llama3.1:8b (5.8–6.9 GB) cannot share
the GPU in either direction. With both resident, Ollama answered appraisal with
an empty body, and llama-server failed with ``HTTP 500: Compute error`` — and
then failed *every* later request, a two-word prompt included, until restarted,
while ``/health`` still said ok. Ollama's embedding model (0.7 GB) shares the GPU
with Bonsai without trouble (twenty embedding batches, no error). So the only
generative model on the machine is Bonsai.

It mirrors ``ollama_client`` deliberately: the same system prompts, the same
flattened schemas as decoding grammars, the same temperatures and output caps,
the same single repair turn with the same wording (``repair_instruction``). A
comparison between the two providers should measure the model and nothing else.

Differences worth knowing:

* **The context window is the server's, not the request's.** ``llama-server
  -c`` fixes it at start-up; there is no per-request ``num_ctx``. Start it at
  32768, for the silent-truncation reason ``SYNTHESIS_NUM_CTX`` documents.
* **Ollama's language models are unloaded before every call** (``make_room``),
  keeping the embedding model. Nothing in the pipeline should have loaded one,
  but anything that did (an eval judge, a manual ``ollama run``) would otherwise
  break the server for every run after it.
* **An empty answer is named as what it is.** Out of GPU memory, llama-server
  returns ``finish_reason: stop`` with no content; parsed as JSON that reads as a
  schema failure and points at the prompt. It raises a retryable ``LLMError``
  saying the server needs a restart instead, as ``Compute error`` does.
* **Reasoning counts against ``max_tokens``.** Bonsai is a thinking model. With
  ``reasoning_effort`` other than ``"none"`` the output cap is widened by
  ``REASONING_ALLOWANCE``, because a reasoning model that runs out of budget
  mid-thought returns nothing. The reasoning text itself is discarded.
* **A truncated answer is named as one** (``finish_reason == "length"``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from ollama import AsyncClient
from pydantic import BaseModel, ValidationError

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
from app.llm.base import LLMError, LLMResult, TokenUsage
from app.llm.ollama_client import (
    APPRAISAL_MAX_TOKENS,
    APPRAISAL_TEMPERATURE,
    EXTRACTION_MAX_TOKENS,
    EXTRACTION_TEMPERATURE,
    MAX_ATTEMPTS,
    READER_EDIT_MAX_TOKENS,
    READER_EDIT_TEMPERATURE,
    SYNTHESIS_MAX_TOKENS,
    SYNTHESIS_TEMPERATURE,
    _flat_schema,
    _validation_feedback,
    repair_instruction,
)
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

PROVIDER = "llamacpp"

#: Added to the output cap when the model is allowed to think. PrismML's
#: guidance is 16K+ output for a thinking turn; their "medium" effort rarely
#: exhausts that.
REASONING_ALLOWANCE = 16_384

#: Longer than Ollama's 600 s: a 27B model on a laptop GPU processes a ~15K-token
#: synthesis prompt before it writes a word, and a repair turn repeats it.
TIMEOUT_SECONDS = 1_800.0

MakeRoom = Callable[[], Awaitable[None]]


def qualified(model: str) -> str:
    """Namespace a model label for the price table."""
    return f"{PROVIDER}/{model}"


def _usage(body: dict[str, Any]) -> TokenUsage:
    usage = body.get("usage") or {}
    return TokenUsage(
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )


class LlamaCppChat:
    """One model on one ``llama-server``: the transport the three clients share."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        model: str,
        reasoning_effort: str = "none",
        make_room: MakeRoom | None = None,
    ) -> None:
        self._client = client
        self.model = model
        self._reasoning_effort = reasoning_effort
        self._make_room = make_room

    async def chat(
        self,
        *,
        call: str,
        system: str,
        user: str,
        output_format: type[BaseModel],
        max_tokens: int,
        temperature: float,
    ) -> LLMResult[Any]:
        """One structured-output turn, with a single validation-repair retry.

        Raises:
            LLMError: transport failure, an empty or truncated answer, or output
                that still fails validation after ``MAX_ATTEMPTS``.
        """
        if self._make_room is not None:
            await self._make_room()
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if self._reasoning_effort != "none":
            max_tokens += REASONING_ALLOWANCE
        usage = TokenUsage()
        last_error: ValidationError | None = None
        model_id = qualified(self.model)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            logger.info(
                "%s prompt -> %s (reasoning=%s, attempt %d/%d)",
                call, self.model, self._reasoning_effort, attempt, MAX_ATTEMPTS,
            )
            request = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": output_format.__name__,
                        "schema": _flat_schema(output_format),
                        "strict": True,
                    },
                },
                # The chat template reads it from either place depending on the
                # fork's build; both carry the same value.
                "reasoning_effort": self._reasoning_effort,
                "chat_template_kwargs": {"reasoning_effort": self._reasoning_effort},
            }
            try:
                response = await self._client.post("/v1/chat/completions", json=request)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                hint = (
                    " — llama-server is out of GPU memory and stays broken until "
                    "restarted"
                    if "Compute error" in exc.response.text
                    else ""
                )
                raise LLMError(
                    f"{call} call failed: HTTP {status}: {exc.response.text[:500]}{hint}",
                    usage=usage,
                    model=model_id,
                    retryable=status >= 500 or status == 429,
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMError(
                    f"{call} call failed: {exc!r} — is `llama-server` running?",
                    usage=usage,
                    model=model_id,
                    retryable=True,
                ) from exc

            body = response.json()
            usage = usage + _usage(body)
            choice = body["choices"][0]
            content = choice["message"].get("content") or ""

            if choice.get("finish_reason") == "length":
                raise LLMError(
                    f"{call}: output hit max_tokens={max_tokens} before the answer "
                    f"finished (reasoning={self._reasoning_effort})",
                    usage=usage,
                    model=model_id,
                )
            if not content.strip():
                raise LLMError(
                    f"{call}: llama-server returned an empty answer — the usual "
                    "cause is running out of GPU memory; restart llama-server",
                    usage=usage,
                    model=model_id,
                    retryable=True,
                )

            try:
                parsed = output_format.model_validate_json(content)
            except ValidationError as exc:
                last_error = exc
                logger.warning(
                    "%s: attempt %d/%d failed contract validation: %s",
                    call, attempt, MAX_ATTEMPTS,
                    _validation_feedback(exc).replace("\n", "; "),
                )
                messages = [
                    *messages[:2],
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": repair_instruction(exc)},
                ]
                continue

            return LLMResult(output=parsed, usage=usage, model=model_id)

        raise LLMError(
            f"{call}: output still failed validation after {MAX_ATTEMPTS} attempts "
            f"({last_error})",
            usage=usage,
            model=model_id,
            content=content,
        )


class LlamaCppExtractionClient:
    """LLM call 1 on ``llama-server``."""

    def __init__(self, chat: LlamaCppChat) -> None:
        self._chat = chat

    async def extract(self, payload: ExtractionInput) -> LLMResult[ExtractionOutput]:
        """Extract product, claims and ingredients from a topic + optional blurb.

        Raises:
            LLMError: as ``LlamaCppChat.chat``.
        """
        result = await self._chat.chat(
            call="extraction",
            system=EXTRACTION_SYSTEM_PROMPT,
            user=build_extraction_user_prompt(payload),
            output_format=ExtractionOutput,
            max_tokens=EXTRACTION_MAX_TOKENS,
            temperature=EXTRACTION_TEMPERATURE,
        )
        parsed: ExtractionOutput = result.output
        logger.info(
            "extracted %d claims for topic=%r", len(parsed.target_claims), payload.topic
        )
        return LLMResult(output=parsed, usage=result.usage, model=result.model)


class LlamaCppSynthesisClient:
    """LLM call 2 on ``llama-server``. Produces a draft; checks nothing."""

    def __init__(self, chat: LlamaCppChat) -> None:
        self._chat = chat

    async def synthesize(
        self, payload: SynthesisInput, *, feedback: str | None = None
    ) -> LLMResult[SynthesisOutput]:
        """Write a grounded article from the retrieved abstracts.

        Raises:
            LLMError: as ``LlamaCppChat.chat``.
        """
        result = await self._chat.chat(
            call="synthesis",
            system=SYNTHESIS_SYSTEM_PROMPT,
            user=build_synthesis_user_prompt(payload, feedback=feedback),
            output_format=SynthesisOutput,
            max_tokens=SYNTHESIS_MAX_TOKENS,
            temperature=SYNTHESIS_TEMPERATURE,
        )
        parsed: SynthesisOutput = result.output
        logger.info(
            "synthesised article verdict=%s citing %d/%d sources",
            parsed.verdict,
            len(parsed.all_cited_handles()),
            len(payload.sources),
        )
        return LLMResult(output=parsed, usage=result.usage, model=result.model)

    async def edit_for_readers(self, payload: ReaderEditInput) -> LLMResult[ReaderEditOutput]:
        """Reword one paragraph for everyday readers; see ``prompts/reader_edit.py``.

        Raises:
            LLMError: as ``LlamaCppChat.chat``.
        """
        return await self._chat.chat(
            call="reader_edit",
            system=READER_EDIT_SYSTEM_PROMPT,
            user=build_reader_edit_user_prompt(payload),
            output_format=ReaderEditOutput,
            max_tokens=READER_EDIT_MAX_TOKENS,
            temperature=READER_EDIT_TEMPERATURE,
        )


class LlamaCppAppraisalClient:
    """APPRAISE's calls on ``llama-server``. Labels; checks nothing."""

    def __init__(self, chat: LlamaCppChat) -> None:
        self._chat = chat

    async def appraise(self, payload: AppraisalInput) -> LLMResult[AppraisalOutput]:
        """``one_call``: label each source in ``payload`` against its claim."""
        return await self._call(
            "appraisal", APPRAISAL_SYSTEM_PROMPT, build_appraisal_user_prompt(payload),
            AppraisalOutput,
        )

    async def relevance(self, payload: AppraisalInput) -> LLMResult[RelevanceOutput]:
        """``two_call``, first call: whether each source measured the claim's outcome."""
        return await self._call(
            "appraisal relevance", RELEVANCE_SYSTEM_PROMPT,
            build_relevance_user_prompt(payload), RelevanceOutput,
        )

    async def direction(self, payload: AppraisalInput) -> LLMResult[DirectionOutput]:
        """``two_call``, second call: which way each (relevant) source points."""
        return await self._call(
            "appraisal direction", DIRECTION_SYSTEM_PROMPT,
            build_direction_user_prompt(payload), DirectionOutput,
        )

    async def _call(
        self, call: str, system: str, user: str, output_format: type[BaseModel]
    ) -> LLMResult[Any]:
        return await self._chat.chat(
            call=call,
            system=system,
            user=user,
            output_format=output_format,
            max_tokens=APPRAISAL_MAX_TOKENS,
            temperature=APPRAISAL_TEMPERATURE,
        )


def ollama_unloader(ollama: AsyncClient, *, keep: frozenset[str] = frozenset()) -> MakeRoom:
    """A ``make_room`` that unloads every Ollama model except those in ``keep``.

    ``keep`` holds the embedding model, which fits beside Bonsai and is reloaded
    by every RETRIEVE otherwise. Names match with or without ``:latest``.

    Never raises: a failed unload leaves memory as it was, so it is logged and
    the call goes on — and fails visibly on its own if memory really is short.
    """
    kept = {name.removesuffix(":latest") for name in keep}

    async def unload() -> None:
        try:
            running = await ollama.ps()
            for loaded in running.models:
                name = loaded.model or loaded.name
                if not name or name.removesuffix(":latest") in kept:
                    continue
                await ollama.generate(model=name, prompt="", keep_alive=0)
                logger.info("unloaded %s from ollama to make room for llama-server", name)
        except Exception:
            logger.warning("could not unload ollama models", exc_info=True)

    return unload


def build_llamacpp_client(base_url: str) -> httpx.AsyncClient:
    """Transport for ``llama-server``. No retry policy, as for Ollama."""
    return httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT_SECONDS)
