"""A verdict badge must not say more than the article's own conclusion.

Measured 2026-11-06: 4 of the pilot's 6 wrong verdicts were ``mixed`` above a bottom
line saying the research does not back the claim (wt_011, wt_055, wt_082, wt_095).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.contracts import ValidationFailure, ValidationReport
from app.domain.enums import StudyType, Verdict
from app.evidence.validation import claim_not_backed
from app.pipeline.steps import persist
from app.pipeline.steps.persist import ValidateStage
from app.pipeline.steps.synthesize import SynthesizeStage
from tests.unitTest.test_synthesis_coverage import _context, _draft

BACKED = "Trials in adults found a clear benefit for sleep."
NOT_BACKED = "The research does not support the claim that it improves sleep."


def _with_text(
    bottom_line: str, verdict: Verdict = Verdict.MIXED, summary: str = BACKED
) -> Any:
    draft = _draft(cites=2)
    body = draft.body.model_copy(update={"beat_3_bottom_line": bottom_line})
    return draft.model_copy(update={"verdict": verdict, "summary": summary, "body": body})


@pytest.mark.parametrize(
    "text",
    [
        NOT_BACKED,
        "The claim is not supported by these trials.",
        "It doesn't support that claim.",
        "It doesn\u2019t support that claim.",
        "Healing has not been proven.",
        "There is no clear benefit in healthy adults.",
        "The evidence is not strong enough to say it works.",
    ],
)
def test_a_conclusion_disowning_the_claim_is_found(text: str) -> None:
    assert claim_not_backed(_with_text(text)) == text.rstrip(".")


def test_it_reads_the_summary_too() -> None:
    assert claim_not_backed(_with_text(BACKED, summary=NOT_BACKED)) is not None


def test_a_conclusion_that_backs_the_claim_is_left_alone() -> None:
    assert claim_not_backed(_with_text(BACKED)) is None


def test_only_the_conclusion_is_read_not_the_body() -> None:
    """A section may report one negative trial in a body of positive ones."""
    draft = _with_text(BACKED)
    body = draft.body.model_copy(update={"beat_2_evidence": NOT_BACKED + " [S1]"})
    assert claim_not_backed(draft.model_copy(update={"body": body})) is None


def _report(*, passed: bool = True, warnings: list[ValidationFailure] | None = None) -> Any:
    return ValidationReport(
        passed=passed,
        citations_total=2,
        citations_resolved=2,
        best_evidence_grade=StudyType.RCT,
        verdict_ceiling=Verdict.SUPPORTED,
        failures=[] if passed else [ValidationFailure(code="uncited_beat", message="m")],
        warnings=warnings or [],
    )


async def _validate(
    monkeypatch: pytest.MonkeyPatch, draft: Any, *, passed: bool = True
) -> tuple[Any, list[Verdict]]:
    seen: list[Verdict] = []

    async def fake(_session: Any, output: Any, _payload: Any, **_: Any) -> ValidationReport:
        seen.append(output.verdict)
        return _report(passed=passed)

    monkeypatch.setattr(persist, "validate_draft", fake)
    context = _context(3).model_copy(update={"draft": draft})
    payload = SynthesizeStage._build_input(context)
    context = context.model_copy(update={"synthesis_input": payload})
    return await ValidateStage(session=None).run(context), seen  # type: ignore[arg-type]


@pytest.mark.parametrize("written", [Verdict.MIXED, Verdict.SUPPORTED])
async def test_a_passing_draft_is_lowered_and_the_reviewer_is_told(
    monkeypatch: pytest.MonkeyPatch, written: Verdict
) -> None:
    result, seen = await _validate(monkeypatch, _with_text(NOT_BACKED, written))

    assert result.draft.verdict is Verdict.WEAK
    assert seen == [written, Verdict.WEAK], "the warnings must describe the verdict stored"
    warning = next(w for w in result.validation.warnings if w.code == "verdict_lowered_by_text")
    assert str(written) in warning.message and NOT_BACKED.rstrip(".") in warning.message
    assert result.metrics["validate"]["verdict_lowered_from"] == str(written)


async def test_a_weak_verdict_is_not_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    result, seen = await _validate(monkeypatch, _with_text(NOT_BACKED, Verdict.WEAK))

    assert result.draft.verdict is Verdict.WEAK and len(seen) == 1
    assert result.metrics["validate"]["verdict_lowered_from"] is None


async def test_nothing_ever_raises_a_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = await _validate(monkeypatch, _with_text(NOT_BACKED, Verdict.NO_EVIDENCE))

    assert result.draft.verdict is Verdict.NO_EVIDENCE


async def test_a_failing_draft_keeps_its_verdict_for_the_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revision is shown the draft it corrects, so this would change its request."""
    result, seen = await _validate(monkeypatch, _with_text(NOT_BACKED), passed=False)

    assert result.draft.verdict is Verdict.MIXED and len(seen) == 1


async def test_a_conclusion_backing_the_claim_keeps_its_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, seen = await _validate(monkeypatch, _with_text(BACKED, Verdict.SUPPORTED))

    assert result.draft.verdict is Verdict.SUPPORTED and len(seen) == 1


@pytest.mark.parametrize(
    "text",
    [
        # wt_061, 2026-11-06: a split between studies is what ``mixed`` means.
        "Several trials found benefits, while others found no clear effect.",
        "Some reviews found a benefit and others found no clear benefit.",
    ],
)
def test_a_split_between_studies_is_not_a_conclusion_against_the_claim(text: str) -> None:
    assert claim_not_backed(_with_text(text)) is None


NO_HARM = "No clear evidence of harm was found in these trials."


@pytest.mark.parametrize(
    "text",
    [
        NO_HARM,
        "Research does not support worries that creatine harms healthy kidneys.",
        "Creatine has not been shown to damage the kidneys of healthy adults.",
        "No clear evidence of harm was found, and it appears safe.",
    ],
)
def test_finding_no_harm_backs_a_safety_claim(text: str) -> None:
    assert claim_not_backed(_with_text(text), ["creatine is safe for the kidneys"]) is None


def test_finding_no_harm_disowns_a_harm_claim() -> None:
    claims = ["damages the kidneys"]
    assert claim_not_backed(_with_text(NO_HARM), claims) == NO_HARM.rstrip(".")


@pytest.mark.parametrize(
    "text",
    [
        "There is insufficient evidence of harm to call it safe.",  # nobody knows
        "There is no clear evidence that it is safe.",  # names no harm
        "It has not been proven harmless for the kidneys.",  # "harmless" is no harm word
        "The research does not support that it is safe and free of side effects.",
    ],
)
def test_not_knowing_is_not_the_same_as_finding_it_safe(text: str) -> None:
    assert claim_not_backed(_with_text(text), ["melatonin is safe"]) == text.rstrip(".")


def test_a_safety_article_still_gets_its_other_conclusions_checked() -> None:
    text = f"{NO_HARM} {NOT_BACKED}"
    claims = ["is safe", "improves sleep"]
    assert claim_not_backed(_with_text(text), claims) == NOT_BACKED.rstrip(".")


def test_a_split_does_not_hide_a_conclusion_in_another_sentence() -> None:
    text = f"Some trials found a benefit, while others found no clear effect. {NOT_BACKED}"
    assert claim_not_backed(_with_text(text)) == NOT_BACKED.rstrip(".")


def test_a_decimal_point_does_not_end_the_quoted_sentence() -> None:
    text = "Gains averaged 0.5 kg, but the evidence is not strong enough."
    assert claim_not_backed(_with_text(text)) == text.rstrip(".")
