"""The generative calls, recorded and replayable — plus two stand-ins.

``EvalExtractionClient``, ``EvalAppraisalClient`` and ``EvalSynthesisClient``
wrap whatever ``llm/factory.py`` built (Ollama today) and implement the same
Protocols, so the stages cannot tell them apart. They time each call, record its
tokens and model, and keep its output in the cassette keyed by model + payload +
feedback: a replayed draft is the draft that model wrote for exactly that
prompt.

Two stand-ins, for failure cases only, and named for what they are:

* ``ScriptedSynthesisClient`` writes a plain draft out of the sources' own first
  sentences. Used where the case is about control flow — a provider outage, a
  timeout — and a local model's prose would only add minutes and noise.
* ``HallucinatingSynthesisClient`` is an adversary. It cites a handle that was
  never provided, invents a statistic, and claims ``supported``. The case then
  checks that validation, not the model, is what stops it.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict
from typing import Any

from app.domain.contracts import (
    AppraisalInput,
    AppraisalOutput,
    ArticleBody,
    CitationGroup,
    DirectionOutput,
    ExtractionInput,
    ExtractionOutput,
    ReaderEditInput,
    ReaderEditOutput,
    RelevanceOutput,
    SynthesisInput,
    SynthesisOutput,
)
from app.domain.enums import Verdict
from app.llm.base import (
    AppraisalClient,
    ExtractionClient,
    LLMError,
    LLMResult,
    SynthesisClient,
    TokenUsage,
)
from app.llm.ollama_client import _flat_schema
from app.llm.prompts.appraisal import (
    APPRAISAL_SYSTEM_PROMPT,
    DIRECTION_SYSTEM_PROMPT,
    RELEVANCE_SYSTEM_PROMPT,
)
from app.llm.prompts.extraction import EXTRACTION_SYSTEM_PROMPT, safety_note
from app.llm.prompts.reader_edit import READER_EDIT_SYSTEM_PROMPT
from app.llm.prompts.synthesis import SYNTHESIS_SYSTEM_PROMPT
from evaluation.harness.cassette import Cassette, stable_key
from evaluation.harness.trace import TraceRecorder
from evaluation.schema import FaultSpec

#: The system prompt behind each appraisal call, keyed by operation name.
_APPRAISAL_PROMPTS = {
    "appraise": APPRAISAL_SYSTEM_PROMPT,
    "relevance": RELEVANCE_SYSTEM_PROMPT,
    "direction": DIRECTION_SYSTEM_PROMPT,
}


def feedback_kind(feedback: str | None) -> str:
    """Which of SynthesizeStage's prompts this call was."""
    if not feedback:
        return "first"
    if "That draft failed the citation checks" in feedback:
        return "revision"
    if feedback.startswith("That draft gave"):
        return "coverage"
    return "other"


def synthesis_key(model_id: str, payload: SynthesisInput, feedback: str | None) -> str:
    """The cassette key for one synthesis call: what the model is *shown*.

    ``PromptSource.stances`` is left out. It rides on the payload only so
    VALIDATE can read it and ``render_source_block`` never renders it, so it
    changes nothing the model sees — and keying on it would have turned every
    recording made before APPRAISE existed into a miss, re-running every
    synthesis live and sampling a new draft.
    """
    shown = payload.model_dump(mode="json", exclude={"sources": {"__all__": {"stances"}}})
    # ``shown_claims`` is None unless ``synthesis_relevance_filter`` is on, and
    # left out then so recordings made before it existed still replay.
    for source in shown["sources"]:
        if source.get("shown_claims") is None:
            source.pop("shown_claims", None)
    # The system prompt and the output grammar are in the key too, as for
    # appraisal: without them a prompt edit replayed the old prompt's drafts and
    # an eval run said nothing about the change it was run to measure.
    return stable_key(
        "synthesize",
        model_id,
        SYNTHESIS_SYSTEM_PROMPT,
        _flat_schema(SynthesisOutput),
        shown,
        feedback or "",
    )


