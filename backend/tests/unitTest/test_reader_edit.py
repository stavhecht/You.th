"""The reader edit (``pipeline/steps/reader_edit.py``).

The guards are the point: a paragraph whose edit changes anything but the
wording keeps its original text, and an edit that fails in any way leaves the
draft as it was. These pin both, with sentences from the reference article
(foam rolling and range of motion, 2026-10-15).
"""

from __future__ import annotations

from app.domain.contracts import (
    ArticleBody,
    ArticleSection,
    CitationGroup,
    ExtractionOutput,
    PromptSource,
    ReaderEditInput,
    ReaderEditOutput,
    SynthesisInput,
    SynthesisOutput,
)
from app.domain.enums import StudyType, Verdict
from app.llm.base import LLMError, LLMResult, TokenUsage
from app.pipeline.stages import PipelineContext
from app.pipeline.steps.reader_edit import (
    Part,
    assemble,
    drop_repeated_explanations,
    explanations,
    paragraph_rejection,
    parts,
    readability,
)
from app.pipeline.steps.synthesize import SynthesizeStage

SOURCES = [
    PromptSource(
        handle=handle,
        title=f"Review {handle}",
        abstract=abstract,
        journal=None,
        year=year,
        study_type=StudyType.META_ANALYSIS,
        source_id=f"id-{handle}",
        claims=["improves range of motion"],
    )
    for handle, year, abstract in [
        ("S1", 2020, "Twenty-six trials. Large effect (SMD: 0.74, 95% CI 0.42-1.01)."),
        ("S2", 2022, "Eleven studies. Moderate effect (ES = 0.823) after 4 weeks."),
    ]
]
PAYLOAD = SynthesisInput(
    product="Foam rolling", target_claims=["improves range of motion"], sources=SOURCES
)

BEAT_2 = Part(
    "beat 2",
    "A 2020 meta-analysis of 26 trials found a large acute increase in range of motion "
    "(SMD 0.74, 95% CI 0.42 to 1.01) [S1].",
    5,
    "beat_2",
)
PLAIN_BEAT_2 = (
    "Rolling loosened muscles right away: a 2020 meta-analysis (a study that combines the "
    "results of earlier trials) of 26 trials found a large effect (effect size 0.74, a "
    "standard score of how big a difference is) [S1]."
)
SECTION = (
    "A 2022 meta-analysis of 11 studies found a moderate effect (ES = 0.823) for foam "
    "rolling training over more than 4 weeks [S2]."
)
PLAIN_SECTION = (
    "Rolling regularly helped too, but only after more than 4 weeks: a 2022 meta-analysis "
    "of 11 studies found a moderate effect (effect size 0.823) [S2]."
)

DRAFT = SynthesisOutput(
    headline="Foam rolling improves range of motion, mostly short term",
    verdict=Verdict.SUPPORTED,
    summary="Rolling loosens muscles for a short while.",
    body=ArticleBody(
        beat_1_claim="Foam rolling is promoted as a way to move further.",
        beat_2_evidence=BEAT_2.text,
        sections=[ArticleSection(heading="Long-term training effects", body=SECTION)],
        beat_3_bottom_line="The effect is real but short-lived.",
    ),
    citations=[CitationGroup(claim="large effect", source_ids=["S1", "S2"])],
)


# --- the paragraph guards ----------------------------------------------------


def test_a_pure_rewording_is_kept() -> None:
    assert paragraph_rejection(BEAT_2, PLAIN_BEAT_2, SOURCES) is None


def test_a_changed_citation_is_refused() -> None:
    edited = PLAIN_BEAT_2.replace("[S1]", "[S1, S2]")
    assert "cites" in (paragraph_rejection(BEAT_2, edited, SOURCES) or "")


def test_an_added_number_is_refused_even_when_a_source_has_it() -> None:
    """S2 says 4 weeks, but this paragraph never did."""
    edited = PLAIN_BEAT_2.replace("right away", "within 4 weeks")
    assert "4" in (paragraph_rejection(BEAT_2, edited, SOURCES) or "")


