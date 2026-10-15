"""Stage 4 — the RAG generation (LLM call 2)."""

from __future__ import annotations

import logging

from app.domain.contracts import (
    CITATION_RUN_RE,
    PromptSource,
    ReaderEditInput,
    SynthesisInput,
    SynthesisOutput,
    ValidationReport,
    extract_handles,
)
from app.domain.enums import Stance
from app.llm.base import LLMError, LLMResult, SynthesisClient
from app.pipeline.stages import PipelineContext, StageError, StageName
from app.pipeline.steps import reader_edit
from app.services.no_evidence import no_evidence_draft

logger = logging.getLogger(__name__)

#: Fraction of the offered sources a draft must give a citation of their own
#: (see ``MAX_HANDLES_PER_CITATION``) before it is left alone. Below this it is
#: re-prompted once, naming the handles it skipped.
#:
#: **This is not a length rule, and it deliberately stopped being one.** It was
#: briefly a word-count floor aimed at a three-minute read, which was the wrong
#: target twice over: it fired on essentially every run because no local model
#: this machine can hold writes 700 words, and length was never the defect
#: anyway. The defect is a draft that was handed 24 systematic reviews and wrote
#: about three of them — measured, repeatedly — and the one re-prompt that
#: clearly worked took a draft from 3 cited sources to 24.
#:
#: Aiming at coverage instead keeps DESIGN.md §6 intact rather than bending it:
#: there is no floor on how long an article may be, and a thoroughly-cited short
#: article is now a correct output that is never re-prompted. It also restates a
#: rule the synthesis prompt already gives — "every source bearing on a claim
#: deserves a sentence" — rather than adding a competing one.
#:
#: Raised 0.5 -> 0.75 on 2026-09-29, together with counting own citations
#: rather than handles. Against the twelve articles in the database the old
#: rule let two first drafts through: one citing exactly half its sources, and
#: one that "cited" 22 of 24 by putting 19 handles in one bracket, in 109
#: words. The new rule re-prompts all twelve, so expect it to fire on most runs
#: with ``SECTIONS_EXPECTED_FROM`` or more sources. Each firing is one more
#: synthesis call: free locally, and recorded in the token ledger when hosted.
MIN_CITED_FRACTION = 0.75

#: Most handles one citation may carry and still count toward coverage. A
#: citation is one chip on the page, a run of adjacent markers split by the
#: same ``CITATION_RUN_RE`` the renderer uses, so ``[S1][S2][S3][S4]`` is one
#: citation of four, exactly as a reader sees it.
#:
#: Three because the synthesis prompt's own example of a shared citation is
#: ``[S1, S5, S8]``, and because that is where real drafts sit: of the 57
#: citations in the database on 2026-09-29, 54 carried one to three handles
#: and the other three carried 5, 5 and 19. A long list after one sentence says
#: nothing about what any of those sources found, which is the thing the
#: re-prompt exists to ask for.
#:
#: This decides only what the nudge counts. ``cited_handles()``, ``was_cited``
#: and validation still count every handle anywhere in the body.
MAX_HANDLES_PER_CITATION = 3

#: Sources below which a thinly-cited draft is left alone. The prompt already
#: says "six or more usable sources supports 3 to 5 sections"; reusing its number
#: keeps this a nudge toward a rule the model was given.
#:
#: This is the anti-padding guarantee. A draft citing one of two sources has
#: nothing more to say, and asking it for more can only invent it. Only a draft
#: sitting on plenty of unused evidence is worth a second ask.
SECTIONS_EXPECTED_FROM = 6


