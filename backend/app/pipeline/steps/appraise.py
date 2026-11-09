"""Stage 5 — label which way each ranked source points, per claim.

The verdict scale measures support, and the verdict cap counts a cited source
toward a claim by what it was *retrieved for*, never by what it *found*. So a
trial that tested a claim and found nothing looked, to every check downstream,
exactly like one that bore it out. Retrieval already brings such trials back —
queries are ``<substance> AND <outcome>`` with the directional verbs dropped —
and until this stage nothing recorded which way they pointed.

Each (source, claim) pair gets a ``Stance`` on ``ctx.stances``, under the same
S-handles RANK assigned. SYNTHESIZE copies them onto the prompt payload for
VALIDATE to read, and PERSIST writes them to ``article_sources.stance``.

**Two modes, chosen by ``APPRAISAL_MODE``; both put relevance before direction.**

* ``one_call`` (default): one call per claim. The model names the claim's
  outcome, quotes each abstract's sentence about it (or "none"), and labels the
  quote; ``label`` stores ``off_topic`` where there is no quote.
* ``two_call``: a relevance call over every source, then a direction call shown
  only the sources it accepted, so a study of something else is never asked for
  a direction. A claim with nothing relevant skips the second call.

Measured 2026-10-02 on llama3.1:8b over 132 blind-labelled pairs, the two were
level on flips (2 each, on different studies) and ``one_call`` was slightly
ahead on direction (79% against 78%) and on off-topic studies caught (75%
against 69%) at one call instead of two — the full table is in app/pipeline/CLAUDE.md. Both
are kept because the winner is a fact about a model, not about the task: a
larger or hosted model may reverse it, and re-measuring is a setting away.

**The synthesis model is not shown these labels**, deliberately: two
independent readings of the same abstracts are what make a disagreement
visible. Today a label changes nothing but a reviewer warning; whether it ever
gates a verdict is decided from what the evaluation measures of its accuracy.

**This stage never fails a run**, like FULL_TEXT and ILLUSTRATE. Without labels
the article is drafted and validated exactly as before the stage existed, and
``cause`` in the metrics says why there were none. A pair with no label is *not
appraised* — never ``unclear``, which is an answer the model gave.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

from app.domain.contracts import (
    AppraisalInput,
    AppraisalItem,
    AppraisalSource,
    RankedSource,
)
from app.domain.enums import Stance
from app.evidence.grading import AGAINST_STANCES
from app.llm.base import AppraisalClient, LLMError
from app.pipeline.stages import PipelineContext, StageName

logger = logging.getLogger(__name__)


def label(item: AppraisalItem, claim_outcome: str = "") -> Stance:
    """The stance stored for one item. Two rules can overrule the model, and
    both only ever move a label to ``off_topic``:

    * **No quote, no label.** ``off_topic`` whenever the abstract has no
      sentence reporting the claim's outcome, whatever direction the model
      gave. A direction on an off-topic study was the error measured most often
      (2026-10-02, llama3.1:8b: asked for a direction alone, only 23% of
      off-topic studies were called off topic). A quote of "none" counts as no
      quote even when the boolean beside it says otherwise.
    * **A refutation must be about the outcome.** A ``no_effect`` or
      ``contradicts`` label whose quote shares no word with ``claim_outcome`` is
      stored ``off_topic``. Measured on the same day, the commonest directional
      error left was a made-up refutation — 11 of 131 labels — where the model
      quoted a sentence about something else ("increases in strength", for a
      claim about endurance) and called it no effect. An "against" label is the
      one a ``not_supported`` verdict would be built from, so it is the one held
      to the stricter test; ``supports`` is not, because synonyms ("TG" for
      triglycerides) would turn real support into a false off-topic call.

    Measured over the same 132 pairs: made-up refutations 11 -> 6 and flipped
    directions 2 -> 0, at a price — real refutations missed 9 -> 16, because a
    quote like "the pooled RR was 0.96" names no outcome. On an 18-case run it
    removed one of the two false "against" warnings and kept the real catch. A
    prompt rule against the same error ("silence is not a result") was tried
    first and did nothing (12 made-up refutations), so the rule lives here.

    It is a word test, not an understanding of the sentence: a quote that shares
    a word but answers a different question ("muscle strength" against "muscle
    gain") passes — the other false warning was exactly that. It errs toward
    losing a direction, which is the safe way to be wrong here.
    """
    if _no_result(item):
        return Stance.OFF_TOPIC
    if item.stance in AGAINST_STANCES and _mentions_outcome(item.quote, claim_outcome) is False:
        return Stance.OFF_TOPIC
    return item.stance


def _no_result(item: AppraisalItem) -> bool:
    return not item.reports_claim_outcome or item.quote.strip().strip(".").lower() in (
        "",
        "none",
    )


#: Words that say how an outcome is counted rather than what it is. Left in,
#: "how often colds happen" would match any quote containing "often".
_OUTCOME_FILLER = frozenset(
    {
        "and", "the", "for", "with", "from", "into", "their", "its",
        "how", "often", "much", "many", "people", "person", "happen", "happens",
        "level", "levels", "risk", "risks", "rate", "rates", "score", "scores",
        "test", "tests", "amount", "number", "chance", "likelihood", "measure",
        "measures", "change", "changes", "overall", "general",
    }
)  # fmt: skip
_WORD_RE = re.compile(r"[a-z]{3,}")


def _mentions_outcome(quote: str, claim_outcome: str) -> bool | None:
    """Whether the quote shares a word with the claim's outcome, up to a suffix
    ("cold"/"colds", "latency"/"latencies"). None when the outcome has no word
    left to check once the filler is gone — then there is nothing to test."""
    outcome = {w for w in _WORD_RE.findall(claim_outcome.lower()) if w not in _OUTCOME_FILLER}
    if not outcome:
        return None
    quoted = {w for w in _WORD_RE.findall(quote.lower()) if w not in _OUTCOME_FILLER}
    return any(_same_stem(a, b) for a in outcome for b in quoted)


def _same_stem(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return (len(short) >= 4 and long.startswith(short)) or (
        len(a) >= 5 and len(b) >= 5 and a[:5] == b[:5]
    )


class AppraiseStage:
    name = StageName.APPRAISE

    def __init__(self, client: AppraisalClient | None, *, two_call: bool = False) -> None:
        """``client`` is None when appraisal is switched off
        (``APPRAISAL_ENABLED=false``). The stage is still constructed, so its
        absence is a recorded ``cause`` and not a gap in the stage list.
        ``two_call`` selects the relevance-then-direction mode."""
        self._client = client
        self._two_call = two_call

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        metrics: dict[str, Any] = {"cause": None}
        # Filled in place, so the claims labelled before an unexpected error
        # are kept rather than thrown away with it.
        stances: dict[str, dict[str, Stance]] = {}
        try:
            await self._appraise(ctx, stances, metrics)
        except Exception as exc:
            # Broad on purpose, as in FullTextStage: a bug here must not fail
            # every run for a label nothing yet depends on.
            logger.exception("appraisal raised unexpectedly for run %s", ctx.run_id)
            metrics.update(cause="unexpected", detail=f"{type(exc).__name__}: {exc}")

        # A claim whose calls all failed leaves an empty entry behind.
        stances = {claim: labels for claim, labels in stances.items() if labels}
        by_stance = Counter(
            str(stance) for labels in stances.values() for stance in labels.values()
        )
        metrics.update(labelled=sum(by_stance.values()), by_stance=dict(by_stance))
        ctx.record_metrics(self.name, metrics)
        return ctx.model_copy(update={"stances": stances})

    async def _appraise(
        self,
        ctx: PipelineContext,
        stances: dict[str, dict[str, Stance]],
        metrics: dict[str, Any],
    ) -> None:
        metrics["mode"] = "two_call" if self._two_call else "one_call"
        if self._client is None:
            metrics["cause"] = "disabled"
            return
        claims = {claim: entries for claim, entries in ctx.ranked.items() if entries}
        if not claims:
            metrics["cause"] = "no_sources"
            return

        product = ctx.extraction.product if ctx.extraction is not None else ctx.topic
        counts: Counter[str] = Counter()
        errors: list[str] = []
        per_claim: dict[str, dict[str, int]] = {}

        for claim, entries in claims.items():
            by_handle = {entry.citation_handle: entry for entry in entries}
            counts["offered"] += len(by_handle)
            labels: dict[str, Stance] = {}
            stances[claim] = labels
            label_claim = self._two_calls if self._two_call else self._one_call
            await label_claim(
                ctx, self._client, product, claim, by_handle, labels, counts, errors
            )
            counts["missing"] += len(by_handle) - len(labels)
            per_claim[claim] = dict(Counter(str(stance) for stance in labels.values()))

        if counts["failed_calls"] and counts["failed_calls"] == counts["calls"]:
            metrics["cause"] = "model_error"
        elif counts["failed_calls"]:
            metrics["cause"] = "partial_model_error"
        metrics.update(
            claims=len(claims),
            calls=counts["calls"],
            offered=counts["offered"],
            missing=counts["missing"],
            invented=counts["invented"],
            duplicates=counts["duplicates"],
            failed_calls=counts["failed_calls"],
            per_claim=per_claim,
        )
        if self._two_call:
            # Sources the relevance call rejected, which the direction call
            # never saw, and claims where nothing was relevant at all.
            metrics.update(
                ruled_off_topic=counts["ruled_off_topic"],
                direction_skipped=counts["direction_skipped"],
            )
        else:
            # Directions given to studies the model itself found no result in,
            # and refutations whose quote never mentions the claim's outcome.
            metrics["relevance_overrides"] = counts["relevance_overrides"]
            metrics["outcome_guard_overrides"] = counts["outcome_guard_overrides"]
        if errors:
            metrics["detail"] = "; ".join(errors)[:1000]

    async def _one_call(
        self,
        ctx: PipelineContext,
        client: AppraisalClient,
        product: str,
        claim: str,
        by_handle: dict[str, RankedSource],
        labels: dict[str, Stance],
        counts: Counter[str],
        errors: list[str],
    ) -> None:
        counts["calls"] += 1
        try:
            result = await client.appraise(_payload(product, claim, by_handle))
        except LLMError as exc:
            _failed(self.name, ctx, claim, exc, counts, errors)
            return
        ctx.record_usage(self.name, result.model, result.usage)

        for item in result.output.items:
            entry = by_handle.get(item.handle)
            if entry is None:
                # A handle this claim was never offered. Dropped, never
                # guessed onto a source: the label is about a paper the model
                # may not have been shown.
                counts["invented"] += 1
            elif entry.source_id in labels:
                # First answer wins; a second is the model repeating itself.
                counts["duplicates"] += 1
            else:
                stored = label(item, result.output.claim_outcome)
                if stored is not item.stance and _no_result(item):
                    counts["relevance_overrides"] += 1
                elif stored is not item.stance:
                    counts["outcome_guard_overrides"] += 1
                labels[entry.source_id] = stored

    async def _two_calls(
        self,
        ctx: PipelineContext,
        client: AppraisalClient,
        product: str,
        claim: str,
        by_handle: dict[str, RankedSource],
        labels: dict[str, Stance],
        counts: Counter[str],
        errors: list[str],
    ) -> None:
        # Call 1: which sources measured the claim's outcome at all.
        counts["calls"] += 1
        try:
            relevance = await client.relevance(_payload(product, claim, by_handle))
        except LLMError as exc:
            _failed(self.name, ctx, claim, exc, counts, errors)
            return
        ctx.record_usage(self.name, relevance.model, relevance.usage)

        relevant: dict[str, RankedSource] = {}
        for rel in relevance.output.items:
            entry = by_handle.get(rel.handle)
            if entry is None:
                counts["invented"] += 1
            elif entry.source_id in labels or rel.handle in relevant:
                counts["duplicates"] += 1  # first answer wins
            elif rel.measures_claim_outcome:
                relevant[rel.handle] = entry
            else:
                labels[entry.source_id] = Stance.OFF_TOPIC
        counts["ruled_off_topic"] += len(labels)

        # Call 2: which way the relevant ones point — and only those, so a
        # study of something else is never asked for a direction.
        if not relevant:
            counts["direction_skipped"] += 1
            return
        counts["calls"] += 1
        try:
            direction = await client.direction(_payload(product, claim, relevant))
        except LLMError as exc:
            # The off-topic rulings came from a call that succeeded and stand.
            _failed(self.name, ctx, claim, exc, counts, errors)
            return
        ctx.record_usage(self.name, direction.model, direction.usage)
        for item in direction.output.items:
            entry = relevant.get(item.handle)
            if entry is None:
                # Includes a handle the relevance call rejected: it was not
                # shown, so a direction for it is noise.
                counts["invented"] += 1
            elif entry.source_id in labels:
                counts["duplicates"] += 1
            else:
                labels[entry.source_id] = item.stance


def _failed(
    stage: StageName,
    ctx: PipelineContext,
    claim: str,
    exc: LLMError,
    counts: Counter[str],
    errors: list[str],
) -> None:
    # Recorded before anything else: a call that failed after the provider
    # answered is billed like a success.
    ctx.record_usage(stage, exc.model, exc.usage)
    logger.warning("appraisal call failed for claim %r: %s", claim, exc)
    counts["failed_calls"] += 1
    errors.append(str(exc))


def _payload(product: str, claim: str, by_handle: dict[str, RankedSource]) -> AppraisalInput:
    return AppraisalInput(
        product=product,
        claim=claim,
        sources=[
            AppraisalSource(
                handle=handle,
                title=entry.paper.title,
                abstract=entry.paper.abstract,
                study_type=entry.paper.study_type,
            )
            for handle, entry in by_handle.items()
        ],
    )