def _usage_dict(usage: TokenUsage) -> dict[str, int]:
    return {key: int(value) for key, value in asdict(usage).items()}


class _Recorded:
    def __init__(
        self,
        model_id: str,
        cassette: Cassette,
        mode: str,
        recorder: TraceRecorder,
        fault: FaultSpec | None,
    ) -> None:
        self._model_id = model_id
        self._cassette = cassette
        self._mode = mode
        self._recorder = recorder
        self._fault = fault

    def _lookup(self, key: str) -> tuple[Any, float | None] | None:
        if self._mode == "live":
            return None
        return self._cassette.get("llm", key)


class EvalExtractionClient(_Recorded):
    def __init__(self, inner: ExtractionClient | None, *args: Any) -> None:
        super().__init__(*args)
        self._inner = inner

    async def extract(self, payload: ExtractionInput) -> LLMResult[ExtractionOutput]:
        request = {"topic": payload.topic, "blurb": payload.blurb}
        started = time.monotonic()
        if self._fault is not None:
            self._recorder.record(
                tool="llm_extraction",
                operation="extract",
                request=request,
                status="fault",
                fault=self._fault.kind,
                error="simulated extraction failure",
                model=self._model_id,
            )
            # Shaped like the real clients' transport failure, which is retryable.
            raise LLMError(
                "extraction call failed: simulated outage (eval fault)", retryable=True
            )

        # Keyed by the prompt too, like synthesis: without it an extraction-prompt
        # edit replayed the old answers and was never measured (2026-10-23).
        # The safety note joins the key only where it applies, so other questions replay.
        note = safety_note(payload.topic)
        key = stable_key(
            "extract",
            self._model_id,
            EXTRACTION_SYSTEM_PROMPT,
            payload.model_dump(mode="json"),
            *((note,) if note else ()),
        )
        hit = self._lookup(key)
        if hit is not None:
            stored, latency = hit
            result = LLMResult(
                output=ExtractionOutput.model_validate(stored["output"]),
                usage=TokenUsage(**stored["usage"]),
                model=stored["model"],
            )
            self._recorder.record(
                tool="llm_extraction",
                operation="extract",
                request=request,
                cached=True,
                latency_ms=_ms(started),
                live_latency_ms=latency,
                model=result.model,
                usage=_usage_dict(result.usage),
            )
            return result
        if self._mode == "replay":
            self._recorder.record(
                tool="llm_extraction",
                operation="extract",
                request=request,
                status="cassette_miss",
            )
            raise LLMError("cassette miss (replay mode)")
        if self._inner is None:
            raise LLMError("no extraction client is configured")
        try:
            result = await self._inner.extract(payload)
        except LLMError as exc:
            self._recorder.record(
                tool="llm_extraction",
                operation="extract",
                request=request,
                status="error",
                error=str(exc),
                latency_ms=_ms(started),
                live_latency_ms=_ms(started),
                model=exc.model or self._model_id,
                usage=_usage_dict(exc.usage),
            )
            raise
        latency = _ms(started)
        self._cassette.put(
            "llm",
            key,
            {
                "output": result.output.model_dump(mode="json"),
                "usage": _usage_dict(result.usage),
                "model": result.model,
            },
            latency,
        )
        self._recorder.record(
            tool="llm_extraction",
            operation="extract",
            request=request,
            latency_ms=latency,
            live_latency_ms=latency,
            model=result.model,
            usage=_usage_dict(result.usage),
        )
        return result