class SynthesizeStage:
    name = StageName.SYNTHESIZE

    def __init__(
        self,
        client: SynthesisClient,
        *,
        relevance_filter: bool = False,
        reader_edit: bool = False,
    ) -> None:
        self._client = client
        self._relevance_filter = relevance_filter
        self._reader_edit = reader_edit

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Write the article, grounded strictly in the ranked abstracts.

        The ``SynthesisInput`` built here is carried forward on the context
        unchanged, because VALIDATE needs the *exact* handle set rendered into
        the prompt. Rebuilding it in the validate stage would look equivalent
        and would quietly break invariant #2.

        This stage does not check its own output. Schema conformance is
        guaranteed by structured outputs; grounding is VALIDATE's job. Keeping
        them apart is what stops the generator from also being the approver.

        **Zero sources takes the template branch, not an error.** RETRIEVE
        deliberately lets an empty candidate set through so that a fringe trend
        can be reported honestly; failing here would discard that at the last
        step and turn the most useful thing this product can say into a
        ``draft_failed`` run. See ``services/no_evidence.py``.
        """
        if ctx.extraction is None:
            raise StageError(self.name, "extraction stage did not run")

        if _is_revision(ctx):
            return await self._revise(ctx)

        payload = self._build_input(ctx)
        filtered = None
        if self._relevance_filter:
            payload, filtered = _filter_by_relevance(payload)

        if not payload.sources:
            return self._no_evidence(ctx, payload)

        fresh_draft = False
        try:
            result = await self._client.synthesize(payload)
        except LLMError as exc:
            # Synthesis carries the pipeline's largest token budget, so a
            # failure here is the most expensive thing that can happen and the
            # one least worth reporting as free. See LLMError.usage.
            ctx.record_usage(self.name, exc.model, exc.usage)
            if exc.retryable:
                raise StageError(self.name, str(exc), retryable=True) from exc
            # The draft broke a field limit and the client's repair round did
            # not fix it — a nine-word section heading, a six-sentence beat.
            # That is a property of one sample, not of the request: synthesis
            # samples at temperature 0.4, and a fresh draft usually fits.
            # Measured 2026-10-02, it failed an otherwise complete run outright.
            # Exactly one more draft, never a loop; if it breaks the contract
            # too, the run fails as before.
            logger.warning("first draft broke the output contract; writing one fresh draft")
            fresh_draft = True
            try:
                result = await self._client.synthesize(payload)
            except LLMError as again:
                ctx.record_usage(self.name, again.model, again.usage)
                raise StageError(self.name, str(again), retryable=again.retryable) from again

        ctx.record_usage(self.name, result.model, result.usage)

        first_cited = len(result.output.body.cited_handles())
        first_covered = len(_covered_handles(result.output, payload.handle_set))
        if _is_thin(result.output, payload):
            retried = True
            result = await self._use_more_sources(ctx, payload, result)
        else:
            retried = False

        edit_metrics = None
        if self._reader_edit:
            result, edit_metrics = await self._edit_for_readers(ctx, payload, result)

        ctx.record_metrics(
            self.name,
            {
                "verdict": str(result.output.verdict),
                "sources_in_prompt": len(payload.sources),
                "coverage_retry": retried,
                "fresh_draft_after_contract_failure": fresh_draft,
                "first_draft_handles_cited": first_cited,
                # What the re-prompt gate counts: sources given a citation of
                # their own. Kept beside ``handles_cited`` because the gap
                # between the two is how a draft that lists its sources in one
                # long bracket shows up in the ledger.
                "first_draft_handles_covered": first_covered,
                # Body handles, matching ``was_cited`` in PERSIST. Counting
                # the union with the ``citations`` list reported sources the
                # article never refers to as cited, which is the one direction
                # this metric must not err in.
                "handles_cited": len(result.output.body.cited_handles()),
                "handles_covered": len(
                    _covered_handles(result.output, payload.handle_set)
                ),
                "handles_listed_not_written": len(
                    result.output.all_cited_handles()
                    - result.output.body.cited_handles()
                ),
                "body_words": _body_words(result.output),
                # None when the filter is off, so on/off is one query.
                "relevance_filter": filtered,
                # None when the edit is off; see ``reader_edit.py``.
                "reader_edit": edit_metrics,
            },
        )

        return ctx.model_copy(
            update={"synthesis_input": payload, "draft": result.output}
        )

    async def _revise(self, ctx: PipelineContext) -> PipelineContext:
        """Rewrite a draft that failed validation, with the failures fed back.

        Reached only through the graph's VALIDATE -> SYNTHESIZE edge, at most
        ``MAX_REVISE_ROUNDS`` times. The payload is the one the first draft was
        written from — reused, not rebuilt, for the reason ``synthesis_input``
        exists at all: VALIDATE must check against the exact handle set the
        model saw.

        **The rewrite is validated like any draft; it is never waved through.**
        If it fails again the article is stored ``validation_failed`` exactly as
        before this loop existed. And a rewrite that errors keeps the first
        draft, which then fails validation again and is stored the same way —
        a failed revision must not turn a reportable draft into a failed run.
        """
        assert ctx.synthesis_input is not None and ctx.validation is not None
        assert ctx.draft is not None
        payload = ctx.synthesis_input
        codes = sorted({failure.code for failure in ctx.validation.failures})
        try:
            result = await self._client.synthesize(
                payload, feedback=revision_feedback(ctx.validation, ctx.draft)
            )
        except LLMError as exc:
            ctx.record_usage(self.name, exc.model, exc.usage)
            logger.warning(
                "revision failed (%s); keeping the draft that failed validation", exc
            )
            ctx.record_metrics(
                self.name,
                {
                    "revision": True,
                    "revision_failed": str(exc),
                    "failures_fed_back": codes,
                    "verdict": str(ctx.draft.verdict),
                },
            )
            return ctx

        ctx.record_usage(self.name, result.model, result.usage)
        ctx.record_metrics(
            self.name,
            {
                "revision": True,
                "failures_fed_back": codes,
                "verdict": str(result.output.verdict),
                "sources_in_prompt": len(payload.sources),
                "handles_cited": len(result.output.body.cited_handles()),
                "handles_covered": len(_covered_handles(result.output, payload.handle_set)),
                "body_words": _body_words(result.output),
            },
        )
        logger.info("revised a draft that failed validation (%s)", ", ".join(codes))
        return ctx.model_copy(update={"draft": result.output, "validation": None})

    async def _use_more_sources(
        self,
        ctx: PipelineContext,
        payload: SynthesisInput,
        result: LLMResult[SynthesisOutput],
    ) -> LLMResult[SynthesisOutput]:
        """Ask once for the skipped sources, and keep the first draft if refused.

        **This can only improve the outcome, never fail the run.** A draft is
        already in hand and it is publishable — it passed the same contract the
        second one has to. So a re-prompt that raises is logged and discarded
        rather than propagated: turning a usable article into a failed run would
        be strictly worse than shipping it, and would hand this nudge a veto over
        the pipeline that it has no business having. Its tokens are still
        recorded, because they were still spent.

        **The second draft is kept only if it gives more sources a citation of
        their own than the first**, which is the same question the re-prompt
        asked. Keeping it unconditionally was tried and is wrong: live, a draft
        citing 3 of 13 came back citing 1, and separately a 152-word ``mixed``
        draft came back at ``no_evidence`` while citing everything. Nothing
        downstream catches the latter — ``no_evidence`` is exempt from the
        cited-beat rule and sits below every ceiling, so it validates cleanly
        and publishes. Comparing raw handle counts instead would prefer a second
        draft that lists every source in one bracket, which is exactly what
        ``MAX_HANDLES_PER_CITATION`` refuses to count.

        Note the test is coverage, not length. A second draft that says more
        about fewer papers is not what was asked for, and a shorter draft that
        genuinely works through more of the evidence is.
        """
        offered = payload.handle_set
        covered_set = _covered_handles(result.output, offered)
        covered = len(covered_set)
        cited = result.output.body.cited_handles()
        unused = sorted(offered - covered_set, key=lambda handle: int(handle[1:]))
        # Cited, but only inside a long list. Named apart from the never-cited
        # ones: told they were unused, a model that can see its own
        # "[S2, S3, … S24]" has been told something false.
        lumped = [handle for handle in unused if handle in cited]
        uncited = [handle for handle in unused if handle not in cited]
        logger.info(
            "draft gives %d of %d sources a citation of their own (%d uncited, "
            "%d only in a long list); asking once for the rest",
            covered,
            len(payload.sources),
            len(uncited),
            len(lumped),
        )

        try:
            retry = await self._client.synthesize(
                payload, feedback=_coverage_feedback(covered, uncited, lumped)
            )
        except LLMError as exc:
            ctx.record_usage(self.name, exc.model, exc.usage)
            logger.warning(
                "coverage re-prompt failed (%s); keeping the first draft (%d covered)",
                exc,
                covered,
            )
            return result

        ctx.record_usage(self.name, retry.model, retry.usage)
        improved = len(_covered_handles(retry.output, offered))
        if improved <= covered:
            logger.info(
                "coverage re-prompt covered %d of %d (was %d); keeping the first draft",
                improved,
                len(payload.sources),
                covered,
            )
            return result

        logger.info(
            "coverage re-prompt covered %d of %d (was %d)",
            improved,
            len(payload.sources),
            covered,
        )
        return retry

    async def _edit_for_readers(
        self,
        ctx: PipelineContext,
        payload: SynthesisInput,
        result: LLMResult[SynthesisOutput],
    ) -> tuple[LLMResult[SynthesisOutput], dict[str, object]]:
        """Reword the draft for everyday readers, a paragraph at a time.

        See ``reader_edit.py``. Like the coverage re-prompt, this can only
        improve the outcome: a call that raises or a paragraph the guards refuse
        leaves that paragraph as it was, and an assembled article that fails a
        whole-article check leaves the draft exactly as it was. Every call's
        tokens are recorded. A retryable failure (the model server is down or
        broken) stops the pass rather than waiting out the same failure once
        per paragraph. A refused paragraph is asked for once more, told why.
        """
        draft = result.output
        before = reader_edit.readability(draft)
        texts: list[str] = []
        earlier: list[str] = []
        refused: list[str] = []
        retried: list[str] = []
        # Explanations the body has given so far; the summary keeps its own.
        explained: set[str] = set()
        stopped = False
        for part in reader_edit.parts(draft):
            text = part.text
            # The first call edits the draft's paragraph. One more is made when
            # that edit was refused (from the draft again, told why) or kept but
            # still leaves something to fix (from that edit, told what), and the
            # plainer of the kept edits wins.
            request = ReaderEditInput(
                product=payload.product,
                earlier=earlier,
                paragraph=part.text,
                max_sentences=part.max_sentences,
            )
            reason: str | None = None
            for attempt in range(2):
                if stopped:
                    break
                try:
                    edit = await self._client.edit_for_readers(request)
                except LLMError as exc:
                    ctx.record_usage(self.name, exc.model, exc.usage)
                    reason = reason or f"call failed: {exc}"
                    stopped = exc.retryable
                    break
                ctx.record_usage(self.name, edit.model, edit.usage)
                proposed = edit.output.text
                if part.slot != "summary":
                    proposed = reader_edit.drop_repeated_explanations(proposed, explained)
                rejection = reader_edit.paragraph_rejection(part, proposed, payload.sources)
                if rejection is None:
                    plainer = reader_edit.score(proposed) < reader_edit.score(text)
                    if text == part.text or plainer:
                        text = proposed
                    reason = None
                elif text == part.text:
                    reason = rejection
                if attempt == 1:
                    break
                note = reader_edit.leftovers(text) if text != part.text else None
                if text != part.text and note:
                    retried.append(f"{part.name}: {note}")
                    request = request.model_copy(
                        update={
                            "paragraph": text,
                            "feedback": f"This is your edit so far. Still to fix: {note}. "
                            "Change those sentences as you changed the others; keep the rest.",
                        }
                    )
                elif text == part.text and reason:
                    retried.append(f"{part.name}: {reason}")
                    request = request.model_copy(
                        update={
                            "feedback": "Your last edit of this paragraph was thrown away "
                            f"because it {reason}. Edit it again, keeping to every rule."
                        }
                    )
                else:
                    break
            if text == part.text and reason is not None:
                refused.append(f"{part.name}: {reason}")
            texts.append(text)
            if part.slot != "summary":
                earlier.append(text)
                explained |= reader_edit.explanations(text)

        metrics: dict[str, object] = {
            "paragraphs": len(texts),
            "refused": refused,
            "retried": retried,
        }
        edited = reader_edit.assemble(draft, texts)
        rejected = (
            "the edited article broke the output contract"
            if edited is None
            else reader_edit.article_rejection(draft, edited, payload)
        )
        if edited is None or rejected:
            logger.info("reader edit %s; keeping the draft", rejected)
            return result, {**metrics, "kept": 0, "rejected": rejected, "before": before}

        kept = len(texts) - len(refused)
        after = reader_edit.readability(edited)
        logger.info(
            "reader edit kept %d of %d paragraphs (%s -> %s)", kept, len(texts), before, after
        )
        return (
            LLMResult(output=edited, usage=result.usage, model=result.model),
            {**metrics, "kept": kept, "before": before, "after": after},
        )

    def _no_evidence(
        self, ctx: PipelineContext, payload: SynthesisInput
    ) -> PipelineContext:
        """Take the deterministic branch: no model call, no tokens, no cost.

        Records *why* the source list was empty. The two causes are the same
        sentence to a reader and different problems to us: nothing published on
        the topic is the product working as intended, whereas candidates
        retrieved and then filtered away is a signal that ``min_year`` or the
        grade floor is tighter than the corpus can satisfy. Distinguishing them
        after the fact means reading two stages of metrics, so it is recorded
        here where both numbers are in hand.
        """
        retrieved = sum(len(papers) for papers in ctx.candidates.values())
        cause = "no_candidates_retrieved" if retrieved == 0 else "all_candidates_filtered"

        draft = no_evidence_draft(payload.product, payload.target_claims)

        logger.info(
            "no usable sources for %r (%s, %d retrieved); "
            "writing the deterministic no-evidence article",
            payload.product,
            cause,
            retrieved,
        )

        ctx.record_metrics(
            self.name,
            {
                "verdict": str(draft.verdict),
                "sources_in_prompt": 0,
                "handles_cited": 0,
                "handles_covered": 0,
                "handles_listed_not_written": 0,
                "body_words": _body_words(draft),
                "model_called": False,
                # Recorded as False rather than omitted so that "how often does
                # the coverage nudge fire?" is one query over every run, not one
                # that has to know which branch each row came from.
                "coverage_retry": False,
                "no_evidence_cause": cause,
                "candidates_retrieved": retrieved,
            },
        )

        return ctx.model_copy(update={"synthesis_input": payload, "draft": draft})

    @staticmethod
    def _build_input(ctx: PipelineContext) -> SynthesisInput:
        """Flatten per-claim ranked sources into one handle-ordered list.

        Deduplicated by handle: a source backing two claims appears once in the
        prompt. Presenting it twice would invite the model to cite it as two
        separate findings.
        """
        assert ctx.extraction is not None

        by_handle: dict[str, PromptSource] = {}
        for claim, ranked in ctx.ranked.items():
            for entry in ranked:
                # For VALIDATE only: render_source_block does not show it.
                stance = ctx.stances.get(claim, {}).get(entry.source_id)
                if (existing := by_handle.get(entry.citation_handle)) is not None:
                    # Still one entry in the prompt — but it now records every
                    # claim it answers, which is what the per-claim quorum
                    # counts against. Dropping the second claim outright, as
                    # this did, made a shared source invisible to one of them.
                    if claim not in existing.claims:
                        existing.claims.append(claim)
                    if stance is not None:
                        existing.stances[claim] = stance
                    continue
                by_handle[entry.citation_handle] = PromptSource(
                    handle=entry.citation_handle,
                    title=entry.paper.title,
                    abstract=entry.paper.abstract,
                    journal=entry.paper.journal,
                    year=entry.paper.year,
                    study_type=entry.paper.study_type,
                    source_id=entry.source_id,
                    claims=[claim],
                    excerpts=ctx.excerpts.get(entry.source_id, []),
                    stances={claim: stance} if stance is not None else {},
                )

        ordered = sorted(by_handle.values(), key=lambda s: int(s.handle[1:]))
        return SynthesisInput(
            product=ctx.extraction.product,
            target_claims=ctx.extraction.target_claims,
            sources=ordered,
        )


def _filter_by_relevance(payload: SynthesisInput) -> tuple[SynthesisInput, dict[str, object]]:
    """Trim the prompt to what APPRAISE found relevant (``synthesis_relevance_filter``).

    A source labelled ``off_topic`` for every claim it was retrieved for is
    left out; the rest name only the claims they were not labelled off-topic
    for. An unappraised claim counts as relevant: a missing label means nobody
    looked, never "off topic".

    **Never trims to nothing.** If every source was labelled off-topic the
    payload is returned whole — an empty one would take the no-evidence
    template, and labels measured at ~75% must not decide on their own that an
    article has no evidence. ``claims`` is untouched, so the quorum validation
    applies is the same either way; a dropped source is still recorded in
    ``article_sources`` (PERSIST reads ``ctx.ranked``), uncited.
    """
    kept: list[PromptSource] = []
    narrowed = 0
    for source in payload.sources:
        relevant = [
            claim for claim in source.claims
            if source.stances.get(claim) is not Stance.OFF_TOPIC
        ]
        if not relevant:
            continue
        if len(relevant) < len(source.claims):
            narrowed += 1
        kept.append(source.model_copy(update={"shown_claims": relevant}))

    if not kept:
        return payload, {"dropped": 0, "narrowed": 0, "skipped": "all_off_topic"}
    dropped = sorted(
        (s.handle for s in payload.sources if s.handle not in {k.handle for k in kept}),
        key=lambda handle: int(handle[1:]),
    )
    return (
        payload.model_copy(update={"sources": kept}),
        {"dropped": len(dropped), "dropped_handles": dropped, "narrowed": narrowed},
    )


def _body_words(draft: SynthesisOutput) -> int:
    """Words of article body, recorded as a metric and never used as a target.

    Nothing branches on this. It is here so that "how long are articles coming
    out?" is answerable from ``pipeline_stage_runs`` without re-parsing the
    documents — which is how the 66-to-266-word problem was found in the first
    place. A *threshold* on it was tried and removed: see ``MIN_CITED_FRACTION``.

    ``as_text()`` is beats plus sections, which is every block
    ``body_text_to_doc`` emits, and it still counts the ``[S1]`` markers that
    the frontend's own word count skips. Close enough for a trend line, which is
    all it is.
    """
    return len(draft.body.as_text().split())


def _is_thin(draft: SynthesisOutput, payload: SynthesisInput) -> bool:
    """Ignoring most of its sources, *and* given plenty. Both halves required."""
    if len(payload.sources) < SECTIONS_EXPECTED_FROM:
        return False
    covered = len(_covered_handles(draft, payload.handle_set))
    return covered < len(payload.sources) * MIN_CITED_FRACTION


def _covered_handles(draft: SynthesisOutput, offered: set[str]) -> set[str]:
    """The offered sources this draft gives a citation of their own.

    A handle counts when some citation carrying it holds at most
    ``MAX_HANDLES_PER_CITATION`` handles, so a source cited once on its own and
    again in a long list still counts. A handle the prompt never offered does
    not: it is not a source, and VALIDATE rejects the draft for it anyway.
    """
    covered: set[str] = set()
    for citation in CITATION_RUN_RE.findall(draft.body.as_text()):
        handles = extract_handles(citation)
        if len(handles) <= MAX_HANDLES_PER_CITATION:
            covered |= handles
    return covered & offered


def _coverage_feedback(covered: int, uncited: list[str], lumped: list[str]) -> str:
    """The note appended to the user turn on a re-prompt.

    Every sentence points at the *unused sources*, and the explicit refusal of
    padding is load-bearing: an instruction to write more, handed to a model with
    nothing left to say, produces exactly the manufactured nuance DESIGN.md §6
    refuses. Nothing here names a word count, because nothing here wants one.

    Sources cited only inside a long list are named apart from those never
    cited, and the limit is stated, because the model cannot meet a rule it was
    never told.
    """
    gaps = [f"That draft gave {covered} of the sources above a citation of their own."]
    if uncited:
        gaps.append(f"It never cited {len(uncited)}: {', '.join(uncited)}.")
    if lumped:
        gaps.append(
            f"It cited {len(lumped)} only inside a long list of handles, which "
            f"says nothing about what each one found: {', '.join(lumped)}."
        )
    return (
        " ".join(gaps) + "\n\n"
        "Go back through those and give each one a sentence: what it measured, "
        "in whom, and what it found, with its handle. A citation may carry at "
        f"most {MAX_HANDLES_PER_CITATION} handles; a longer list does not count "
        "as using them. Take every claim in turn and check it has sources cited "
        "against it.\n\n"
        "Do not pad. Every sentence you add must state a specific finding from "
        "one of the sources above and carry its handle. If a source does not "
        "bear on any claim, say so and cite it there. If a source genuinely has "
        "nothing to add, leave it out: an article that works through the "
        "evidence honestly is the goal, not a longer one."
    )


def _is_revision(ctx: PipelineContext) -> bool:
    """This pass was sent back by VALIDATE, with a failed report to fix."""
    return (
        ctx.revise_round > 0
        and ctx.draft is not None
        and ctx.synthesis_input is not None
        and ctx.validation is not None
        and not ctx.validation.passed
    )


#: Failure messages fed back in a revision. Past this the list is noise.
_MAX_FAILURES_FED_BACK = 12


def _draft_as_text(draft: SynthesisOutput) -> str:
    """The failed draft, laid out so each part can be copied back unchanged."""
    body = draft.body
    parts = [
        f"Headline: {draft.headline}",
        f"Verdict: {draft.verdict.value}"
        + (f" ({draft.verdict_qualifier})" if draft.verdict_qualifier else ""),
        f"Summary: {draft.summary}",
        f"Beat 1 (the claim): {body.beat_1_claim}",
        f"Beat 2 (what the research shows): {body.beat_2_evidence}",
    ]
    parts += [f"Section \"{s.heading}\": {s.body}" for s in body.sections]
    parts.append(f"Beat 3 (bottom line): {body.beat_3_bottom_line}")
    return "\n\n".join(parts)


def revision_feedback(report: ValidationReport, draft: SynthesisOutput | None = None) -> str:
    """The note appended to the user turn when a draft is sent back.

    It names the failures VALIDATE found, verbatim, and asks for those fixed
    and nothing else — a rewrite told only "try again" is a fresh draft with
    fresh mistakes. The verdict ceiling is stated when known, because
    ``verdict_exceeds_grade`` cannot be fixed without it.

    **The failed draft is included.** "Keep everything else" meant nothing to a
    model that could not see what "everything else" was: measured 2026-10-02,
    10 of 12 drafts sent back for a misattributed number came back with a new
    misattribution in a different sentence, having been rewritten from scratch.
    The coverage re-prompt deliberately does *not* show its draft (that one asks
    for more of the sources, and a draft to extend invites padding); a revision
    asks for a correction, which needs the text being corrected.
    """
    lines = [f"- {failure.message}" for failure in report.failures[:_MAX_FAILURES_FED_BACK]]
    ceiling = (
        f" The verdict may be at most '{report.verdict_ceiling.value}' for the evidence "
        "above; it may be lower."
        if report.verdict_ceiling is not None
        else ""
    )
    shown = (
        f"Your previous draft was:\n\n{_draft_as_text(draft)}\n\n" if draft is not None else ""
    )
    return (
        shown
        + "That draft failed the citation checks, so it cannot be published as written:\n"
        + "\n".join(lines)
        + "\n\nRewrite it to fix exactly these problems and keep everything else: copy every "
        "sentence the problems do not name unchanged, with its citations. "
        "Cite only the handles listed with the sources above, one handle per source "
        "in square brackets like [S1], and use square brackets for nothing else. "
        "Every sentence of the evidence beat and every section must carry at least "
        f"one handle.{ceiling}"
    )