def test_a_size_judgement_the_draft_did_not_make_is_refused() -> None:
    """2026-10-15: "(SMD 0.53)" became "a moderate effect (effect size 0.53)"."""
    section = Part("section", "A vibrating roller did better (SMD 0.53) [S1].", 8, "section_0")
    edited = "A vibrating roller did better, a moderate effect (effect size 0.53) [S1]."
    assert "moderate" in (paragraph_rejection(section, edited, SOURCES) or "")


def test_a_number_moved_to_another_source_is_refused() -> None:
    """Both years are in the paragraph, so only the pair check can see the swap."""
    part = Part(
        "beat 2",
        "A 2020 review found one thing [S1]. A 2022 review found another [S2].",
        5,
        "beat_2",
    )
    edited = "One thing, in a 2022 review [S1]. Another, in a 2020 review [S2]."
    assert "beside" in (paragraph_rejection(part, edited, SOURCES) or "")


def test_a_much_shorter_edit_is_refused() -> None:
    assert "shortened" in (paragraph_rejection(BEAT_2, "Rolling helped [S1].", SOURCES) or "")


def test_an_edit_over_the_sentence_limit_is_refused() -> None:
    edited = " ".join(["Rolling helped for a while [S1]."] * 6)
    assert "limit" in (paragraph_rejection(BEAT_2, edited, SOURCES) or "")


def test_a_stray_square_bracket_is_refused() -> None:
    edited = PLAIN_BEAT_2.replace("(effect size 0.74,", "[effect size 0.74,")
    assert "bracket" in (paragraph_rejection(BEAT_2, edited, SOURCES) or "")


def test_reassembly_keeps_the_headline_verdict_and_citations() -> None:
    texts = [p.text for p in parts(DRAFT)]
    texts[2] = PLAIN_BEAT_2
    edited = assemble(DRAFT, texts)
    assert edited is not None
    assert edited.body.beat_2_evidence == PLAIN_BEAT_2
    assert (edited.headline, edited.verdict, edited.citations) == (
        DRAFT.headline,
        DRAFT.verdict,
        DRAFT.citations,
    )


def test_the_measures_see_what_the_edit_is_for() -> None:
    texts = [p.text for p in parts(DRAFT)]
    texts[2], texts[3] = PLAIN_BEAT_2, PLAIN_SECTION
    edited = assemble(DRAFT, texts)
    assert edited is not None
    before, after = readability(DRAFT), readability(edited)
    assert (before["statistical_shorthand"], after["statistical_shorthand"]) == (3, 0)
    assert (before["study_led_sentences"], after["study_led_sentences"]) == (2, 0)


def test_an_explanation_is_given_once_and_numbers_are_never_dropped() -> None:
    seen = explanations(PLAIN_BEAT_2)
    again = (
        "Regular rolling helped too: a 2022 meta-analysis (a study that combines the results "
        "of earlier trials) found a moderate effect (effect size 0.823, a standard score of "
        "how big a difference is) [S2]."
    )
    assert drop_repeated_explanations(again, seen) == (
        "Regular rolling helped too: a 2022 meta-analysis found a moderate effect "
        "(effect size 0.823) [S2]."
    )
    assert drop_repeated_explanations("It was safe (no side effects) [S2].", seen) == (
        "It was safe (no side effects) [S2]."
    ), "short asides are not explanations"


def test_an_explanation_repeated_within_one_paragraph_is_given_once() -> None:
    """2026-10-16: beat 2 explained "effect size" in each of its three sentences."""
    gloss = "a standard score of how big a difference is"
    text = f"Big (effect size 0.74, {gloss}) [S1]. Moderate (effect size 0.823, {gloss}) [S2]."
    assert drop_repeated_explanations(text, set()) == (
        f"Big (effect size 0.74, {gloss}) [S1]. Moderate (effect size 0.823) [S2]."
    )


def test_a_section_of_several_paragraphs_is_edited_paragraph_by_paragraph() -> None:
    two = DRAFT.model_copy(
        update={
            "body": DRAFT.body.model_copy(
                update={
                    "sections": [
                        ArticleSection(
                            heading="Over weeks", body=f"{SECTION}\n\nIt varied [S2]."
                        )
                    ]
                }
            )
        }
    )
    pieces = [p for p in parts(two) if p.slot == "section_0"]
    assert [p.text for p in pieces] == [SECTION, "It varied [S2]."]
    assert [p.max_sentences for p in pieces] == [7, 7]
    rebuilt = assemble(two, [p.text for p in parts(two)])
    assert rebuilt is not None and rebuilt.body.sections[0].body == two.body.sections[0].body