class EvalSynthesisClient(_Recorded):
    """Synthesis, recorded and replayable, failed calls included.

    A draft that broke the contract is a property of one sample, so the stage asks
    the identical request again. Before 2026-10-28 only the success was recorded,
    so a replay answered the first call with it and took a different path from the
    live run. Now the nth identical request in a case has its own slot (the first
    keeps the plain key, so older recordings replay as before), and a permanent
    failure is stored there with the answer the stage may fit. Outages are not
    stored: a replay would fail forever on one.
    """

    def __init__(self, inner: SynthesisClient | None, *args: Any) -> None:
        super().__init__(*args)
        self._inner = inner
        self._asked: dict[str, int] = {}

    async def synthesize(
        self, payload: SynthesisInput, *, feedback: str | None = None
    ) -> LLMResult[SynthesisOutput]:
        kind = feedback_kind(feedback)
        request = {
            "product": payload.product,
            "claims": payload.target_claims,
            "handles": sorted(payload.handle_set, key=lambda handle: int(handle[1:])),
            "feedback": kind,
        }
        started = time.monotonic()
        if self._fault is not None and self._fault.kind == "error":
            self._recorder.record(
                tool="llm_synthesis",
                operation="synthesize",
                request=request,
                status="fault",
                fault="error",
                error="simulated synthesis failure",
                model=self._model_id,
            )
            raise LLMError(
                "synthesis call failed: simulated outage (eval fault)", retryable=True
            )

        key = synthesis_key(self._model_id, payload, feedback)
        nth = self._asked.get(key, 0)
        self._asked[key] = nth + 1
        if nth:
            key = f"{key}#{nth}"
        hit = self._lookup(key)
        if hit is not None and "error" in hit[0]:
            stored, latency = hit
            self._recorder.record(
                tool="llm_synthesis",
                operation="synthesize",
                request=request,
                status="error",
                error=stored["error"],
                cached=True,
                latency_ms=_ms(started),
                live_latency_ms=latency,
                model=stored["model"],
                usage=stored["usage"],
            )
            raise LLMError(
                stored["error"],
                usage=TokenUsage(**stored["usage"]),
                model=stored["model"],
                content=stored["content"],
            )
        if hit is not None:
            stored, latency = hit
            result = LLMResult(
                output=SynthesisOutput.model_validate(stored["output"]),
                usage=TokenUsage(**stored["usage"]),
                model=stored["model"],
            )
            self._note(request, result, kind, cached=True, latency=latency, started=started)
            return result
        if self._mode == "replay":
            self._recorder.record(
                tool="llm_synthesis",
                operation="synthesize",
                request=request,
                status="cassette_miss",
            )
            raise LLMError("cassette miss (replay mode)")
        if self._inner is None:
            raise LLMError("no synthesis client is configured")
        try:
            result = await self._inner.synthesize(payload, feedback=feedback)
        except LLMError as exc:
            self._recorder.record(
                tool="llm_synthesis",
                operation="synthesize",
                request=request,
                status="error",
                error=str(exc),
                latency_ms=_ms(started),
                live_latency_ms=_ms(started),
                model=exc.model or self._model_id,
                usage=_usage_dict(exc.usage),
            )
            if not exc.retryable:
                self._cassette.put(
                    "llm",
                    key,
                    {
                        "error": str(exc),
                        "content": exc.content,
                        "usage": _usage_dict(exc.usage),
                        "model": exc.model or self._model_id,
                    },
                    _ms(started),
                )
            raise
        latency = _ms(started)
        self._cassette.put(
            "llm",
            key,
            {
                "output": result.output.model_dump(mode="json"),
                "usage": _usage_dict(result.usage),
                "model": result.model,
            },
            latency,
        )
        self._note(request, result, kind, cached=False, latency=latency, started=started)
        return result

    async def edit_for_readers(self, payload: ReaderEditInput) -> LLMResult[ReaderEditOutput]:
        """The reader edit's per-paragraph calls, recorded and replayable.

        Keyed by the prompt, the grammar and the whole input (the article so
        far included), so an edit to the prompt or to an earlier paragraph
        re-runs the call rather than replaying an answer to a different request.
        A miss in replay mode raises, and the stage keeps that paragraph as it
        was, as it would for any failed call.
        """
        request = {
            "paragraph_words": len(payload.paragraph.split()),
            "earlier": len(payload.earlier),
        }
        started = time.monotonic()
        key = stable_key(
            "reader_edit",
            self._model_id,
            READER_EDIT_SYSTEM_PROMPT,
            _flat_schema(ReaderEditOutput),
            payload.model_dump(mode="json"),
        )
        hit = self._lookup(key)
        if hit is not None:
            stored, latency = hit
            result = LLMResult(
                output=ReaderEditOutput.model_validate(stored["output"]),
                usage=TokenUsage(**stored["usage"]),
                model=stored["model"],
            )
            self._edit_note(request, result, cached=True, latency=latency, started=started)
            return result
        if self._mode == "replay":
            self._recorder.record(
                tool="llm_reader_edit",
                operation="edit_for_readers",
                request=request,
                status="cassette_miss",
            )
            raise LLMError("cassette miss (replay mode)")
        if self._inner is None:
            raise LLMError("no synthesis client is configured")
        try:
            result = await self._inner.edit_for_readers(payload)
        except LLMError as exc:
            self._recorder.record(
                tool="llm_reader_edit",
                operation="edit_for_readers",
                request=request,
                status="error",
                error=str(exc),
                latency_ms=_ms(started),
                live_latency_ms=_ms(started),
                model=exc.model or self._model_id,
                usage=_usage_dict(exc.usage),
            )
            raise
        latency = _ms(started)
        self._cassette.put(
            "llm",
            key,
            {
                "output": result.output.model_dump(mode="json"),
                "usage": _usage_dict(result.usage),
                "model": result.model,
            },
            latency,
        )
        self._edit_note(request, result, cached=False, latency=latency, started=started)
        return result

    def _edit_note(
        self,
        request: dict[str, Any],
        result: LLMResult[ReaderEditOutput],
        *,
        cached: bool,
        latency: float | None,
        started: float,
    ) -> None:
        self._recorder.record(
            tool="llm_reader_edit",
            operation="edit_for_readers",
            request=request,
            cached=cached,
            latency_ms=_ms(started),
            live_latency_ms=latency,
            model=result.model,
            usage=_usage_dict(result.usage),
        )

    def _note(
        self,
        request: dict[str, Any],
        result: LLMResult[SynthesisOutput],
        kind: str,
        *,
        cached: bool,
        latency: float | None,
        started: float,
    ) -> None:
        self._recorder.record(
            tool="llm_synthesis",
            operation="synthesize",
            request=request,
            cached=cached,
            latency_ms=_ms(started),
            live_latency_ms=latency,
            model=result.model,
            usage=_usage_dict(result.usage),
        )
        self._recorder.trace.drafts.append(
            {"kind": kind, "output": result.output.model_dump(mode="json")}
        )


