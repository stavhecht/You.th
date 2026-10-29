"""Checks 6 and 7 of validation, and the sentence count beneath every beat limit.

Each was added after the evaluation measured the failure it catches; the
docstrings in ``evidence/numbers.py`` and ``evidence/validation.py`` carry the
numbers. These pin the rules, including the cases the rules must *not* catch,
since a false positive here fails a correct draft.
"""

from __future__ import annotations

import pytest

from app.domain.contracts import (
    ArticleBody,
    PromptSource,
    SynthesisInput,
    SynthesisOutput,
    count_sentences,
)
from app.domain.enums import StudyType, Verdict
from app.evidence.validation import (
    check_no_evidence_cites_nothing,
    check_numbers_are_sourced,
)
from app.llm.base import ExtractionClient, LLMError
from app.pipeline.graph import REVISABLE_FAILURES
from app.pipeline.stages import PipelineContext, StageError
from app.pipeline.steps.extract import ExtractStage
from app.services.no_evidence import no_evidence_draft


def _source(handle: str, abstract: str, year: int = 2020) -> PromptSource:
    return PromptSource(
        handle=handle,
        title=f"Study {handle}",
        abstract=abstract,
        journal=None,
        year=year,
        study_type=StudyType.META_ANALYSIS,
        source_id=f"src-{handle}",
        claims=["improves endurance"],
    )


PAYLOAD = SynthesisInput(
    product="Caffeine",
    target_claims=["improves endurance"],
    sources=[
        _source("S1", "Caffeine improved time-trial performance by 2.9% over placebo."),
        _source("S2", "Caffeine with carbohydrate gave an effect size of 0.26 (0.15 to 0.38)."),
        _source("S3", "Twelve weeks of caffeine in 24 cyclists improved power by 27.9 W."),
    ],
)


def _draft(beat_2: str, verdict: Verdict = Verdict.MIXED) -> SynthesisOutput:
    return SynthesisOutput(
        headline="Caffeine and endurance",
        verdict=verdict,
        summary="Some trials found a small effect.",
        body=ArticleBody(
            beat_1_claim="Caffeine is said to improve endurance.",
            beat_2_evidence=beat_2,
            beat_3_bottom_line="The effect is small.",
        ),
    )


def test_a_number_cited_to_the_wrong_source_fails_and_names_the_right_one() -> None:
    failures = check_numbers_are_sourced(
        _draft("A meta-analysis found an effect size of 0.26 (0.15 to 0.38) [S1]."), PAYLOAD
    )
    assert [f.code for f in failures] == ["unsourced_number"]
    assert failures[0].detail["numbers"] == ["0.26", "0.15", "0.38"]
    assert failures[0].detail["found_in"] == ["S2"]
    assert "cite that source" in failures[0].message


def test_a_number_in_no_source_is_told_to_go() -> None:
    failures = check_numbers_are_sourced(_draft("Performance rose by 87% [S1]."), PAYLOAD)
    assert failures[0].detail["found_in"] == []
    assert "remove it" in failures[0].message


def test_numbers_from_the_cited_source_pass_including_roundings_and_words() -> None:
    draft = _draft(
        "Performance improved by 2.9% [S1]. Power rose by about 28 W over 12 weeks in 24 "
        "cyclists [S3]."
    )
    assert check_numbers_are_sourced(draft, PAYLOAD) == []


def test_counts_of_sources_are_not_checked() -> None:
    draft = _draft("Three trials and 2 meta-analyses found a small effect [S1, S2].")
    assert check_numbers_are_sourced(draft, PAYLOAD) == []


def test_a_marker_after_the_full_stop_belongs_to_the_sentence_before_it() -> None:
    wrong = _draft("Performance rose by 2.9%. [S2] Another trial disagreed [S3].")
    assert [f.detail["handles"] for f in check_numbers_are_sourced(wrong, PAYLOAD)] == [["S2"]]


def test_an_uncited_sentence_is_not_this_checks_business() -> None:
    assert check_numbers_are_sourced(_draft("Most people drink 200 mg a day."), PAYLOAD) == []


def test_a_no_evidence_verdict_that_cites_sources_fails() -> None:
    draft = _draft("No trial studied this directly [S1].", verdict=Verdict.NO_EVIDENCE)
    assert [f.code for f in check_no_evidence_cites_nothing(draft)] == [
        "no_evidence_with_citations"
    ]


