"""The evidence quorum — how *much* evidence, not just how good.

Invariant #3 asked one question for a long time: is the best cited source
strong enough for this verdict? A single randomised trial answers that
perfectly and still leaves the article claiming more than it should, because
one study is a finding rather than a conclusion. Real output showed it: an
article reached the review queue saying ``supported`` on two cited sources,
only one of which carried the grade that licensed it.

Two rules, both applied per claim:

* **``supported`` needs two sources at a supported-tier grade** (RCT,
  systematic review, meta-analysis). Two rather than one because
  classification here is heuristic and has been measurably wrong in both
  directions on real data — a second source means no single misgrading can
  license the strongest verdict by itself.
* **The article inherits its weakest claim's ceiling.** Counted article-wide,
  two claims each backed by one trial satisfy a quorum of two between them
  while neither is corroborated, and the finished article shows no trace of it.

Below ``supported`` nothing changes. Those ceilings already say "provisional",
and a quorum there would demote thin evidence that is being reported as thin.
"""

from __future__ import annotations

import pytest

from app.domain.contracts import PromptSource, SynthesisInput
from app.domain.enums import StudyType, Verdict
from app.evidence.grading import (
    QUORUM_FOR_SUPPORTED,
    VERDICT_CEILING,
    VERDICT_STRENGTH,
    max_verdict_for_claims,
    max_verdict_for_sources,
)
from app.evidence.validation import validate_draft
from tests.conftest import FakeSession, FakeSource, make_draft

SUPPORTED_TIER = [
    study for study, ceiling in VERDICT_CEILING.items() if ceiling is Verdict.SUPPORTED
]


# ---------------------------------------------------------------------------
# One claim's ceiling.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("study_type", SUPPORTED_TIER)
def test_a_lone_strong_study_reaches_mixed_not_supported(study_type: StudyType) -> None:
    """The gap the quorum closes, for every grade that could open it.

    Parametrised rather than written once for RCT: a grade added to the
    supported tier later must not quietly arrive without a quorum.
    """
    assert max_verdict_for_sources([study_type]) is Verdict.MIXED


@pytest.mark.parametrize("study_type", SUPPORTED_TIER)
def test_two_strong_studies_reach_supported(study_type: StudyType) -> None:
    assert max_verdict_for_sources([study_type] * QUORUM_FOR_SUPPORTED) is Verdict.SUPPORTED


def test_the_quorum_may_be_met_by_different_strong_grades() -> None:
    """A review plus a trial is two independent sources. Requiring them to
    match would demand a kind of uniformity the literature does not have."""
    assert (
        max_verdict_for_sources([StudyType.SYSTEMATIC_REVIEW, StudyType.RCT])
        is Verdict.SUPPORTED
    )


def test_weak_sources_do_not_count_toward_the_quorum() -> None:
    """Otherwise the rule is satisfiable by padding: cite the one real trial,
    then anything at all, and ``supported`` unlocks."""
    padded = [StudyType.RCT, StudyType.IN_VITRO, StudyType.ANIMAL, StudyType.UNKNOWN]

    assert max_verdict_for_sources(padded) is Verdict.MIXED


def test_a_narrative_review_does_not_count_toward_the_quorum() -> None:
    """It is secondary evidence with no stated method — the tier added after
    the first live runs. Counting it would let two literature summaries
    licence a verdict no primary study supports."""
    assert (
        max_verdict_for_sources([StudyType.RCT, StudyType.NARRATIVE_REVIEW])
        is Verdict.MIXED
    )


def test_shortfall_drops_exactly_one_step() -> None:
    """To ``mixed``, meaning "there is real evidence and it is not settled",
    which is the honest description of a single good trial. Dropping to
    ``weak`` would understate an RCT as badly as ``supported`` overstates it."""
    assert max_verdict_for_sources([StudyType.META_ANALYSIS]) is Verdict.MIXED
    assert (
        VERDICT_STRENGTH[Verdict.SUPPORTED] - VERDICT_STRENGTH[Verdict.MIXED] == 1
    )


def test_ceilings_below_supported_are_untouched_by_quorum() -> None:
    """A single observational study still caps at ``mixed`` and a single
    in-vitro study at ``weak``, exactly as before. Applying a quorum here would
    penalise evidence that is already being described as provisional."""
    assert max_verdict_for_sources([StudyType.OBSERVATIONAL]) is Verdict.MIXED
    assert max_verdict_for_sources([StudyType.IN_VITRO]) is Verdict.WEAK
    assert max_verdict_for_sources([StudyType.NARRATIVE_REVIEW]) is Verdict.WEAK