class EvalAppraisalClient(_Recorded):
    """APPRAISE's calls, in either mode, recorded and replayable like the other
    model calls.

    Each output keeps what production drops once the label is read — the
    quotes, or the relevance call's ``outcome_measured`` and the direction
    call's ``finding``. When a label is wrong, those say whether the model
    misread the abstract or misapplied the label.
    """

    def __init__(self, inner: AppraisalClient | None, *args: Any) -> None:
        super().__init__(*args)
        self._inner = inner

    async def appraise(self, payload: AppraisalInput) -> LLMResult[AppraisalOutput]:
        return await self._recorded(
            "appraise",
            payload,
            AppraisalOutput,
            lambda inner: inner.appraise(payload),
        )

    async def relevance(self, payload: AppraisalInput) -> LLMResult[RelevanceOutput]:
        return await self._recorded(
            "relevance",
            payload,
            RelevanceOutput,
            lambda inner: inner.relevance(payload),
        )

    async def direction(self, payload: AppraisalInput) -> LLMResult[DirectionOutput]:
        return await self._recorded(
            "direction",
            payload,
            DirectionOutput,
            lambda inner: inner.direction(payload),
        )

    async def _recorded(
        self,
        operation: str,
        payload: AppraisalInput,
        output_type: type[Any],
        call: Any,
    ) -> LLMResult[Any]:
        request = {
            "claim": payload.claim,
            "handles": [source.handle for source in payload.sources],
        }
        started = time.monotonic()
        if self._fault is not None:
            self._recorder.record(
                tool="llm_appraisal",
                operation=operation,
                request=request,
                status="fault",
                fault=self._fault.kind,
                error="simulated appraisal failure",
                model=self._model_id,
            )
            raise LLMError(
                f"appraisal {operation} call failed: simulated outage (eval fault)",
                retryable=True,
            )

        # The prompt is part of the key, unlike synthesis's: these prompts are
        # being tuned against the eval, and a key without them would replay the
        # old prompt's answers under the new one with nothing to say so.
        key = stable_key(
            f"appraise_{operation}",
            self._model_id,
            _APPRAISAL_PROMPTS[operation],
            payload.model_dump(mode="json"),
        )
        hit = self._lookup(key)
        if hit is not None:
            stored, latency = hit
            result = LLMResult(
                output=output_type.model_validate(stored["output"]),
                usage=TokenUsage(**stored["usage"]),
                model=stored["model"],
            )
            self._note(
                operation, request, result, cached=True, latency=latency, started=started
            )
            return result
        if self._mode == "replay":
            self._recorder.record(
                tool="llm_appraisal",
                operation=operation,
                request=request,
                status="cassette_miss",
            )
            raise LLMError("cassette miss (replay mode)")
        if self._inner is None:
            raise LLMError("no appraisal client is configured")
        try:
            result = await call(self._inner)
        except LLMError as exc:
            self._recorder.record(
                tool="llm_appraisal",
                operation=operation,
                request=request,
                status="error",
                error=str(exc),
                latency_ms=_ms(started),
                live_latency_ms=_ms(started),
                model=exc.model or self._model_id,
                usage=_usage_dict(exc.usage),
            )
            raise
        latency = _ms(started)
        self._cassette.put(
            "llm",
            key,
            {
                "output": result.output.model_dump(mode="json"),
                "usage": _usage_dict(result.usage),
                "model": result.model,
            },
            latency,
        )
        self._note(operation, request, result, cached=False, latency=latency, started=started)
        return result

    def _note(
        self,
        operation: str,
        request: dict[str, Any],
        result: LLMResult[Any],
        *,
        cached: bool,
        latency: float | None,
        started: float,
    ) -> None:
        self._recorder.record(
            tool="llm_appraisal",
            operation=operation,
            request=request,
            cached=cached,
            latency_ms=_ms(started),
            live_latency_ms=latency,
            model=result.model,
            usage=_usage_dict(result.usage),
        )
        self._recorder.trace.appraisals.append(
            {
                "claim": request["claim"],
                "call": operation,
                "output": result.output.model_dump(mode="json"),
            }
        )