# --- the stage ---------------------------------------------------------------


def _context() -> PipelineContext:
    return PipelineContext(
        run_id="run-1",
        topic="Foam rolling for range of motion",
        extraction=ExtractionOutput(
            product="Foam rolling", target_claims=["improves range of motion"], ingredients=[]
        ),
    )


class Client:
    """Writes DRAFT, then edits each paragraph by ``rewrite`` (or raises)."""

    def __init__(self, rewrite: dict[str, str | LLMError] | None = None) -> None:
        self._rewrite = rewrite or {}
        self.edits: list[ReaderEditInput] = []

    async def synthesize(
        self, payload: SynthesisInput, *, feedback: str | None = None
    ) -> LLMResult[SynthesisOutput]:
        return LLMResult(output=DRAFT, usage=TokenUsage(input_tokens=10), model="llamacpp/x")

    async def edit_for_readers(self, payload: ReaderEditInput) -> LLMResult[ReaderEditOutput]:
        self.edits.append(payload)
        answer = self._rewrite.get(payload.paragraph, payload.paragraph)
        if isinstance(answer, LLMError):
            raise answer
        return LLMResult(
            output=ReaderEditOutput(sentences=[answer]),
            usage=TokenUsage(input_tokens=5),
            model="x",
        )


class Stage(SynthesizeStage):
    """The real stage, with the payload fixed (building it needs ranked sources)."""

    @staticmethod
    def _build_input(ctx: PipelineContext) -> SynthesisInput:
        return PAYLOAD


async def test_good_paragraphs_are_kept_and_bad_ones_left_as_they_were() -> None:
    client = Client({BEAT_2.text: PLAIN_BEAT_2, SECTION: "Rolling helped [S2]."})
    ctx = await Stage(client, reader_edit=True).run(_context())  # type: ignore[arg-type]

    assert ctx.draft is not None
    assert ctx.draft.body.beat_2_evidence == PLAIN_BEAT_2
    assert ctx.draft.body.sections[0].body == SECTION, "the shortened section was refused"
    metrics = ctx.metrics["synthesize"]["reader_edit"]
    assert metrics["kept"] == metrics["paragraphs"] - 1
    assert "shortened" in metrics["refused"][0]


async def test_each_paragraph_is_shown_the_edited_article_so_far_but_not_the_summary() -> None:
    client = Client({BEAT_2.text: PLAIN_BEAT_2})
    await Stage(client, reader_edit=True).run(_context())  # type: ignore[arg-type]

    assert [len(e.earlier) for e in client.edits] == [0, 0, 1, 2, 3]
    assert client.edits[3].earlier[-1] == PLAIN_BEAT_2
    assert client.edits[3].max_sentences == 8


async def test_a_refused_paragraph_is_asked_for_once_more_and_told_why() -> None:
    class Retrying(Client):
        async def edit_for_readers(
            self, payload: ReaderEditInput
        ) -> LLMResult[ReaderEditOutput]:
            if payload.paragraph == BEAT_2.text and payload.feedback is None:
                self.edits.append(payload)
                bad = PLAIN_BEAT_2.replace("a large effect", "a large and proven effect")
                return LLMResult(
                    output=ReaderEditOutput(sentences=[bad]), usage=TokenUsage(), model="x"
                )
            return await super().edit_for_readers(payload)

    client = Retrying({BEAT_2.text: PLAIN_BEAT_2})
    ctx = await Stage(client, reader_edit=True).run(_context())  # type: ignore[arg-type]

    retry = [e for e in client.edits if e.paragraph == BEAT_2.text][1]
    assert "proven" in (retry.feedback or "")
    assert ctx.draft is not None and ctx.draft.body.beat_2_evidence == PLAIN_BEAT_2
    metrics = ctx.metrics["synthesize"]["reader_edit"]
    assert metrics["refused"] == [] and len(metrics["retried"]) == 1