def test_no_sources_at_all_caps_at_weak() -> None:
    """``no_evidence`` is still permitted everywhere — an article may always
    report finding nothing. What it may not do is assert anything stronger."""
    assert max_verdict_for_sources([]) is Verdict.WEAK
    assert VERDICT_STRENGTH[Verdict.NO_EVIDENCE] < VERDICT_STRENGTH[Verdict.WEAK]


# ---------------------------------------------------------------------------
# The article's ceiling is its weakest claim's.
# ---------------------------------------------------------------------------


def test_a_thin_claim_drags_the_article_down() -> None:
    """The free-riding case. Article-wide counting sees four supported-tier
    sources and permits ``supported``; per claim, one of the three claims rests
    on a single trial and another on nothing."""
    ceiling = max_verdict_for_claims(
        {
            "builds muscle": [StudyType.SYSTEMATIC_REVIEW, StudyType.RCT],
            "aids recovery": [StudyType.RCT],
            "boosts energy": [],
        }
    )

    assert ceiling is Verdict.WEAK


def test_two_claims_cannot_pool_their_evidence() -> None:
    """Counted together these are two supported-tier sources and would clear
    the quorum. Neither claim is corroborated, and nothing in the finished
    article would show which of the two a given source actually backed."""
    ceiling = max_verdict_for_claims(
        {"builds muscle": [StudyType.RCT], "aids recovery": [StudyType.RCT]}
    )

    assert ceiling is Verdict.MIXED


def test_every_claim_clearing_the_quorum_reaches_supported() -> None:
    ceiling = max_verdict_for_claims(
        {
            "builds muscle": [StudyType.SYSTEMATIC_REVIEW, StudyType.RCT],
            "aids recovery": [StudyType.META_ANALYSIS, StudyType.RCT],
        }
    )

    assert ceiling is Verdict.SUPPORTED


def test_an_article_with_no_claims_caps_at_weak() -> None:
    """Unreachable through the pipeline — extraction always yields claims — but
    the ceiling has to exist, and an empty article asserting ``supported`` is
    the wrong direction to fail in."""
    assert max_verdict_for_claims({}) is Verdict.WEAK


# ---------------------------------------------------------------------------
# End to end, through validation.
# ---------------------------------------------------------------------------


def _payload(claims: dict[str, list[tuple[str, StudyType]]]) -> SynthesisInput:
    """Build a prompt payload from ``{claim: [(handle, study_type), ...]}``.

    A handle appearing under two claims yields one source carrying both, which
    is what the pipeline produces and what the quorum has to count correctly.
    """
    sources: dict[str, PromptSource] = {}
    for claim, entries in claims.items():
        for handle, study_type in entries:
            if handle in sources:
                sources[handle].claims.append(claim)
                continue
            sources[handle] = PromptSource(
                handle=handle,
                title=f"Study {handle}",
                abstract="An abstract long enough to be plausible for testing.",
                journal="Journal of Testing",
                year=2022,
                study_type=study_type,
                source_id=f"src-{handle}",
                claims=[claim],
            )
    return SynthesisInput(
        product="Creatine monohydrate",
        target_claims=list(claims),
        sources=[sources[h] for h in sorted(sources, key=lambda h: int(h[1:]))],
    )


def _rows(payload: SynthesisInput) -> FakeSession:
    return FakeSession(
        [
            FakeSource(source.source_id, study_type=source.study_type)
            for source in payload.sources
        ]
    )


async def test_a_supported_draft_on_one_cited_trial_is_rejected() -> None:
    """The whole point, through the real check. The draft is written as
    ``validation_failed`` and never reaches a reviewer."""
    payload = _payload({"improves workout performance": [("S1", StudyType.RCT)]})
    draft = make_draft(verdict=Verdict.SUPPORTED)

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    assert not report.passed
    assert any(f.code == "verdict_exceeds_grade" for f in report.failures)


async def test_the_same_draft_passes_as_mixed() -> None:
    """The evidence is real; only the confidence was wrong. A quorum that
    blocked the article outright rather than its overstatement would push
    thin-evidence topics out of the product entirely."""
    payload = _payload({"improves workout performance": [("S1", StudyType.RCT)]})

    report = await validate_draft(  # type: ignore[arg-type]
        _rows(payload), make_draft(verdict=Verdict.MIXED), payload
    )

    assert report.passed


