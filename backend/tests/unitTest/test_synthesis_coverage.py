"""The coverage nudge in SYNTHESIZE.

A draft handed 24 systematic reviews that writes about three of them is
under-using its evidence, and that is the defect this re-prompt exists for.

It is deliberately **not** a length rule. It was one briefly, aimed at a
three-minute read, and that was the wrong target twice over: it fired on
essentially every run because no local model this machine can hold writes 700
words, and length was never the defect anyway. DESIGN.md §6 is explicit that
length is a ceiling and never a floor, so a thoroughly-cited short article is a
correct output and is never re-prompted.

What these tests pin is mostly the set of cases where it must NOT fire, and the
guarantee that it can never cost a run.

A source counts as used only when it has a citation of its own: at most
``MAX_HANDLES_PER_CITATION`` handles in one chip. Counting every handle in the
body let a 109-word draft count as citing 22 of its 24 sources, because 19 of
them sat in one bracket.
"""

from __future__ import annotations

from app.domain.contracts import (
    ArticleBody,
    ArticleSection,
    CandidatePaper,
    ExtractionOutput,
    PromptSource,
    RankedSource,
    SynthesisInput,
    SynthesisOutput,
    ValidationFailure,
    ValidationReport,
)
from app.domain.enums import SourceApi, StudyType, Verdict
from app.llm.base import LLMError, LLMResult, TokenUsage
from app.pipeline.stages import PipelineContext
from app.pipeline.steps.synthesize import (
    MAX_HANDLES_PER_CITATION,
    MIN_CITED_FRACTION,
    SECTIONS_EXPECTED_FROM,
    SynthesizeStage,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _output(beat_2: str, sections: list[ArticleSection]) -> SynthesisOutput:
    return SynthesisOutput(
        headline="Ashwagandha and stress: what the evidence shows",
        verdict=Verdict.MIXED,
        summary="A short summary sentence.",
        body=ArticleBody(
            beat_1_claim="It is claimed to reduce stress.",
            beat_2_evidence=beat_2,
            sections=sections,
            beat_3_bottom_line="The evidence is early.",
        ),
    )


def _draft(*, cites: int, sentences_each: int = 2) -> SynthesisOutput:
    """A schema-valid draft giving handles S1..S`cites` a citation each.

    Beat 2 carries the first handle (validation requires it). The rest are dealt
    round-robin over up to five sections, each handle in sentences of its own,
    so every one counts as covered. At two sentences per handle that stays inside
    the 8-sentence section cap up to 21 handles; these tests use at most 13.
    """
    rest = [f"S{i}" for i in range(2, cites + 1)]
    groups = [rest[start::5] for start in range(min(5, len(rest)))]
    sections = [
        ArticleSection(
            heading=f"What trial {index + 1} measured",
            body=" ".join(
                f"A trial measured a thing and found a result [{handle}]."
                for handle in group
                for _ in range(sentences_each)
            ),
        )
        for index, group in enumerate(groups)
    ]
    return _output("One trial found a small effect [S1].", sections)


def _lumped_draft(*, cites: int) -> SynthesisOutput:
    """S1 cited on its own in beat 2; S2..S`cites` all in one bracket.

    The measured shape: the 109-word article that counted as citing 22 of its 24
    sources, 19 of them in a single marker.
    """
    handles = ", ".join(f"S{i}" for i in range(2, cites + 1))
    return _output(
        "One trial found a small effect [S1].",
        [
            ArticleSection(
                heading="What the other trials found", body=f"They agreed [{handles}]."
            )
        ],
    )


def _payload(n_sources: int) -> SynthesisInput:
    return SynthesisInput(
        product="Ashwagandha",
        target_claims=["reduces stress"],
        sources=[
            PromptSource(
                handle=f"S{i + 1}",
                title=f"Trial {i + 1}",
                abstract="An abstract.",
                study_type=StudyType.SYSTEMATIC_REVIEW,
                source_id=f"id-{i + 1}",
                claims=["reduces stress"],
            )
            for i in range(n_sources)
        ],
    )


def _context(n_sources: int) -> PipelineContext:
    return PipelineContext(
        run_id="run-1",
        topic="Ashwagandha",
        extraction=ExtractionOutput(
            product="Ashwagandha",
            target_claims=["reduces stress"],
            ingredients=["ashwagandha"],
        ),
        ranked={
            "reduces stress": [
                RankedSource(
                    source_id=f"id-{i + 1}",
                    claim="reduces stress",
                    citation_handle=f"S{i + 1}",
                    paper=CandidatePaper(
                        pmid=str(1000 + i),
                        title=f"Trial {i + 1}",
                        abstract="An abstract.",
                        study_type=StudyType.SYSTEMATIC_REVIEW,
                        source_api=SourceApi.PUBMED,
                    ),
                    cosine_similarity=0.5,
                    final_score=0.6,
                )
                for i in range(n_sources)
            ]
        },
    )


class RecordingClient:
    """Returns each queued draft in turn and records the feedback it was sent."""

    def __init__(self, *drafts: SynthesisOutput) -> None:
        self._drafts = list(drafts)
        self.calls: list[str | None] = []

    async def synthesize(
        self, payload: SynthesisInput, *, feedback: str | None = None
    ) -> LLMResult[SynthesisOutput]:
        self.calls.append(feedback)
        draft = self._drafts[min(len(self.calls) - 1, len(self._drafts) - 1)]
        return LLMResult(
            output=draft,
            usage=TokenUsage(input_tokens=100, output_tokens=50),
            model="ollama/test",
        )


# ---------------------------------------------------------------------------
# When it must not fire
# ---------------------------------------------------------------------------


async def test_a_thin_source_set_is_left_alone() -> None:
    """The anti-padding guarantee, and why the gate has two halves. A draft
    citing one of two sources has nothing more to say, and asking for more can
    only invent it."""
    client = RecordingClient(_draft(cites=1))

    result = await SynthesizeStage(client).run(  # type: ignore[arg-type]
        _context(SECTIONS_EXPECTED_FROM - 1)
    )

    assert client.calls == [None]
    assert result.metrics["synthesize"]["coverage_retry"] is False


async def test_a_well_covered_draft_is_left_alone_however_short() -> None:
    """Length is not the test. A short article that works through its evidence
    is a correct output, and DESIGN.md §6 forbids a floor on it."""
    client = RecordingClient(_draft(cites=10, sentences_each=1))

    result = await SynthesizeStage(client).run(_context(10))  # type: ignore[arg-type]

    assert client.calls == [None], "cited every source; nothing to ask for"
    assert result.metrics["synthesize"]["coverage_retry"] is False
    assert result.metrics["synthesize"]["body_words"] < 200, "and it is short"


async def test_the_threshold_is_a_fraction_of_what_was_offered() -> None:
    """Twelve citations is thorough against 13 sources and thin against 100."""
    assert 13 * MIN_CITED_FRACTION <= 12, "guard: 12 of 13 clears the bar"
    over = RecordingClient(_draft(cites=12))
    await SynthesizeStage(over).run(_context(13))  # type: ignore[arg-type]
    assert over.calls == [None]

    assert 30 * MIN_CITED_FRACTION > 12, "guard: 12 of 30 is below the bar"
    under = RecordingClient(_draft(cites=12))
    await SynthesizeStage(under).run(_context(30))  # type: ignore[arg-type]
    assert len(under.calls) == 2


# ---------------------------------------------------------------------------
# When it fires
# ---------------------------------------------------------------------------


async def test_a_thinly_cited_draft_is_re_prompted_once() -> None:
    thin, full = _draft(cites=3), _draft(cites=13)
    client = RecordingClient(thin, full)

    result = await SynthesizeStage(client).run(_context(13))  # type: ignore[arg-type]

    assert len(client.calls) == 2, "expected exactly one re-prompt"
    assert client.calls[0] is None
    assert client.calls[1] is not None
    assert result.draft is full
    assert result.metrics["synthesize"]["coverage_retry"] is True
    assert result.metrics["synthesize"]["first_draft_handles_cited"] == 3


async def test_it_asks_only_once_even_if_the_second_draft_is_also_thin() -> None:
    """A floor would loop here. This is a nudge, so it accepts and moves on."""
    client = RecordingClient(_draft(cites=3), _draft(cites=4))

    result = await SynthesizeStage(client).run(_context(13))  # type: ignore[arg-type]

    assert len(client.calls) == 2
    assert result.draft is not None
    assert result.validation is None  # nothing failed; it goes on to VALIDATE


async def test_the_re_prompt_names_the_sources_the_draft_ignored() -> None:
    """The note has to point at unused evidence, not at a word count, or it is
    an instruction to pad."""
    client = RecordingClient(_draft(cites=2))

    await SynthesizeStage(client).run(_context(13))  # type: ignore[arg-type]

    note = client.calls[1]
    assert note is not None
    assert "S13" in note and "S3" in note
    assert "S1," not in note, "the sources it did cite should not be listed"
    assert "Do not pad" in note
    assert "word" not in note.lower(), "this is not a length instruction"


# ---------------------------------------------------------------------------
# What counts as using a source
# ---------------------------------------------------------------------------


async def test_a_draft_listing_its_sources_in_one_bracket_is_re_prompted() -> None:
    """Measured: a 109-word article counted as citing 22 of its 24 sources,
    because 19 sat in one bracket. Counting handles let it through; counting
    citations of their own does not."""
    full = _draft(cites=13)
    client = RecordingClient(_lumped_draft(cites=13), full)

    result = await SynthesizeStage(client).run(_context(13))  # type: ignore[arg-type]

    metrics = result.metrics["synthesize"]
    assert metrics["first_draft_handles_cited"] == 13, "every handle is in the body"
    assert metrics["first_draft_handles_covered"] == 1, "only S1 has a citation of its own"
    assert len(client.calls) == 2
    assert result.draft is full
    assert metrics["handles_covered"] == 13


async def test_a_citation_of_three_counts_and_a_run_of_four_does_not() -> None:
    """Three is the prompt's own example of a shared citation, `[S1, S5, S8]`.
    Adjacent brackets are one chip on the page, so `[S1][S2][S3][S4]` is a
    citation of four, not four citations."""
    threes = _output(
        "Three trials agreed [S1, S2, S3].",
        [
            ArticleSection(heading="Next three", body="They agreed [S4, S5, S6]."),
            ArticleSection(heading="Three more", body="They agreed [S7, S8, S9]."),
            ArticleSection(heading="Last three", body="They agreed [S10][S11][S12]."),
        ],
    )
    fours = _output(
        "Four trials agreed [S1][S2][S3][S4].",
        [
            ArticleSection(heading="Next four", body="They agreed [S5, S6, S7, S8]."),
            ArticleSection(heading="Last four", body="They agreed [S9][S10][S11][S12]."),
        ],
    )

    left_alone = RecordingClient(threes)
    await SynthesizeStage(left_alone).run(_context(12))  # type: ignore[arg-type]
    assert left_alone.calls == [None], "every source is in a citation of three"

    asked = RecordingClient(fours)
    result = await SynthesizeStage(asked).run(_context(12))  # type: ignore[arg-type]
    assert len(asked.calls) == 2, "no source has a citation of its own"
    assert result.metrics["synthesize"]["first_draft_handles_covered"] == 0


async def test_a_second_draft_that_lists_more_in_one_bracket_is_discarded() -> None:
    """Judged by the count the re-prompt asked about. By raw handles the lumped
    draft cites all 13 and would replace a first draft that gave five sources a
    sentence each."""
    first = _draft(cites=5)
    client = RecordingClient(first, _lumped_draft(cites=13))

    result = await SynthesizeStage(client).run(_context(13))  # type: ignore[arg-type]

    assert len(client.calls) == 2
    assert result.draft is first


async def test_the_re_prompt_says_which_sources_were_only_in_a_long_list() -> None:
    """Told they were unused, a model that can see its own long bracket has been
    told something false. The note says what happened and states the limit."""
    client = RecordingClient(_lumped_draft(cites=13))

    await SynthesizeStage(client).run(_context(13))  # type: ignore[arg-type]

    note = client.calls[1]
    assert note is not None
    assert "only inside a long list" in note
    assert "S2," in note and "S13" in note
    assert "never cited" not in note, "every source was cited somewhere"
    assert f"at most {MAX_HANDLES_PER_CITATION} handles" in note
    assert "Do not pad" in note
    assert "word" not in note.lower(), "this is not a length instruction"


async def test_both_calls_are_billed() -> None:
    """Two calls happened, so two calls are charged. A stage that reports only
    the second makes the run record lie about what the nudge cost."""
    client = RecordingClient(_draft(cites=3), _draft(cites=13))

    result = await SynthesizeStage(client).run(_context(13))  # type: ignore[arg-type]

    assert result.usage.input_tokens == 200
    assert result.usage.output_tokens == 100


# ---------------------------------------------------------------------------
# It can never make the outcome worse
# ---------------------------------------------------------------------------


async def test_a_second_draft_citing_fewer_sources_is_discarded() -> None:
    """Observed live, both halves of it: a draft citing 3 of 13 came back citing
    1, and separately a 152-word `mixed` draft came back at `no_evidence` while
    citing everything. Neither is caught downstream — `no_evidence` is exempt
    from the cited-beat rule and sits below every ceiling, so it validates clean
    and publishes."""
    first = _draft(cites=5)
    client = RecordingClient(first, _draft(cites=2))

    result = await SynthesizeStage(client).run(_context(13))  # type: ignore[arg-type]

    assert len(client.calls) == 2, "it still asks"
    assert result.draft is first, "but keeps the better answer"


class FailingRetryClient:
    def __init__(self, first: SynthesisOutput) -> None:
        self._first = first
        self.calls = 0

    async def synthesize(
        self, payload: SynthesisInput, *, feedback: str | None = None
    ) -> LLMResult[SynthesisOutput]:
        self.calls += 1
        if feedback is None:
            return LLMResult(
                output=self._first,
                usage=TokenUsage(input_tokens=100, output_tokens=50),
                model="ollama/test",
            )
        raise LLMError(
            "boom",
            usage=TokenUsage(input_tokens=90, output_tokens=10),
            model="ollama/test",
        )


async def test_a_failed_re_prompt_keeps_the_first_draft() -> None:
    """The first draft is already publishable — it passed the same contract the
    second one would have. Failing the run here would give this nudge a veto it
    has no business having."""
    first = _draft(cites=3)
    client = FailingRetryClient(first)

    result = await SynthesizeStage(client).run(_context(13))  # type: ignore[arg-type]

    assert client.calls == 2
    assert result.draft is first


async def test_a_failed_re_prompt_still_records_its_tokens() -> None:
    client = FailingRetryClient(_draft(cites=3))

    result = await SynthesizeStage(client).run(_context(13))  # type: ignore[arg-type]

    assert result.usage.input_tokens == 190
    assert result.usage.output_tokens == 60


# ---------------------------------------------------------------------------
# The revision pass (sent back by VALIDATE)
# ---------------------------------------------------------------------------


def _failed_report() -> ValidationReport:
    return ValidationReport(
        passed=False,
        citations_total=2,
        citations_resolved=1,
        best_evidence_grade=StudyType.SYSTEMATIC_REVIEW,
        verdict_ceiling=Verdict.MIXED,
        failures=[
            ValidationFailure(
                code="hallucinated_handle", message="Citation S9 was never provided"
            )
        ],
    )


def _revision_context(first: SynthesisOutput) -> PipelineContext:
    ctx = _context(3)
    return ctx.model_copy(
        update={
            # Built the way the first pass built it, so the test can check the
            # revision reuses it rather than rebuilding.
            "synthesis_input": SynthesizeStage._build_input(ctx),
            "draft": first,
            "validation": _failed_report(),
            "revise_round": 1,
        }
    )


async def test_a_revision_feeds_back_the_failures_and_reuses_the_payload() -> None:
    first, second = _draft(cites=1), _draft(cites=3)
    client = RecordingClient(second)

    result = await SynthesizeStage(client).run(_revision_context(first))  # type: ignore[arg-type]

    assert len(client.calls) == 1, "one rewrite, no coverage re-prompt on top"
    feedback = client.calls[0] or ""
    assert "Citation S9 was never provided" in feedback
    assert "at most 'mixed'" in feedback
    assert result.draft == second
    assert result.validation is None, "the rewrite is validated afresh"
    assert result.synthesis_input == SynthesizeStage._build_input(_context(3))
    assert result.metrics["synthesize"]["revision"] is True
    assert result.metrics["synthesize"]["failures_fed_back"] == ["hallucinated_handle"]


async def test_a_revision_that_errors_keeps_the_failed_draft() -> None:
    """It fails validation again and is stored validation_failed, as before."""

    class _Broken:
        async def synthesize(
            self, payload: SynthesisInput, *, feedback: str | None = None
        ) -> LLMResult[SynthesisOutput]:
            raise LLMError("ollama timed out", usage=TokenUsage(input_tokens=10))

    first = _draft(cites=1)
    result = await SynthesizeStage(_Broken()).run(_revision_context(first))  # type: ignore[arg-type]

    assert result.draft == first
    assert result.validation is not None and not result.validation.passed
    assert "ollama timed out" in result.metrics["synthesize"]["revision_failed"]