HALF_DONE = (
    "Rolling loosened muscles right away: a 2020 meta-analysis (a study that combines the "
    "results of earlier trials) of 26 trials found a large effect (SMD 0.74) [S1]."
)


async def test_a_kept_edit_with_leftovers_is_sent_back_with_the_sentences_named() -> None:
    client = Client({BEAT_2.text: HALF_DONE, HALF_DONE: PLAIN_BEAT_2})
    ctx = await Stage(client, reader_edit=True).run(_context())  # type: ignore[arg-type]

    second = [e for e in client.edits if e.paragraph == HALF_DONE]
    assert len(second) == 1 and "sentence 1 (SMD 0.74)" in (second[0].feedback or "")
    assert ctx.draft is not None and ctx.draft.body.beat_2_evidence == PLAIN_BEAT_2


async def test_a_worse_second_edit_never_replaces_the_first() -> None:
    worse = HALF_DONE.replace("(SMD 0.74)", "(SMD 0.74, P = 0.01)")
    client = Client({BEAT_2.text: HALF_DONE, HALF_DONE: worse})
    ctx = await Stage(client, reader_edit=True).run(_context())  # type: ignore[arg-type]

    assert ctx.draft is not None and ctx.draft.body.beat_2_evidence == HALF_DONE


async def test_a_server_outage_stops_the_pass_and_keeps_the_draft() -> None:
    outage = LLMError("Compute error", retryable=True)
    client = Client({DRAFT.body.beat_1_claim: outage, BEAT_2.text: PLAIN_BEAT_2})
    ctx = await Stage(client, reader_edit=True).run(_context())  # type: ignore[arg-type]

    assert len(client.edits) == 2, "no call after the outage"
    assert ctx.draft is not None and ctx.draft.body == DRAFT.body


async def test_the_edit_is_off_unless_asked_for() -> None:
    client = Client({BEAT_2.text: PLAIN_BEAT_2})
    ctx = await Stage(client).run(_context())  # type: ignore[arg-type]

    assert client.edits == []
    assert ctx.metrics["synthesize"]["reader_edit"] is None


async def test_a_revised_draft_is_edited_for_readers_too() -> None:
    """2026-10-22: revisions came back in the academic register the edit had removed."""
    from app.domain.contracts import ValidationFailure, ValidationReport

    failed = ValidationReport(
        passed=False,
        citations_total=2,
        citations_resolved=2,
        best_evidence_grade=StudyType.META_ANALYSIS,
        failures=[ValidationFailure(code="unsourced_number", message="a number")],
    )
    ctx = _context().model_copy(
        update={
            "revise_round": 1,
            "draft": DRAFT,
            "synthesis_input": PAYLOAD,
            "validation": failed,
        }
    )
    client = Client({BEAT_2.text: PLAIN_BEAT_2})
    out = await Stage(client, reader_edit=True).run(ctx)  # type: ignore[arg-type]

    assert out.draft is not None and out.draft.body.beat_2_evidence == PLAIN_BEAT_2
    metrics = out.metrics["synthesize"]
    assert metrics["revision"] is True and metrics["reader_edit"]["kept"] >= 1


# --- the targeted repair after an unsourced number ----------------------------

BAD_SECTION = SECTION.replace("(ES = 0.823)", "(ES = 0.9)")
FIXED_SECTION = (
    "A 2022 meta-analysis of 11 studies found a moderate effect for foam rolling "
    "training over more than 4 weeks [S2]."
)


def _failed_on_the_section() -> PipelineContext:
    from app.domain.contracts import ValidationFailure, ValidationReport

    draft = DRAFT.model_copy(
        update={
            "body": DRAFT.body.model_copy(
                update={"sections": [ArticleSection(heading="Long-term", body=BAD_SECTION)]}
            )
        }
    )
    # Built by the real number check, whose sentence has its markers stripped:
    # a hand-written failure kept "[S2]" and hid that the repair never matched.
    from app.evidence.numbers import unsupported

    ((sentence, handles, missing, elsewhere),) = unsupported(draft, SOURCES)
    failed = ValidationReport(
        passed=False,
        citations_total=2,
        citations_resolved=2,
        best_evidence_grade=StudyType.META_ANALYSIS,
        failures=[
            ValidationFailure(
                code="unsourced_number",
                message="gives 0.9 and cites S2, but S2 does not contain it; remove it",
                detail={
                    "sentence": sentence,
                    "handles": handles,
                    "numbers": missing,
                    "found_in": elsewhere,
                },
            )
        ],
    )
    return _context().model_copy(
        update={
            "revise_round": 1,
            "draft": draft,
            "synthesis_input": PAYLOAD,
            "validation": failed,
        }
    )