async def test_two_cited_strong_sources_pass_as_supported() -> None:
    """The shape of the article already in the queue: one systematic review
    and one RCT, both cited, both backing the single claim."""
    payload = _payload(
        {
            "improves workout performance": [
                ("S1", StudyType.SYSTEMATIC_REVIEW),
                ("S2", StudyType.RCT),
            ]
        }
    )
    draft = make_draft(
        verdict=Verdict.SUPPORTED,
        beat_2="Trials report improved output [S1][S2].",
        citations=None,
    )

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    assert report.passed, report.failures


async def test_an_uncited_second_source_does_not_make_the_quorum() -> None:
    """Consistent with the rule that only cited sources set the grade. A
    strong paper sitting unread in the prompt corroborates nothing — the
    article never told the reader it existed."""
    payload = _payload(
        {
            "improves workout performance": [
                ("S1", StudyType.RCT),
                ("S2", StudyType.RCT),  # in the prompt, never cited
            ]
        }
    )
    draft = make_draft(verdict=Verdict.SUPPORTED, beat_2="One trial found gains [S1].")

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    assert not report.passed


async def test_the_failure_names_the_underpowered_claims() -> None:
    """A reviewer reading the report has to know *which* claim was thin —
    "verdict exceeds evidence" on a three-claim article otherwise means
    re-deriving the arithmetic by hand."""
    payload = _payload(
        {
            "improves workout performance": [
                ("S1", StudyType.RCT),
                ("S2", StudyType.META_ANALYSIS),
            ],
            "speeds recovery": [("S3", StudyType.RCT)],
        }
    )
    draft = make_draft(
        verdict=Verdict.SUPPORTED, beat_2="Trials agree [S1][S2][S3]."
    )

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    failure = next(f for f in report.failures if f.code == "verdict_exceeds_grade")
    assert failure.detail["underpowered_claims"] == ["speeds recovery"]
    assert "speeds recovery" in failure.message


async def test_a_source_backing_two_claims_counts_for_both() -> None:
    """It is one paper and one handle, and it corroborates each claim it was
    retrieved for. Counting it once — for whichever claim happened to be
    processed first — would fail the other claim for no reason."""
    payload = _payload(
        {
            "improves workout performance": [
                ("S1", StudyType.SYSTEMATIC_REVIEW),
                ("S2", StudyType.RCT),
            ],
            "speeds recovery": [
                ("S1", StudyType.SYSTEMATIC_REVIEW),
                ("S2", StudyType.RCT),
            ],
        }
    )
    assert payload.sources[0].claims == [
        "improves workout performance",
        "speeds recovery",
    ]
    draft = make_draft(verdict=Verdict.SUPPORTED, beat_2="Both hold [S1][S2].")

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    assert report.passed, report.failures


async def test_the_stored_grade_still_describes_the_best_evidence() -> None:
    """The quorum lowers the *ceiling*, not the recorded grade.

    ``best_evidence_grade`` is shown in the console and stored on the row as a
    description of what was cited. Folding the quorum into it would make a
    meta-analysis read as something weaker than it is, and the console would
    stop being able to say what the article actually rests on.
    """
    payload = _payload({"improves workout performance": [("S1", StudyType.META_ANALYSIS)]})
    draft = make_draft(verdict=Verdict.SUPPORTED)

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    assert not report.passed
    assert report.best_evidence_grade is StudyType.META_ANALYSIS


async def test_no_evidence_is_still_permitted_with_nothing_cited() -> None:
    """The fringe-trend path must survive every tightening of this check."""
    payload = _payload({"cures insomnia": [("S1", StudyType.IN_VITRO)]})
    draft = make_draft(verdict=Verdict.NO_EVIDENCE, beat_2="Nothing was found.")

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    assert report.passed, report.failures


# ---------------------------------------------------------------------------
# The contract that makes per-claim counting possible.
# ---------------------------------------------------------------------------


def test_a_prompt_source_must_name_at_least_one_claim() -> None:
    """Required rather than defaulted, and the reason is a bug this had.

    With ``claims`` defaulting to ``[]``, a source attributed to no claim
    supports no claim — so every draft failed the quorum, reporting a verdict
    problem when the actual fault was a missing field. Fail-closed is right;
    failing at construction points at the real mistake.
    """
    with pytest.raises(ValueError):
        PromptSource(
            handle="S1",
            title="Study",
            abstract="An abstract.",
            journal=None,
            year=2022,
            study_type=StudyType.RCT,
            source_id="src-1",
            claims=[],
        )
