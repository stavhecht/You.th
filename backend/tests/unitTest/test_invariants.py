"""The invariant test suite.

These are the guarantees the product rests on. Each maps to a specific
mechanism named in DESIGN.md §1:

  #1 human-in-the-loop      -> services/review.py + a DB CHECK constraint
  #2 grounded, not invented -> evidence/validation.py
  #3 evidence caps verdict  -> evidence/grading.py
  #4 immutable AI draft     -> a DB trigger

Invariants #1 and #4 are enforced by Postgres, so their tests live in
``test_db_invariants.py`` and need a live database — a fake cannot verify a
guarantee whose whole point is that it holds independently of application code.
"""

from __future__ import annotations

import pytest

from app.domain.contracts import ArticleSection, CitationGroup, SynthesisInput
from app.domain.enums import StudyType, Verdict
from app.evidence.grading import (
    best_grade,
    classify_study_type,
    is_weak_evidence,
    max_verdict_for_grade,
    verdict_exceeds_grade,
)
from app.evidence.validation import summarise_failures, validate_draft
from tests.conftest import FakeSession, FakeSource, make_draft

# ---------------------------------------------------------------------------
# Invariant #3 — evidence grade caps verdict confidence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("grade", "ceiling"),
    [
        (StudyType.META_ANALYSIS, Verdict.SUPPORTED),
        (StudyType.SYSTEMATIC_REVIEW, Verdict.SUPPORTED),
        (StudyType.RCT, Verdict.SUPPORTED),
        (StudyType.OBSERVATIONAL, Verdict.MIXED),
        (StudyType.CASE_REPORT, Verdict.WEAK),
        (StudyType.ANIMAL, Verdict.WEAK),
        (StudyType.IN_VITRO, Verdict.WEAK),
        (StudyType.UNKNOWN, Verdict.WEAK),
    ],
)
def test_verdict_ceiling_by_grade(grade: StudyType, ceiling: Verdict) -> None:
    assert max_verdict_for_grade(grade) is ceiling


def test_supported_verdict_on_in_vitro_evidence_is_rejected() -> None:
    """The headline case: a confident verdict on cell-culture evidence."""
    assert verdict_exceeds_grade(Verdict.SUPPORTED, StudyType.IN_VITRO)


def test_no_evidence_verdict_is_always_permitted() -> None:
    """An article may always report finding nothing, whatever it retrieved."""
    for grade in StudyType:
        assert not verdict_exceeds_grade(Verdict.NO_EVIDENCE, grade)


def test_unknown_study_type_caps_rather_than_licenses() -> None:
    """Uncertainty must reduce confidence, never grant it.

    The failure mode this guards: treating unclassifiable sources as neutral,
    which would let a pile of ungradeable papers unlock 'supported'.
    """
    assert verdict_exceeds_grade(Verdict.SUPPORTED, StudyType.UNKNOWN)
    assert verdict_exceeds_grade(Verdict.MIXED, StudyType.UNKNOWN)


def test_best_grade_of_empty_set_is_unknown() -> None:
    """An article citing nothing gets the weakest possible ceiling."""
    assert best_grade([]) is StudyType.UNKNOWN


def test_best_grade_takes_the_strongest_source() -> None:
    assert best_grade([StudyType.IN_VITRO, StudyType.RCT, StudyType.ANIMAL]) is StudyType.RCT


def test_weak_evidence_flag_covers_everything_below_observational() -> None:
    """Drives the inline warning in the review UI's sources panel."""
    assert is_weak_evidence(StudyType.IN_VITRO)
    assert is_weak_evidence(StudyType.ANIMAL)
    assert is_weak_evidence(StudyType.CASE_REPORT)
    assert is_weak_evidence(StudyType.UNKNOWN)
    assert not is_weak_evidence(StudyType.OBSERVATIONAL)
    assert not is_weak_evidence(StudyType.RCT)


# ---------------------------------------------------------------------------
# Study-type classification.
# ---------------------------------------------------------------------------


def test_publication_type_beats_abstract_text() -> None:
    """A curated tag outranks prose that merely mentions randomisation."""
    assert (
        classify_study_type(
            ["Meta-Analysis", "Review"],
            title="Effects of X",
            abstract="We pooled randomized trials in mice.",
        )
        is StudyType.META_ANALYSIS
    )


def test_strongest_publication_type_wins() -> None:
    """PubMed tags meta-analyses as 'Review' too; the stronger tag is the signal."""
    assert (
        classify_study_type(["Journal Article", "Randomized Controlled Trial"])
        is StudyType.RCT
    )


def test_text_fallback_when_no_usable_publication_type() -> None:
    """OpenAlex and Semantic Scholar often supply nothing usable."""
    assert (
        classify_study_type(
            [], title="A systematic review of Withania somnifera", abstract=""
        )
        is StudyType.SYSTEMATIC_REVIEW
    )
    assert (
        classify_study_type([], title="Effects in rats", abstract="Male Wistar rats were...")
        is StudyType.ANIMAL
    )