def test_the_no_evidence_template_still_passes() -> None:
    template = no_evidence_draft("Zorbanol", ["improves focus"])
    assert check_no_evidence_cites_nothing(template) == []


def test_both_new_failures_are_sent_back_for_one_revision() -> None:
    assert {"unsourced_number", "no_evidence_with_citations"} <= REVISABLE_FAILURES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("One fell. [S1] Two rose. [S2]", 2),
        ("One fell [S1]. Two rose [S2, S3].", 2),
        ("A. B. C. D. E. [S5]", 5),
    ],
)
def test_a_trailing_citation_is_not_a_sentence(text: str, expected: int) -> None:
    """A correct five-sentence beat ending ". [S5]" used to count as six and
    fail the whole run."""
    assert count_sentences(text) == expected


class _Unreachable:
    def __init__(self, retryable: bool) -> None:
        self._retryable = retryable

    async def extract(self, payload: object) -> object:
        raise LLMError("extraction call failed", retryable=self._retryable)


@pytest.mark.parametrize("retryable", [True, False])
async def test_extraction_passes_on_whether_its_failure_is_transient(retryable: bool) -> None:
    stage = ExtractStage(_Unreachable(retryable))  # type: ignore[arg-type]
    with pytest.raises(StageError) as exc_info:
        await stage.run(PipelineContext(run_id="r", topic="caffeine"))
    assert exc_info.value.retryable is retryable


def test_the_extraction_protocol_is_unchanged() -> None:
    assert hasattr(ExtractionClient, "extract")


class _Flaky:
    """Breaks the contract on the first ``failures`` calls, then writes a draft."""

    def __init__(self, failures: int, retryable: bool = False) -> None:
        self._failures = failures
        self._retryable = retryable
        self.calls = 0

    async def synthesize(self, payload: SynthesisInput, *, feedback: str | None = None):  # type: ignore[no-untyped-def]
        from app.llm.base import LLMResult, TokenUsage

        self.calls += 1
        if self.calls <= self._failures:
            raise LLMError("a section heading is over 8 words", retryable=self._retryable)
        return LLMResult(output=_draft("Performance rose by 2.9% [S1]."), usage=TokenUsage())


def _synth_ctx() -> PipelineContext:
    from app.domain.contracts import CandidatePaper, ExtractionOutput, RankedSource
    from app.domain.enums import SourceApi

    paper = CandidatePaper(
        pmid="1",
        title="Study S1",
        abstract=PAYLOAD.sources[0].abstract,
        study_type=StudyType.META_ANALYSIS,
        source_api=SourceApi.PUBMED,
    )
    ranked = RankedSource(
        source_id="src-S1",
        claim="improves endurance",
        citation_handle="S1",
        paper=paper,
        cosine_similarity=0.9,
        final_score=1.0,
    )
    return PipelineContext(
        run_id="r", topic="caffeine",
        extraction=ExtractionOutput(product="Caffeine", target_claims=["improves endurance"]),
        ranked={"improves endurance": [ranked]},
    )  # fmt: skip


async def test_a_first_draft_that_breaks_the_contract_gets_one_fresh_draft() -> None:
    from app.pipeline.steps.synthesize import SynthesizeStage

    client = _Flaky(failures=1)
    ctx = await SynthesizeStage(client).run(_synth_ctx())  # type: ignore[arg-type]
    assert client.calls == 2
    assert ctx.draft is not None
    assert ctx.metrics["synthesize"]["fresh_draft_after_contract_failure"] is True