class Repairing(Client):
    """Answers the repair with ``answers`` in turn; counts whole-article rewrites."""

    def __init__(self, answers: list[str]) -> None:
        super().__init__()
        self._answers = answers
        self.rewrites = 0

    async def synthesize(
        self, payload: SynthesisInput, *, feedback: str | None = None
    ) -> LLMResult[SynthesisOutput]:
        self.rewrites += 1
        return await super().synthesize(payload, feedback=feedback)

    async def edit_for_readers(self, payload: ReaderEditInput) -> LLMResult[ReaderEditOutput]:
        if payload.paragraph != BAD_SECTION:
            return await super().edit_for_readers(payload)
        self.edits.append(payload)
        return LLMResult(
            output=ReaderEditOutput(sentences=[self._answers.pop(0)]),
            usage=TokenUsage(),
            model="x",
        )


async def test_an_unsourced_number_is_repaired_in_its_paragraph_and_the_rest_kept() -> None:
    """2026-10-23: the whole-article revision cut a 12-source draft to 5."""
    client = Repairing([FIXED_SECTION])
    ctx = _failed_on_the_section()
    out = await Stage(client, reader_edit=True).run(ctx)  # type: ignore[arg-type]

    assert client.rewrites == 0
    assert out.draft is not None and out.draft.body.sections[0].body == FIXED_SECTION
    assert out.draft.body.beat_2_evidence == BEAT_2.text, "untouched paragraphs stay verbatim"
    assert out.metrics["synthesize"]["repair"] == {
        "paragraphs": 1,
        "calls": 1,
        "sentences_dropped": 0,
    }
    assert out.validation is None


async def test_a_refused_repair_is_tried_again_with_its_neighbours_shown() -> None:
    client = Repairing([BAD_SECTION, FIXED_SECTION])
    out = await Stage(client, reader_edit=True).run(_failed_on_the_section())  # type: ignore[arg-type]

    first, second = client.edits
    assert first.earlier == [] and second.earlier == [BEAT_2.text]
    assert "still gives 0.9 beside S2" in (second.feedback or "")
    assert DRAFT.body.beat_3_bottom_line in (second.feedback or "")
    assert out.draft is not None and out.draft.body.sections[0].body == FIXED_SECTION


async def test_a_repair_that_fails_twice_falls_back_to_the_whole_article_revision() -> None:
    client = Repairing([BAD_SECTION, BAD_SECTION])
    out = await Stage(client, reader_edit=True).run(_failed_on_the_section())  # type: ignore[arg-type]

    assert client.rewrites == 1
    assert "still gives 0.9" in out.metrics["synthesize"]["repair_failed"]


def test_a_repair_may_move_only_the_citations_its_failure_named() -> None:
    from app.pipeline.steps.reader_edit import repair_rejection

    part = Part("section", BAD_SECTION, 8, "section_0")
    moved = FIXED_SECTION.replace("[S2]", "[S1]")
    assert "did not name (S1)" in (
        repair_rejection(part, moved, SOURCES, {"S2"}, {"0.9"}) or ""
    )
    assert repair_rejection(part, FIXED_SECTION, SOURCES, {"S2"}, {"0.9"}) is None


def test_a_repair_may_not_keep_the_number_by_dropping_its_citation() -> None:
    """The number check reads only cited sentences, so an uncited 0.9 would pass it."""
    from app.pipeline.steps.reader_edit import repair_rejection

    part = Part("section", f"Rolling helped [S2]. {BAD_SECTION}", 8, "section_0")
    uncited = f"Rolling helped [S2]. {BAD_SECTION.removesuffix(' [S2].')}."
    reason = repair_rejection(part, uncited, SOURCES, {"S2"}, {"0.9"})
    assert reason is not None and "kept 0.9 in a sentence that cites nothing" in reason