def test_unclassifiable_returns_unknown_not_a_guess() -> None:
    """Conservative by design: UNKNOWN caps the verdict, a guess would unlock it."""
    result = classify_study_type([], title="Some paper", abstract="Some text.")
    assert result is StudyType.UNKNOWN


# ---------------------------------------------------------------------------
# Invariant #2 — grounded, never hallucinated.
#
# The brief's instruction to watch validation reject something is these tests.
# ---------------------------------------------------------------------------


async def test_valid_draft_passes(payload: SynthesisInput) -> None:
    """Baseline: a well-grounded draft reaches the queue."""
    session = FakeSession([FakeSource("src-1"), FakeSource("src-2")])
    report = await validate_draft(session, make_draft(), payload)  # type: ignore[arg-type]

    assert report.passed
    assert report.citations_resolved == 1
    assert report.citations_total == 1
    assert report.badge == "1/1 citations resolve"
    assert report.failures == []


async def test_handle_not_in_prompt_fails_validation(payload: SynthesisInput) -> None:
    """A draft citing S9 when only S1-S2 were provided must not enter the queue.

    This is the core anti-hallucination check, and the single most important
    assertion in the suite.
    """
    session = FakeSession([FakeSource("src-1"), FakeSource("src-2")])
    draft = make_draft(
        beat_2="One trial reported lower cortisol [S9].",
        citations=[CitationGroup(claim="reduces cortisol", source_ids=["S9"])],
    )

    report = await validate_draft(session, draft, payload)  # type: ignore[arg-type]

    assert not report.passed
    codes = {failure.code for failure in report.failures}
    assert "hallucinated_handle" in codes
    assert any("S9" in failure.message for failure in report.failures)


async def test_hallucinated_handle_is_reported_once_not_twice(
    payload: SynthesisInput,
) -> None:
    """An unknown handle is a hallucination, not also an unresolvable source.

    Reporting the same defect under two codes makes the report harder to read,
    not more convincing.
    """
    session = FakeSession([FakeSource("src-1")])
    draft = make_draft(
        beat_2="Cortisol fell [S7].",
        citations=[CitationGroup(claim="cortisol", source_ids=["S7"])],
    )

    report = await validate_draft(session, draft, payload)  # type: ignore[arg-type]

    codes = [failure.code for failure in report.failures]
    assert codes.count("hallucinated_handle") == 1
    assert "unresolvable_source" not in codes


async def test_source_without_resolvable_identifier_fails_validation(
    payload: SynthesisInput,
) -> None:
    """A cited source with neither PMID nor DOI fails 'citations resolve'.

    Note the cascade, which is intended: an unresolvable citation is also
    excluded from ``best_grade``, so the evidence ceiling collapses to UNKNOWN
    and the verdict check fires too. An article whose citations do not resolve
    has not earned any verdict above 'weak' either.
    """
    session = FakeSession(
        [FakeSource("src-1", pmid=None, doi=None), FakeSource("src-2")]
    )

    report = await validate_draft(session, make_draft(), payload)  # type: ignore[arg-type]

    assert not report.passed
    assert report.citations_resolved == 0
    codes = {failure.code for failure in report.failures}
    assert "unresolvable_source" in codes
    assert "verdict_exceeds_grade" in codes
    assert report.best_evidence_grade is StudyType.UNKNOWN


async def test_source_deleted_between_retrieval_and_validation_fails(
    payload: SynthesisInput,
) -> None:
    """The row is gone; the handle cannot resolve to anything real."""
    session = FakeSession([FakeSource("src-2")])  # src-1 absent

    report = await validate_draft(session, make_draft(), payload)  # type: ignore[arg-type]

    assert not report.passed
    assert any(f.code == "unresolvable_source" for f in report.failures)


async def test_uncited_evidence_beat_fails_validation(payload: SynthesisInput) -> None:
    """Beat 2 states findings, so it must cite something."""
    session = FakeSession([FakeSource("src-1"), FakeSource("src-2")])
    draft = make_draft(
        beat_2="Several studies show it works well for stress.",
        citations=[],
    )

    report = await validate_draft(session, draft, payload)  # type: ignore[arg-type]

    assert not report.passed
    assert any(f.code == "uncited_beat" for f in report.failures)


async def test_no_evidence_verdict_is_exempt_from_the_citation_requirement(
    payload: SynthesisInput,
) -> None:
    """An honest 'we found nothing' has nothing to cite.

    Demanding a citation here would push the model toward citing something
    irrelevant purely to satisfy the check.
    """
    session = FakeSession([FakeSource("src-1"), FakeSource("src-2")])
    draft = make_draft(
        verdict=Verdict.NO_EVIDENCE,
        beat_2="No trials have tested this claim in people.",
        citations=[],
    )

    report = await validate_draft(session, draft, payload)  # type: ignore[arg-type]

    assert report.passed


