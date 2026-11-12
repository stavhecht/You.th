"""VERIFY: does each cited paper say what the article says it says.

A model reads one cited paper (title, abstract, any full-text excerpts) against
the article sentences that cite it, and says per sentence: supported, partly,
or not_supported, quoting the paper's own sentence first. Each sentence it does
not find supported becomes a warning on the validation report, which the desk
lists under "Need to be checked".

The lexical checks in VALIDATE (shared words, numbers present, the best-source
check) are blind to a sentence built from a paper's words that claims what the
paper does not. This reads meaning, so it can catch that; it is also a model,
so it is wrong sometimes. Hence, like APPRAISE:

* **It never fails a run and never blocks publishing.** Switched off, out of
  sources, or broken, the draft goes on exactly as before, with a ``cause``.
* **It does not believe a "supported" it cannot anchor.** A quote that is not
  in the paper means the model may be judging from memory; that reading is
  reported as ``partly`` (``unanchored`` in the metrics), so a reviewer looks.

Its own stage rather than part of VALIDATE: VALIDATE queries the database, and
the orchestrator commits between stages, so the model calls here run with no
transaction open. It sees only drafts that passed, which reach it once.
Unmeasured against hand labels as of 2026-11-12.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

from app.domain.contracts import (
    PromptSource,
    SupportInput,
    SynthesisOutput,
    ValidationFailure,
    extract_handles,
)
from app.evidence import numbers
from app.llm.base import AppraisalClient, LLMError
from app.pipeline.stages import PipelineContext, StageName

logger = logging.getLogger(__name__)

#: A paper cited by more sentences than this is checked on its first ones: the
#: prompt stays short enough for a small model, and the call bounded.
MAX_SENTENCES_PER_SOURCE = 12

#: Papers read per run, the most-cited first. The calls are sequential, about
#: 50-120 s each on Bonsai, on a worker that runs one job at a time: uncapped,
#: an article citing 24 papers added ~40 minutes and held up the queue. The
#: rest are named in ``metrics.verify.unchecked_sources``, never passed over
#: in silence.
MAX_SOURCES = 12

_SPACE = re.compile(r"\s+")
_SAYS = {"not_supported": "does not say", "partly": "says only part of"}
#: The space a removed marker leaves before punctuation ("fell ." -> "fell.").
_ORPHANED_SPACE = re.compile(r"\s+([.,;:!?])")


def cited_sentences(draft: SynthesisOutput) -> dict[str, list[str]]:
    """Handle -> the plain sentences citing it, in reading order, each once.

    Only the places findings live (beat 2 and the sections), as the other
    citation checks read them; beats 1 and 3 carry no findings.
    """
    body = draft.body
    by_handle: dict[str, list[str]] = {}
    for block in [body.beat_2_evidence, *(section.body for section in body.sections)]:
        for sentence in numbers.sentences(block):
            # Tidied for the model and the reviewer only; the shared
            # `plain_sentence` form is a lookup key elsewhere and stays as is.
            plain = _ORPHANED_SPACE.sub(r"\1", numbers.plain_sentence(sentence))
            for handle in dict.fromkeys(extract_handles(sentence)):
                listed = by_handle.setdefault(handle, [])
                if plain not in listed:
                    listed.append(plain)
    return by_handle


def _normalise(text: str) -> str:
    return _SPACE.sub(" ", text).strip().lower().rstrip(".")


class VerifyStage:
    name = StageName.VERIFY

    def __init__(self, client: AppraisalClient | None) -> None:
        """``client`` is None when ``SUPPORT_CHECK_ENABLED=false``; the stage is
        still in the list, so its absence is a recorded cause."""
        self._client = client

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        metrics: dict[str, Any] = {"cause": None}
        warnings: list[ValidationFailure] = []
        try:
            await self._verify(ctx, warnings, metrics)
        except Exception as exc:
            # Broad on purpose, as in APPRAISE: a bug here must not fail every
            # run for a reading nothing depends on.
            logger.exception("support check raised unexpectedly for run %s", ctx.run_id)
            metrics.update(cause="unexpected", detail=f"{type(exc).__name__}: {exc}")
        ctx.record_metrics(self.name, metrics)
        if not warnings or ctx.validation is None:
            return ctx
        report = ctx.validation.model_copy(
            update={"warnings": [*ctx.validation.warnings, *warnings]}
        )
        return ctx.model_copy(update={"validation": report})

    async def _verify(
        self, ctx: PipelineContext, warnings: list[ValidationFailure], metrics: dict[str, Any]
    ) -> None:
        if self._client is None:
            metrics["cause"] = "disabled"
            return
        if ctx.validation is None or not ctx.validation.passed:
            metrics["cause"] = "not_passed"
            return
        if ctx.draft is None or ctx.synthesis_input is None:
            metrics["cause"] = "no_draft"
            return

        sources = {source.handle: source for source in ctx.synthesis_input.sources}
        by_handle = {
            handle: sentences
            for handle, sentences in cited_sentences(ctx.draft).items()
            if handle in sources
        }
        if not by_handle:
            metrics["cause"] = "nothing_cited"
            return

        # Most-cited first; ties keep reading order (sorted is stable).
        ranked = sorted(by_handle, key=lambda handle: -len(by_handle[handle]))
        checked, unchecked = ranked[:MAX_SOURCES], ranked[MAX_SOURCES:]
        metrics["unchecked_sources"] = unchecked

        counts: Counter[str] = Counter()
        errors: list[str] = []
        for handle in checked:
            sentences = by_handle[handle]
            await self._check_source(
                ctx, sources[handle], sentences[:MAX_SENTENCES_PER_SOURCE],
                warnings, counts, errors,
            )

        if counts["failed_calls"] and counts["failed_calls"] == counts["calls"]:
            metrics["cause"] = "model_error"
        elif counts["failed_calls"]:
            metrics["cause"] = "partial_model_error"
        metrics.update(
            calls=counts["calls"],
            failed_calls=counts["failed_calls"],
            sentences=counts["sentences"],
            supported=counts["supported"],
            partly=counts["partly"],
            not_supported=counts["not_supported"],
            unanchored=counts["unanchored"],
            missing=counts["missing"],
        )
        if errors:
            metrics["detail"] = "; ".join(errors)[:1000]

    async def _check_source(
        self,
        ctx: PipelineContext,
        source: PromptSource,
        sentences: list[str],
        warnings: list[ValidationFailure],
        counts: Counter[str],
        errors: list[str],
    ) -> None:
        assert self._client is not None
        paper = numbers.source_text(source)
        counts["calls"] += 1
        counts["sentences"] += len(sentences)
        try:
            result = await self._client.support(
                SupportInput(
                    handle=source.handle,
                    title=source.title,
                    abstract=paper,
                    sentences=sentences,
                )
            )
        except LLMError as exc:
            # Billed like a success, so the tokens are kept.
            ctx.record_usage(self.name, exc.model, exc.usage)
            logger.warning("support check failed for %s: %s", source.handle, exc)
            counts["failed_calls"] += 1
            errors.append(str(exc))
            return
        ctx.record_usage(self.name, result.model, result.usage)

        read: set[int] = set()
        haystack = _normalise(paper)
        for item in result.output.items:
            if not 1 <= item.sentence <= len(sentences) or item.sentence in read:
                continue  # invented or repeated: never guessed onto a sentence
            read.add(item.sentence)
            verdict = item.verdict
            quote = item.quote.strip()
            anchored = quote.lower() != "none" and _normalise(quote) in haystack
            if verdict == "supported" and not anchored:
                counts["unanchored"] += 1
                verdict = "partly"
            counts[verdict] += 1
            if verdict == "supported":
                continue
            sentence = sentences[item.sentence - 1]
            said = (
                f" The closest it says: “{quote[:200]}”"
                if anchored
                else " It quotes nothing in the paper that says so."
            )
            warnings.append(
                ValidationFailure(
                    code=(
                        "citation_not_supported"
                        if verdict == "not_supported"
                        else "citation_partly_supported"
                    ),
                    message=f"{source.handle} {_SAYS[verdict]} “{sentence[:200]}”.{said}",
                    detail={"handle": source.handle, "sentence": sentence, "quote": quote},
                )
            )
        # A sentence the model skipped is not read, never "supported".
        counts["missing"] += len(sentences) - len(read)