async def test_a_sentence_repeated_in_two_paragraphs_is_repaired_in_both() -> None:
    """2026-10-23: one failing sentence sat in two paragraphs; fixing the first
    left the article failing validation on the second."""
    from app.domain.contracts import ValidationFailure, ValidationReport
    from app.evidence.numbers import unsupported

    draft = DRAFT.model_copy(
        update={
            "body": DRAFT.body.model_copy(
                update={
                    "sections": [
                        ArticleSection(heading="Long-term", body=BAD_SECTION),
                        ArticleSection(heading="Again", body=BAD_SECTION),
                    ]
                }
            )
        }
    )
    failures = [
        ValidationFailure(
            code="unsourced_number",
            message="gives 0.9 and cites S2, but S2 does not contain it; remove it",
            detail={"sentence": s, "handles": h, "numbers": m, "found_in": e},
        )
        for s, h, m, e in unsupported(draft, SOURCES)
    ]
    report = ValidationReport(
        passed=False,
        citations_total=2,
        citations_resolved=2,
        best_evidence_grade=StudyType.META_ANALYSIS,
        failures=failures,
    )
    ctx = _context().model_copy(
        update={
            "revise_round": 1,
            "draft": draft,
            "synthesis_input": PAYLOAD,
            "validation": report,
        }
    )
    client = Repairing([FIXED_SECTION, FIXED_SECTION])
    out = await Stage(client, reader_edit=True).run(ctx)  # type: ignore[arg-type]

    assert out.draft is not None
    assert [s.body for s in out.draft.body.sections] == [FIXED_SECTION, FIXED_SECTION]
    assert out.metrics["synthesize"]["repair"]["paragraphs"] == 2


def test_the_reader_edit_prompt_lets_a_repair_note_remove_a_number() -> None:
    from app.llm.prompts.reader_edit import READER_EDIT_SYSTEM_PROMPT

    assert "that note wins" in READER_EDIT_SYSTEM_PROMPT


async def test_a_number_the_model_will_not_drop_costs_its_sentence_not_the_article() -> None:
    """2026-10-23: Bonsai twice kept "1.97 million", which no source holds, and the
    whole-article rewrite that followed cut a 12-source draft to 5."""
    from app.domain.contracts import ValidationFailure, ValidationReport
    from app.evidence.numbers import unsupported

    two = f"Rolling helped over more than 4 weeks [S2]. {BAD_SECTION}"
    draft = DRAFT.model_copy(
        update={
            "body": DRAFT.body.model_copy(
                update={"sections": [ArticleSection(heading="Long-term", body=two)]}
            )
        }
    )
    ((sentence, handles, missing, elsewhere),) = unsupported(draft, SOURCES)
    report = ValidationReport(
        passed=False,
        citations_total=2,
        citations_resolved=2,
        best_evidence_grade=StudyType.META_ANALYSIS,
        failures=[
            ValidationFailure(
                code="unsourced_number",
                message="gives 0.9; remove it",
                detail={
                    "sentence": sentence,
                    "handles": handles,
                    "numbers": missing,
                    "found_in": elsewhere,
                },
            )
        ],
    )
    ctx = _context().model_copy(
        update={
            "revise_round": 1,
            "draft": draft,
            "synthesis_input": PAYLOAD,
            "validation": report,
        }
    )

    class Stubborn(Repairing):
        async def edit_for_readers(
            self, payload: ReaderEditInput
        ) -> LLMResult[ReaderEditOutput]:
            if payload.paragraph != two:
                return await super().edit_for_readers(payload)
            self.edits.append(payload)
            return LLMResult(
                output=ReaderEditOutput(sentences=[two]), usage=TokenUsage(), model="x"
            )

    client = Stubborn([])
    out = await Stage(client, reader_edit=True).run(ctx)  # type: ignore[arg-type]

    assert client.rewrites == 0
    assert out.draft is not None
    assert out.draft.body.sections[0].body == "Rolling helped over more than 4 weeks [S2]."
    assert out.metrics["synthesize"]["repair"]["sentences_dropped"] == 1
