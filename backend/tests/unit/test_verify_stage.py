"""VERIFY: a model reads each cited paper against the sentences that cite it.

Warnings only, like APPRAISE: it never fails a run and never blocks publishing.
What it adds is a reading, per sentence, of whether the paper says what the
article says it says. The lexical checks in VALIDATE cannot see a sentence that
uses the source's words for a claim the source does not make.
"""

from __future__ import annotations

from typing import Any

from app.domain.contracts import SupportInput, SupportItem, SupportOutput, ValidationReport
from app.domain.enums import StudyType
from app.llm.base import LLMError, LLMResult, TokenUsage
from app.llm.prompts.support import build_support_user_prompt
from app.pipeline.stages import PipelineContext, StageName
from app.pipeline.steps.verify import VerifyStage
from tests.conftest import make_draft, make_prompt_source

ABSTRACT = (
    "In 60 adults, ashwagandha lowered evening cortisol by 23% against placebo. "
    "Sleep quality did not change."
)


class _Reader:
    """Answers from a table of (handle, sentence number) -> (verdict, quote)."""

    def __init__(
        self,
        answers: dict[tuple[str, int], tuple[str, str]],
        fail: frozenset[str] = frozenset(),
    ) -> None:
        self.answers = answers
        self.fail = fail
        self.seen: list[SupportInput] = []

    async def support(self, payload: SupportInput) -> LLMResult[SupportOutput]:
        self.seen.append(payload)
        if payload.handle in self.fail:
            raise LLMError("server went away", usage=TokenUsage(input_tokens=5), model="m")
        # Every sentence gets a reading, as a real answer must: the contract
        # refuses an empty list. Unlisted ones read as supported by the abstract.
        items = [
            SupportItem(
                sentence=number,
                quote=self.answers.get((payload.handle, number), ("", ABSTRACT))[1],
                verdict=self.answers.get(  # type: ignore[arg-type]
                    (payload.handle, number), ("supported", "")
                )[0],
            )
            for number in range(1, len(payload.sentences) + 1)
        ]
        return LLMResult(
            output=SupportOutput(items=items), usage=TokenUsage(input_tokens=10), model="m"
        )


def _ctx(beat_2: str, *, passed: bool = True) -> PipelineContext:
    from app.domain.contracts import SynthesisInput

    sources = [make_prompt_source("S1", "src-1"), make_prompt_source("S2", "src-2")]
    for source in sources:
        source.abstract = ABSTRACT
    return PipelineContext(
        run_id="r1",
        topic="ashwagandha for stress",
        draft=make_draft(beat_2=beat_2),
        synthesis_input=SynthesisInput(
            product="Ashwagandha", target_claims=["reduces cortisol"], sources=sources
        ),
        validation=ValidationReport(
            passed=passed,
            citations_total=1,
            citations_resolved=1,
            best_evidence_grade=StudyType.RCT,
            failures=[],
        ),
    )


async def _run(stage: VerifyStage, ctx: PipelineContext) -> PipelineContext:
    return await stage.run(ctx)


async def test_a_sentence_its_paper_does_not_support_becomes_a_warning() -> None:
    reader = _Reader(
        {
            ("S1", 1): (
                "supported",
                "ashwagandha lowered evening cortisol by 23% against placebo.",
            ),
            ("S1", 2): ("not_supported", "none"),
        }
    )
    ctx = _ctx(
        "Ashwagandha lowered evening cortisol by 23% [S1]. "
        "It also improved sleep quality [S1]."
    )

    out = await _run(VerifyStage(reader), ctx)

    assert out.validation is not None
    codes = [w.code for w in out.validation.warnings]
    assert codes == ["citation_not_supported"]
    assert "improved sleep quality" in out.validation.warnings[0].message
    assert out.validation.passed  # a reading, never a block
    metrics = out.metrics[StageName.VERIFY]
    assert (metrics["cause"], metrics["calls"], metrics["not_supported"]) == (None, 1, 1)
    assert out.usage_by_stage[StageName.VERIFY].usage.input_tokens == 10


async def test_one_call_per_cited_paper_with_only_the_sentences_citing_it() -> None:
    reader = _Reader({})
    ctx = _ctx("Cortisol fell [S1]. Cortisol fell in both [S1][S2]. No source here.")

    await _run(VerifyStage(reader), ctx)

    by_handle = {payload.handle: payload.sentences for payload in reader.seen}
    assert by_handle == {
        "S1": ["Cortisol fell.", "Cortisol fell in both."],
        "S2": ["Cortisol fell in both."],
    }


def test_the_prompt_numbers_the_sentences_and_carries_the_abstract() -> None:
    prompt = build_support_user_prompt(
        SupportInput(
            handle="S1", title="A trial", abstract=ABSTRACT, sentences=["One.", "Two."]
        )
    )
    assert "1. One." in prompt and "2. Two." in prompt and "23%" in prompt


async def test_a_supported_verdict_whose_quote_is_not_in_the_paper_is_not_believed() -> None:
    """The quote is the anchor: a sentence the model made up proves nothing."""
    reader = _Reader({("S1", 1): ("supported", "Ashwagandha cured insomnia in everyone.")})
    out = await _run(VerifyStage(reader), _ctx("It cured insomnia [S1]."))

    assert out.validation is not None
    assert [w.code for w in out.validation.warnings] == ["citation_partly_supported"]
    assert out.metrics[StageName.VERIFY]["unanchored"] == 1


async def test_it_never_fails_a_run() -> None:
    off = await _run(VerifyStage(None), _ctx("Cortisol fell [S1]."))
    assert off.metrics[StageName.VERIFY]["cause"] == "disabled"

    failed = _ctx("Cortisol fell [S1].", passed=False)
    failed_draft = await _run(VerifyStage(_Reader({})), failed)
    assert failed_draft.metrics[StageName.VERIFY]["cause"] == "not_passed"

    reader = _Reader({}, fail=frozenset({"S1"}))
    broken = await _run(VerifyStage(reader), _ctx("Cortisol fell [S1]."))
    metrics = broken.metrics[StageName.VERIFY]
    assert metrics["cause"] == "model_error"
    # A failed call is billed like a successful one, so its tokens are kept.
    assert broken.usage_by_stage[StageName.VERIFY].usage.input_tokens == 5

    class _Bug:
        async def support(self, payload: Any) -> Any:
            raise ValueError("a bug")

    buggy = await _run(VerifyStage(_Bug()), _ctx("Cortisol fell [S1]."))  # type: ignore[arg-type]
    assert buggy.metrics[StageName.VERIFY]["cause"] == "unexpected"


def test_verify_runs_after_validate_and_persist_stays_last() -> None:
    order = list(StageName)
    assert order.index(StageName.VALIDATE) < order.index(StageName.VERIFY)
    assert order[-1] is StageName.PERSIST


async def test_it_reads_at_most_a_capped_number_of_papers_the_most_cited_first(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """One call per paper, sequential, on a worker that runs one job at a time:
    an article citing 24 papers added ~40 minutes. The papers left unread are
    named in the metrics, so a short check is never mistaken for a full one."""
    from app.pipeline.steps import verify

    monkeypatch.setattr(verify, "MAX_SOURCES", 1)
    reader = _Reader({})
    ctx = _ctx("Cortisol fell [S2]. Sleep improved [S1]. Mood lifted [S1].")

    out = await _run(VerifyStage(reader), ctx)

    assert [payload.handle for payload in reader.seen] == ["S1"]  # cited twice
    assert out.metrics[StageName.VERIFY]["unchecked_sources"] == ["S2"]
