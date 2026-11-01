"""The statistical-shorthand warning (``evidence/readability.py``).

The flagged sentences are from the first Ternary-Bonsai article through the
real queue (foam rolling, 2026-10-15). The cases it must not flag matter as
much: a false positive puts a wrong warning in front of a reviewer on every
article.
"""

from __future__ import annotations

from app.domain.contracts import ArticleBody, ArticleSection, SynthesisOutput
from app.domain.enums import Verdict
from app.evidence.readability import statistical_shorthand
from app.evidence.validation import check_reader_shorthand
from app.pipeline.graph import REVISABLE_FAILURES


def _draft(beat_2: str, section: str = "Two trials agreed [S1].") -> SynthesisOutput:
    return SynthesisOutput(
        headline="Foam rolling and range of motion",
        verdict=Verdict.MIXED,
        summary="Some trials found a short-lived effect.",
        body=ArticleBody(
            beat_1_claim="Foam rolling is said to loosen muscles.",
            beat_2_evidence=beat_2,
            sections=[ArticleSection(heading="How long it lasts", body=section)],
            beat_3_bottom_line="The effect is real but short-lived.",
        ),
        citations=[],
    )


def test_the_reference_articles_notation_is_found() -> None:
    draft = _draft(
        "A 2020 meta-analysis of 26 trials found a large acute increase in range of motion "
        "(SMD 0.74, 95% CI 0.42 to 1.01) [S12].",
        "A 2022 meta-analysis found no significant difference (ES = 0.079, P = 0.39) [S11]. "
        "A 2020 meta-analysis reported a large effect (d = 0.76) [S1].",
    )
    assert statistical_shorthand(draft) == ["SMD 0.74", "CI 0.42", "ES = 0.079", "P = 0.39",
                                            "d = 0.76"]


def test_ratios_and_p_thresholds_are_found() -> None:
    draft = _draft("Risk was lower (RR 0.80; p < .05) and odds too (OR=0.7) [S1].")
    assert statistical_shorthand(draft) == ["RR 0.80", "p < .05", "OR=0.7"]


def test_plain_writing_is_not_flagged() -> None:
    draft = _draft(
        "Rolling loosened muscles right away: pooling 26 trials in a meta-analysis found a "
        "large effect (effect size 0.74, a standard score of how big a difference is) [S12]. "
        "Rolling or stretching worked about equally well, with no clear difference [S11].",
        "Doctors (MD) and physiotherapists ran 2 trials of 40 people over 4 weeks; "
        "95% of people finished [S1, S2].",
    )
    assert statistical_shorthand(draft) == []


def test_it_is_a_warning_that_never_sends_a_draft_back() -> None:
    [warning] = check_reader_shorthand(_draft("A large effect (SMD 0.74) [S1]."))
    assert warning.code == "statistical_shorthand"
    assert "SMD 0.74" in warning.message
    assert warning.code not in REVISABLE_FAILURES
    assert check_reader_shorthand(_draft("A large effect [S1].")) == []
