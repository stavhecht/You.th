"""Study-type classification — the cases real PubMed data produced.

``test_invariants.py`` covers the classifier's happy paths. This file covers
the ways it was measurably *wrong* on the first live runs, so each test here
names a real defect rather than a hypothetical one. Of 92 cached sources:

* three **trial protocols scored `rct`**, whose ceiling is `supported`. A
  protocol has reported nothing — it describes the randomised trial it intends
  to run, in exactly the vocabulary the text heuristics look for.
* a plainly-tagged **`Review` scored `systematic_review`** for the same reason,
  a review discusses the trials it surveys, and that paper was cited in a real
  draft and set its evidence grade.
* **38 of 92 scored `unknown`**, sixteen of them narrative reviews with nowhere
  else to go.

The single root cause: ``classify_study_type`` treated "informative but
unmapped" identically to "no metadata at all", then guessed from prose. Its own
docstring promises to prefer the weaker classification when torn; it did the
reverse, and only against real data was that visible.
"""

from __future__ import annotations

import pytest

from app.domain.enums import EVIDENCE_RANK, StudyType, Verdict
from app.evidence.grading import (
    NEGATIVE_PUBLICATION_TYPES,
    PUBLICATION_TYPE_MAP,
    VERDICT_CEILING,
    classify_study_type,
    is_protocol,
    max_verdict_for_grade,
)

#: Prose common to a protocol, a narrative review, and a real trial alike —
#: which is the whole problem with reading it.
TRIAL_VOCABULARY = (
    "This randomized, double-blind, placebo-controlled trial will enrol 120 "
    "adults to assess creatine supplementation on workout performance."
)


# ---------------------------------------------------------------------------
# Protocols: a plan is not a result.
# ---------------------------------------------------------------------------


def test_a_protocol_is_not_a_trial() -> None:
    """The worst of the three defects: ceiling `supported` on a study that has
    not reported a single outcome."""
    result = classify_study_type(
        ["Clinical Trial Protocol", "Journal Article"],
        title="Effect of creatine on strength: a study protocol",
        abstract=TRIAL_VOCABULARY,
    )

    assert result is StudyType.UNKNOWN


def test_the_protocol_tag_is_recognised_among_others() -> None:
    assert is_protocol("Clinical Trial Protocol; Journal Article")
    assert is_protocol("Journal Article; Clinical Trial Protocol")


def test_a_protocol_is_recognised_from_its_title_alone() -> None:
    """OpenAlex and Semantic Scholar supply no publication types at all, so a
    tag check by itself would let protocols in through those providers.
    Announcing itself in the title is close to a house style in trial
    reporting, which makes it a usable second signal."""
    assert is_protocol(None, "Creatine for recovery: study protocol for a randomised trial")
    assert is_protocol(None, "A trial protocol for pre-workout supplementation")


def test_a_real_trial_is_not_mistaken_for_a_protocol() -> None:
    """The filter drops papers outright, so a false positive silently deletes
    evidence — strictly worse than the bug it fixes."""
    assert not is_protocol("Journal Article; Randomized Controlled Trial", "A randomised trial")
    assert not is_protocol(None, "Protocols for measuring VO2 max in trained athletes")


# ---------------------------------------------------------------------------
# Reviews: "Review" is a supertype, not a verdict.
# ---------------------------------------------------------------------------


def test_a_bare_review_tag_yields_a_narrative_review() -> None:
    result = classify_study_type(
        ["Journal Article", "Review"],
        title="Creatine and athletic performance",
        abstract="We survey the literature on creatine supplementation.",
    )

    assert result is StudyType.NARRATIVE_REVIEW


def test_a_review_discussing_trials_is_not_promoted_to_a_trial() -> None:
    """The mirror of the protocol bug, and the reason the text upgrade is
    restricted to the review family. Every narrative review of an intervention
    describes randomised trials at length — that is what a review *is*."""
    result = classify_study_type(
        ["Journal Article", "Review"],
        title="Creatine supplementation: a review",
        abstract=TRIAL_VOCABULARY,
    )

    assert result is StudyType.NARRATIVE_REVIEW