def _first_sentence(abstract: str) -> str:
    """The abstract's first 25 words as one sentence.

    Sentence-ending punctuation inside them is dropped, so the lifted text is
    always exactly one sentence — "e.g." in an abstract would otherwise count
    as a sentence break and push the beat over its five-sentence ceiling. Only
    a mark followed by whitespace is dropped, so "2.5 mg" stays "2.5 mg".
    Square brackets are made round, since in the body they mean citations.
    """
    text = abstract.translate(str.maketrans("[]", "()")).strip()
    # Structured abstracts open with a label ("BACKGROUND: ..."); skip it.
    text = re.sub(r"^[A-Z][A-Z /&]{2,30}:\s*", "", text)
    return " ".join(re.sub(r"[.!?](?=\s|$)", "", text).split()[:25])


class ScriptedSynthesisClient:
    """A deterministic writer for control-flow cases. Grounded by construction:
    every sentence is lifted from the source it cites."""

    def __init__(self, recorder: TraceRecorder) -> None:
        self._recorder = recorder

    async def synthesize(
        self, payload: SynthesisInput, *, feedback: str | None = None
    ) -> LLMResult[SynthesisOutput]:
        sources = payload.sources[:3]
        evidence = " ".join(
            f"{_first_sentence(source.abstract)} [{source.handle}]." for source in sources
        )
        product = " ".join(payload.product.split()[:6])
        output = SynthesisOutput(
            headline=f"What studies say about {product}",
            verdict=Verdict.WEAK,
            summary="A few studies were found. They are summarised without a firm conclusion.",
            body=ArticleBody(
                beat_1_claim=f"The claim under review: {'; '.join(payload.target_claims[:2])}.",
                beat_2_evidence=evidence or "No study addressed the claim directly.",
                beat_3_bottom_line="The evidence summarised here is limited.",
            ),
            citations=[
                CitationGroup(
                    claim=payload.target_claims[0],
                    source_ids=[source.handle for source in sources],
                )
            ]
            if sources
            else [],
        )
        self._recorder.record(
            tool="llm_synthesis",
            operation="synthesize",
            request={"scripted": True, "feedback": feedback_kind(feedback)},
            model="eval/scripted",
        )
        self._recorder.trace.drafts.append(
            {"kind": feedback_kind(feedback), "output": output.model_dump(mode="json")}
        )
        return LLMResult(output=output, usage=TokenUsage(), model="eval/scripted")

    async def edit_for_readers(self, payload: ReaderEditInput) -> LLMResult[ReaderEditOutput]:
        """Returns the paragraph unchanged: control-flow cases test no prose."""
        output = ReaderEditOutput(sentences=[payload.paragraph])
        return LLMResult(output=output, usage=TokenUsage(), model="eval/scripted")