async def test_a_draft_over_its_sentence_caps_is_fitted_instead_of_redrawn() -> None:
    """2026-10-28: 41 calls in a week broke the contract after the repair turn, nearly
    all a section or beat 2 over its cap or a long headline. Each cost a fresh draft
    or the run (wt_019); every sentence and its citation now stays, in a new section."""
    import json

    from app.pipeline.steps.synthesize import SynthesizeStage, fit_to_contract

    beat_2 = " ".join(f"Trial {i} found endurance rose [S1]." for i in range(1, 8))
    section = " ".join(f"Study {i} found time to exhaustion rose [S1]." for i in range(1, 11))
    raw = _draft(beat_2="x").model_dump(mode="json")
    raw["headline"] = (
        "Caffeine and endurance: what fourteen recent trials say about "
        "cycling, rowing and running"
    )
    raw["body"]["beat_2_evidence"] = beat_2
    raw["body"]["sections"] = [{"heading": "Longer events", "body": section}]

    class _Overflowing(_Flaky):
        async def synthesize(self, payload, *, feedback=None):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise LLMError("still failed validation", content=json.dumps(raw))

    client = _Overflowing(failures=0)
    ctx = await SynthesizeStage(client).run(_synth_ctx())  # type: ignore[arg-type]
    assert client.calls == 1 and ctx.draft is not None
    body = ctx.draft.body
    assert ctx.draft.headline == "Caffeine and endurance"
    assert [s.heading for s in body.sections] == [
        "What other studies found", "Longer events", "Longer events, continued"
    ]
    assert body.as_text().count("[S1]") == 17
    assert len(ctx.metrics["synthesize"]["contract_fit"]) == 3

    # Not a length problem, or nothing to cut a headline at: a fresh draft, as before.
    raw["headline"] = " ".join(["word"] * 13)
    assert fit_to_contract(json.dumps(raw)) is None
    assert fit_to_contract("not json") is None


async def test_a_second_contract_failure_still_fails_the_run_and_only_once() -> None:
    from app.pipeline.steps.synthesize import SynthesizeStage

    client = _Flaky(failures=5)
    with pytest.raises(StageError) as exc_info:
        await SynthesizeStage(client).run(_synth_ctx())  # type: ignore[arg-type]
    assert client.calls == 2
    assert exc_info.value.retryable is False


async def test_an_outage_is_not_redrafted_but_requeued() -> None:
    from app.pipeline.steps.synthesize import SynthesizeStage

    client = _Flaky(failures=1, retryable=True)
    with pytest.raises(StageError) as exc_info:
        await SynthesizeStage(client).run(_synth_ctx())  # type: ignore[arg-type]
    assert client.calls == 1
    assert exc_info.value.retryable is True


def test_a_p_value_written_without_its_leading_zero_matches() -> None:
    """Abstracts write "p = .005"; drafts write "p = 0.005". Measured: this
    failed a correctly cited draft before bare decimals were read as decimals."""
    payload = SynthesisInput(
        product="Ashwagandha",
        target_claims=["reduces stress"],
        sources=[_source("S1", "Stress fell (SMD -1.55; p = .005; I2 = 93.8%).")],
    )
    draft = _draft("Stress fell (SMD -1.55, p = 0.005, I2 = 93.8%) [S1].")
    assert check_numbers_are_sourced(draft, payload) == []


async def test_a_no_evidence_draft_citing_studies_fails_first_then_warns() -> None:
    """A refuted claim honestly reads "no evidence" while citing the trials that
    found no effect, so only the first draft is blocked; a rewrite that keeps
    the pairing passes, and the reviewer sees why it was flagged."""
    from app.evidence.validation import validate_draft
    from tests.conftest import FakeSession, FakeSource

    draft = _draft("Trials found no kidney harm [S1].", verdict=Verdict.NO_EVIDENCE)
    session = FakeSession([FakeSource("src-S1")])

    first = await validate_draft(session, draft, PAYLOAD)  # type: ignore[arg-type]
    last = await validate_draft(session, draft, PAYLOAD, final_round=True)  # type: ignore[arg-type]

    assert [f.code for f in first.failures] == ["no_evidence_with_citations"]
    assert last.passed
    assert "no_evidence_with_citations" in [w.code for w in last.warnings]


def test_a_revision_is_shown_the_draft_it_is_correcting() -> None:
    from app.domain.contracts import ValidationFailure, ValidationReport
    from app.pipeline.steps.synthesize import revision_feedback

    report = ValidationReport(
        passed=False,
        citations_total=1,
        citations_resolved=1,
        best_evidence_grade=StudyType.META_ANALYSIS,
        failures=[ValidationFailure(code="unsourced_number", message="0.26 is not in S1")],
    )
    draft = _draft("The effect size was 0.26 [S1].")
    note = revision_feedback(report, draft)
    assert "The effect size was 0.26 [S1]." in note
    assert "0.26 is not in S1" in note
    assert "copy every sentence" in note