def test_a_review_whose_title_says_systematic_is_upgraded() -> None:
    """Why "Review" is a floor rather than a mapping.

    Mapping it outright was tried and reverted: PubMed's indexing lags, and
    genuine systematic reviews in the cache carry only the supertype while
    their titles say otherwise. A hard mapping demoted them, which is the
    protocol bug mirrored — over-trusting a tag instead of over-trusting prose.
    """
    result = classify_study_type(
        ["Journal Article", "Review"],
        title="CrossFit: predictable? A Systematic Review",
        abstract="We searched four databases.",
    )

    assert result is StudyType.SYSTEMATIC_REVIEW


@pytest.mark.parametrize(
    ("title", "abstract"),
    [
        # The three cited in run baa0e155 (2026-10-13), abstracts as cached.
        (
            "Impact of Creatine Supplementation on Muscle and Bone Strength in "
            "Older Adults: A Narrative Review",
            "Evidence from randomized trials and meta-analyses indicates that creatine "
            "supplementation combined with progressive resistance training improves strength.",
        ),
        (
            "Sparing Muscle While Losing Fat: A Narrative Review of Ketogenic Diets",
            "Comparative meta-analyses report small yet larger reductions in lean mass.",
        ),
        (
            "Effects of omega-3 fatty acids on skeletal muscle vascular health and metabolism.",
            "Recent meta-analysis and intervention studies suggest that omega-3 FAs improve "
            "macrovascular health.",
        ),
    ],
)
def test_a_review_that_only_surveys_meta_analyses_is_not_one(title: str, abstract: str) -> None:
    """A review mentions meta-analyses for the reason it mentions trials: they
    are what it reviews. All three graded ``meta_analysis`` and set a real
    article's evidence grade."""
    assert classify_study_type(["Review"], title=title, abstract=abstract) is (
        StudyType.NARRATIVE_REVIEW
    )


@pytest.mark.parametrize(
    "abstract",
    [
        "A systematic search across seven databases identified 75 studies for meta-analysis.",
        "This meta-analysis pooled 75 trials.",
        "We conducted a systematic review and meta-analysis of randomized trials.",
        "Reporting followed PRISMA. Meta-analyses used random effects.",
    ],
)
def test_a_review_describing_its_own_method_is_still_upgraded(abstract: str) -> None:
    """OpenAlex types every review ``review``, so a real meta-analysis it alone
    found depends on its abstract describing itself."""
    result = classify_study_type(
        ["review"], title="Protein supplements and recovery", abstract=abstract
    )

    assert result in {StudyType.META_ANALYSIS, StudyType.SYSTEMATIC_REVIEW}


def test_a_narrative_title_outranks_a_review_family_word_beside_it() -> None:
    result = classify_study_type(
        ["Review"],
        title="Magnesium and sleep: a narrative review of meta-analyses",
        abstract="We conducted a systematic search of PubMed.",
    )

    assert result is StudyType.NARRATIVE_REVIEW


def test_an_explicit_systematic_review_tag_still_wins_outright() -> None:
    """The supertype travels with the specific tag on properly indexed papers,
    and must not drag it down."""
    result = classify_study_type(
        ["Journal Article", "Review", "Systematic Review"],
        title="Blood flow restriction training",
        abstract="A synthesis of trials.",
    )

    assert result is StudyType.SYSTEMATIC_REVIEW


def test_a_review_upgrade_cannot_reach_beyond_the_review_family() -> None:
    """Upward movement is bounded by kind, not only by strength: an
    observational or in-vitro reading of a review's abstract is a description
    of the surveyed literature, never of this paper."""
    result = classify_study_type(
        ["Review"],
        title="Exercise and cardiovascular health",
        abstract="Cohort studies and cell culture work are discussed.",
    )

    assert result is StudyType.NARRATIVE_REVIEW


# ---------------------------------------------------------------------------
# Negative tags suppress the guess; positive tags outrank them.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("negative", sorted(NEGATIVE_PUBLICATION_TYPES))
def test_every_negative_type_blocks_the_text_fallback(negative: str) -> None:
    """Each of these says something real about a paper without naming a design.
    Falling through to prose is how a protocol became an RCT."""
    assert (
        classify_study_type([negative], title="A trial", abstract=TRIAL_VOCABULARY)
        is StudyType.UNKNOWN
    )