async def test_an_uncited_section_fails_validation(payload: SynthesisInput) -> None:
    """A section is up to eight sentences, and there may be five of them.

    Exempting them would turn beat 1 and 3's deliberate three-sentence
    allowance into most of the article.
    """
    session = FakeSession([FakeSource("src-1"), FakeSource("src-2")])
    draft = make_draft(
        sections=[
            ArticleSection(
                heading="What the trials measured",
                body="Two trials in adults reported the same effect [S1].",
            ),
            ArticleSection(
                heading="Why it might work",
                body="The mechanism is thought to involve the stress axis.",
            ),
        ],
    )

    report = await validate_draft(session, draft, payload)  # type: ignore[arg-type]

    assert not report.passed
    failures = [f for f in report.failures if f.code == "uncited_section"]
    assert len(failures) == 1
    assert failures[0].detail["section"] == 2


async def test_a_cited_section_passes(payload: SynthesisInput) -> None:
    session = FakeSession([FakeSource("src-1"), FakeSource("src-2")])
    draft = make_draft(
        sections=[
            ArticleSection(
                heading="What the trials measured",
                body="Two trials in adults reported the same effect [S1].",
            ),
        ],
    )

    report = await validate_draft(session, draft, payload)  # type: ignore[arg-type]

    assert report.passed


async def test_no_evidence_verdict_exempts_sections_too(payload: SynthesisInput) -> None:
    session = FakeSession([FakeSource("src-1"), FakeSource("src-2")])
    draft = make_draft(
        verdict=Verdict.NO_EVIDENCE,
        beat_2="No trials have tested this claim in people.",
        sections=[ArticleSection(heading="What we searched", body="Four databases.")],
        citations=[],
    )

    report = await validate_draft(session, draft, payload)  # type: ignore[arg-type]

    assert report.passed


# ---------------------------------------------------------------------------
# Invariant #3, enforced end-to-end through validation.
# ---------------------------------------------------------------------------


async def test_verdict_exceeding_cited_evidence_fails_validation(
    payload: SynthesisInput,
) -> None:
    """'supported' resting on two cell-culture studies never reaches a human."""
    session = FakeSession(
        [
            FakeSource("src-1", study_type=StudyType.IN_VITRO),
            FakeSource("src-2", study_type=StudyType.IN_VITRO),
        ]
    )
    draft = make_draft(verdict=Verdict.SUPPORTED)

    report = await validate_draft(session, draft, payload)  # type: ignore[arg-type]

    assert not report.passed
    assert any(f.code == "verdict_exceeds_grade" for f in report.failures)
    assert report.best_evidence_grade is StudyType.IN_VITRO


async def test_uncited_strong_source_cannot_raise_the_ceiling(
    payload: SynthesisInput,
) -> None:
    """Only *cited* sources set the grade.

    A meta-analysis the model ignored must not license a confident verdict the
    article never actually supported.
    """
    session = FakeSession(
        [
            FakeSource("src-1", study_type=StudyType.IN_VITRO),  # the cited one
            FakeSource("src-2", study_type=StudyType.META_ANALYSIS),  # ignored
        ]
    )
    draft = make_draft(verdict=Verdict.SUPPORTED)

    report = await validate_draft(session, draft, payload)  # type: ignore[arg-type]

    assert not report.passed
    assert report.best_evidence_grade is StudyType.IN_VITRO


async def test_verdict_within_grade_passes(payload: SynthesisInput) -> None:
    """A single cited RCT supports ``mixed`` — real evidence, not settled.

    This asserted ``supported`` until the evidence quorum landed, which is the
    behaviour the quorum removes: one study, however well designed, cannot
    carry the strongest verdict the system can express. See
    ``test_evidence_quorum.py`` for the rule itself.
    """
    session = FakeSession([FakeSource("src-1", study_type=StudyType.RCT)])
    report = await validate_draft(  # type: ignore[arg-type]
        session, make_draft(verdict=Verdict.MIXED), payload
    )
    assert report.passed


# ---------------------------------------------------------------------------
# Reporting.
# ---------------------------------------------------------------------------


async def test_all_failures_are_collected_not_short_circuited(
    payload: SynthesisInput,
) -> None:
    """When a draft is bad you want the whole picture in one report."""
    session = FakeSession([FakeSource("src-1", study_type=StudyType.IN_VITRO)])
    draft = make_draft(
        verdict=Verdict.SUPPORTED,
        beat_2="Cortisol fell [S1] and sleep improved [S9].",
        citations=[CitationGroup(claim="cortisol", source_ids=["S1", "S9"])],
    )

    report = await validate_draft(session, draft, payload)  # type: ignore[arg-type]

    codes = {failure.code for failure in report.failures}
    assert "hallucinated_handle" in codes
    assert "verdict_exceeds_grade" in codes


def test_failure_summary_is_human_readable() -> None:
    from app.domain.contracts import ValidationFailure, ValidationReport

    report = ValidationReport(
        passed=False,
        citations_total=3,
        citations_resolved=1,
        best_evidence_grade=StudyType.IN_VITRO,
        failures=[
            ValidationFailure(code="hallucinated_handle", message="cited S9"),
            ValidationFailure(code="hallucinated_handle", message="cited S8"),
            ValidationFailure(code="verdict_exceeds_grade", message="too confident"),
        ],
    )

    summary = summarise_failures(report)
    assert "2 hallucinated citations" in summary
    assert "verdict exceeds evidence grade" in summary