def test_a_spelled_out_compound_number_in_a_source_is_read_whole() -> None:
    """2026-10-15: S12 said "Twenty-six trials", the draft "26 high-quality trials"."""
    from app.evidence.numbers import source_numbers

    assert 26 in source_numbers("Twenty-six trials with high methodological quality.")
    assert 43 in source_numbers("forty three adults")


def test_a_hyphenated_word_before_the_count_noun_still_counts_sources() -> None:
    from app.evidence.numbers import checked_numbers

    assert checked_numbers("A meta-analysis of 26 high-quality trials found an effect.") == []
    assert checked_numbers("It pooled 26 well-designed randomised trials.") == []
    assert checked_numbers("Range of motion rose by 26 degrees.") == ["26"]
    # A duration is checked, not mistaken for a count of trials.
    assert checked_numbers("Grip improved in a 12-week trial of 22 players.") == ["12", "22"]
    assert checked_numbers("Grip improved in a 12 week trial of 22 players.") == ["12", "22"]


def test_a_cited_sentence_sharing_few_words_with_its_source_is_flagged() -> None:
    """2026-10-24, mp_001: a creatine sentence about muscle soreness cited to a
    source that never mentions it, which no number check can see."""
    from app.domain.contracts import ArticleBody, SynthesisInput, SynthesisOutput
    from app.domain.enums import Verdict
    from app.evidence.validation import check_sentences_share_source_words

    source = PromptSource(
        handle="S2",
        title="Creatine and strength",
        abstract="Creatine supplementation "
        "increased muscle strength and lean mass in resistance-trained adults.",
        journal=None,
        year=2020,
        study_type=StudyType.META_ANALYSIS,
        source_id="id-S2",
        claims=["increases strength"],
    )
    payload = SynthesisInput(product="creatine", target_claims=["x"], sources=[source])

    def draft(beat_2: str) -> SynthesisOutput:
        return SynthesisOutput(
            headline="Creatine and strength",
            verdict=Verdict.WEAK,
            summary="Summary.",
            body=ArticleBody(
                beat_1_claim="Claim.", beat_2_evidence=beat_2, beat_3_bottom_line="Bottom line."
            ),
            citations=[],
        )

    wrong = (
        "Creatine did not change soreness, range of motion or inflammation after damage [S2]."
    )
    right = (
        "Creatine increased muscle strength and lean mass, according to a meta-analysis "
        "(a study that pools the results of earlier trials) [S2]."
    )
    assert check_sentences_share_source_words(draft(wrong), payload)[0].code == (
        "low_source_overlap"
    )
    assert check_sentences_share_source_words(draft(right), payload) == []


def test_a_finding_cited_to_its_neighbour_is_sent_back_naming_the_right_source() -> None:
    """wt_007, 2026-10-25: the opioid trial's finding cited to a memory study, which
    happened to hold the number ("21 highlighted words"), so no number check saw it."""
    from app.evidence.validation import check_citations_point_at_best_source

    payload = SynthesisInput(
        product="vagus nerve stimulation",
        target_claims=["reduces anxiety"],
        sources=[
            _source("S12", "Stimulation reduced distress and withdrawal in patients with "
                    "opioid use disorder in a randomised pilot trial."),
            _source("S13", "Patients memorized 21 highlighted words under three stimulation "
                    "conditions in a cross-over study of verbal memory."),
        ],
    )
    sentence = (
        "A pilot trial of 21 people with opioid use disorder found reductions in distress "
        "after stimulation"
    )
    failures = check_citations_point_at_best_source(_draft(f"{sentence} [S13]."), payload)
    assert [f.code for f in failures] == ["misattributed_citation"]
    assert failures[0].detail["found_in"] == ["S12"]
    assert "misattributed_citation" in REVISABLE_FAILURES
    assert check_citations_point_at_best_source(_draft(f"{sentence} [S12]."), payload) == []

    # 2026-10-28, wellness run: 4 of 5 judged hallucinations were repairs that kept or
    # introduced exactly this, which the final round only warns about.
    from app.pipeline.steps.reader_edit import Part, repair_rejection

    plain = sentence.replace("of 21 people ", "in people ")
    part = Part("section", f"{plain} [S12].", 8, "section_0")
    reason = repair_rejection(part, f"{plain} [S13].", payload.sources, {"S12", "S13"}, set())
    assert reason is not None and "worded like S12" in reason
    assert repair_rejection(part, f"{plain} [S12].", payload.sources, {"S12"}, set()) is None