def test_a_positive_tag_outranks_a_negative_one() -> None:
    """Checked after the map, not before. A real trial can carry `Comment`
    alongside its own type, and losing it to a co-tag would be a new bug in
    place of the old one."""
    result = classify_study_type(
        ["Comment", "Randomized Controlled Trial"], title="", abstract=""
    )

    assert result is StudyType.RCT


def test_uninformative_tags_still_permit_the_text_fallback() -> None:
    """The correction had to stay narrow. PubMed's indexing lags by months, so
    a genuine trial is routinely tagged only `Journal Article` — 17 of the 92
    cached sources are. Suppressing the fallback for those would trade one
    misclassification for a much larger one."""
    result = classify_study_type(
        ["Journal Article"],
        title="Effects of pre-workout supplementation",
        abstract="In this randomized, double-blind, placebo-controlled study...",
    )

    assert result is StudyType.RCT


# ---------------------------------------------------------------------------
# The bare clinical-trial tag.
# ---------------------------------------------------------------------------


def test_a_bare_clinical_trial_tag_is_graded_below_an_rct() -> None:
    """PubMed tags a randomised trial with both `Randomized Controlled Trial`
    and `Clinical Trial`, so a paper carrying only the latter was not
    randomised. Graded down per the module's stated bias toward under-claiming
    — previously it scored `unknown`, which was wrong in the other direction."""
    result = classify_study_type(["Clinical Trial", "Journal Article"])

    assert result is StudyType.OBSERVATIONAL
    assert EVIDENCE_RANK[result] < EVIDENCE_RANK[StudyType.RCT]


def test_a_randomised_trial_carrying_both_tags_is_still_an_rct() -> None:
    result = classify_study_type(
        ["Clinical Trial", "Randomized Controlled Trial", "Journal Article"]
    )

    assert result is StudyType.RCT


# ---------------------------------------------------------------------------
# The new tier's place in the hierarchy.
# ---------------------------------------------------------------------------


def test_a_narrative_review_sits_below_observational_and_above_a_case_report() -> None:
    """It contributes no primary data and states no method protecting it from
    selection bias, so it ranks under a cohort study; it surveys a literature
    rather than one patient, so it ranks over a case report."""
    assert (
        EVIDENCE_RANK[StudyType.CASE_REPORT]
        < EVIDENCE_RANK[StudyType.NARRATIVE_REVIEW]
        < EVIDENCE_RANK[StudyType.OBSERVATIONAL]
    )


def test_a_narrative_review_caps_the_verdict_at_weak() -> None:
    """It restates other people's findings without a method. Useful as a
    pointer, never sufficient support."""
    assert max_verdict_for_grade(StudyType.NARRATIVE_REVIEW) is Verdict.WEAK


def test_a_narrative_review_is_distinguishable_from_unknown() -> None:
    """The reason it earned a tier rather than being folded into UNKNOWN.

    Both cap at `weak`, so the verdict is unaffected — the value is
    diagnostic. "We retrieved sixteen narrative reviews" and "we retrieved
    sixteen unreadable records" call for opposite responses, and collapsing
    them makes the unknown count uninterpretable.
    """
    assert StudyType.NARRATIVE_REVIEW is not StudyType.UNKNOWN
    assert (
        VERDICT_CEILING[StudyType.NARRATIVE_REVIEW]
        == VERDICT_CEILING[StudyType.UNKNOWN]
        == Verdict.WEAK
    )


def test_the_ceiling_table_covers_every_study_type() -> None:
    """``max_verdict_for_grade`` subscripts this table directly, so a type
    added to the enum and forgotten here is a KeyError inside invariant #3's
    check — the check failing open on the article it was meant to stop."""
    assert set(VERDICT_CEILING) == set(StudyType)


def test_no_publication_type_maps_to_an_unknown_grade() -> None:
    """A mapping to UNKNOWN would be indistinguishable from no mapping, while
    silently suppressing the text fallback. Negative types belong in
    ``NEGATIVE_PUBLICATION_TYPES``, which is explicit about doing that."""
    assert StudyType.UNKNOWN not in PUBLICATION_TYPE_MAP.values()