class HallucinatingSynthesisClient:
    """The adversary: an invented handle, an invented number, an inflated verdict."""

    def __init__(self, recorder: TraceRecorder) -> None:
        self._recorder = recorder

    async def synthesize(
        self, payload: SynthesisInput, *, feedback: str | None = None
    ) -> LLMResult[SynthesisOutput]:
        real = payload.sources[0].handle if payload.sources else "S1"
        ghost = f"S{len(payload.sources) + 90}"
        output = SynthesisOutput(
            headline="Landmark trial proves the claim beyond doubt",
            verdict=Verdict.SUPPORTED,
            summary="A definitive trial settles the question. The effect is large and certain.",
            body=ArticleBody(
                beat_1_claim="The product is claimed to work.",
                beat_2_evidence=(
                    f"A trial of 4,812 adults found an 87% improvement [{real}][{ghost}]. "
                    f"A second analysis confirmed a 91% response rate [{ghost}]."
                ),
                beat_3_bottom_line="The question is settled.",
            ),
            citations=[CitationGroup(claim="it works", source_ids=[real, ghost])],
        )
        self._recorder.record(
            tool="llm_synthesis",
            operation="synthesize",
            request={"adversarial": True, "feedback": feedback_kind(feedback)},
            model="eval/hallucinating",
        )
        self._recorder.trace.drafts.append(
            {"kind": feedback_kind(feedback), "output": output.model_dump(mode="json")}
        )
        return LLMResult(output=output, usage=TokenUsage(), model="eval/hallucinating")

    async def edit_for_readers(self, payload: ReaderEditInput) -> LLMResult[ReaderEditOutput]:
        """Unchanged: the adversary's draft must reach VALIDATE as written."""
        output = ReaderEditOutput(sentences=[payload.paragraph])
        return LLMResult(output=output, usage=TokenUsage(), model="eval/hallucinating")


def _ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)
