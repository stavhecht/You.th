"""Scoring and handle assignment — pure functions, no database.

Two things are pinned here, and they are the two the rest of the ranking rests
on.

**The score is not cosine.** `score = cosine + grade + recency` exists so that a
systematic review outranks a topically-tighter primary study, which is the
brief's stated intent and the reason top-k cannot be pushed into SQL (see
``test_rerank_plan.py``). If the bonuses ever stop being able to reorder across
a realistic similarity gap, the exact-sort machinery is protecting nothing.

**A shared source keeps one handle.** ``assign_handles`` is the other side of
the identity guard in ``retrieval/dedup.py``: dedup stops one paper entering
the pool twice, and this stops one paper being *named* twice. Either failure
shows the model the same study under two labels, which it then cites as two
independent findings — corroboration manufactured out of one trial.
"""

from __future__ import annotations

import pytest

from app.domain.contracts import CandidatePaper, RankedSource
from app.domain.enums import SourceApi, StudyType
from app.retrieval.rerank import (
    GRADE_BONUS,
    KEEP_BEST_MATCHES,
    RECENCY_BONUS_MAX,
    RECENCY_FULL_CREDIT_YEARS,
    RECENCY_ZERO_CREDIT_YEARS,
    assign_handles,
    recency_bonus,
    score,
    select_top,
)

NOW = 2026


def _paper(study_type: StudyType = StudyType.RCT) -> CandidatePaper:
    return CandidatePaper(
        pmid="1",
        title="A trial",
        abstract="An abstract long enough to be worth reading and embedding.",
        study_type=study_type,
        source_api=SourceApi.PUBMED,
    )


def _ranked(source_id: str, claim: str, final_score: float) -> RankedSource:
    return RankedSource(
        source_id=source_id,
        claim=claim,
        citation_handle="S0",
        paper=_paper(),
        cosine_similarity=final_score,
        final_score=final_score,
    )


# ---------------------------------------------------------------------------
# Recency.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (NOW, RECENCY_BONUS_MAX),
        (NOW - RECENCY_FULL_CREDIT_YEARS, RECENCY_BONUS_MAX),  # boundary, inclusive
        (NOW - RECENCY_ZERO_CREDIT_YEARS, 0.0),  # boundary, inclusive
        (NOW - 40, 0.0),
        (NOW - 10, RECENCY_BONUS_MAX / 2),  # midpoint of the taper
    ],
)
def test_recency_tapers_between_the_two_boundaries(year: int, expected: float) -> None:
    assert recency_bonus(year, current_year=NOW) == pytest.approx(expected)


def test_an_unknown_year_scores_zero_rather_than_a_penalty() -> None:
    """Missing metadata is common for older but still valid work, and study
    type already carries the quality signal. Penalising absence would demote a
    landmark trial for a gap in a provider's record."""
    assert recency_bonus(None, current_year=NOW) == 0.0


def test_a_future_year_is_not_rewarded_beyond_the_cap() -> None:
    """Ahead-of-print records carry next year's date routinely."""
    assert recency_bonus(NOW + 2, current_year=NOW) == RECENCY_BONUS_MAX


# ---------------------------------------------------------------------------
# The grade bonus, and what it is for.
# ---------------------------------------------------------------------------


def test_every_study_type_has_an_explicit_bonus() -> None:
    """``score`` reads the table with ``.get(study_type, 0.0)``.

    A type added to the enum and forgotten here would therefore score as
    *neutral* — the same as an observational study — silently promoting an
    ungraded source into the prompt. The table has to be exhaustive for the
    default never to be reached.
    """
    assert set(GRADE_BONUS) == set(StudyType)


def test_the_hierarchy_is_monotonic_from_unknown_upward() -> None:
    """Excluding UNKNOWN, which is penalised deliberately rather than ranked.

    A non-monotonic table would mean some weaker design outscores a stronger
    one at equal similarity, which inverts the whole point of grading.
    """
    graded = [t for t in StudyType if t is not StudyType.UNKNOWN]
    bonuses = [GRADE_BONUS[t] for t in graded]
    assert bonuses == sorted(bonuses)


def test_unknown_is_penalised_not_treated_as_neutral() -> None:
    """An unclassifiable source cannot raise the verdict ceiling, so promoting
    it into the prompt wastes a slot a gradeable source could occupy."""
    assert GRADE_BONUS[StudyType.UNKNOWN] < GRADE_BONUS[StudyType.OBSERVATIONAL]


# ---------------------------------------------------------------------------
# The composition — the brief's stated intent.
# ---------------------------------------------------------------------------


def test_a_systematic_review_outranks_a_tighter_primary_study() -> None:
    """The design intent, asserted against the live constants.

    A cell-culture or single-trial abstract using the claim's exact vocabulary
    will out-similarity a review that phrases things clinically. If the grade
    bonus cannot close a realistic similarity gap, the ranking is cosine with
    extra steps — and it is the reason top-k is applied in Python rather than
    by a SQL LIMIT on cosine alone.
    """
    review = score(cosine=0.72, study_type=StudyType.SYSTEMATIC_REVIEW, year=NOW)
    primary = score(cosine=0.78, study_type=StudyType.RCT, year=NOW)

    assert review > primary


def test_an_in_vitro_study_cannot_win_on_vocabulary_alone() -> None:
    """The failure the grading exists to prevent: a petri-dish result phrased
    in exactly the marketing's language, ranked first."""
    in_vitro = score(cosine=0.85, study_type=StudyType.IN_VITRO, year=NOW)
    meta = score(cosine=0.65, study_type=StudyType.META_ANALYSIS, year=NOW)

    assert meta > in_vitro


def test_recency_cannot_outweigh_a_grade_step() -> None:
    """Recency is a tiebreaker, not a lever.

    A recent RCT should not lose to a slightly older one, but neither should
    freshness promote weaker evidence past stronger — a 2026 observational
    study is still weaker than a 2015 meta-analysis.
    """
    fresh_weak = score(cosine=0.70, study_type=StudyType.OBSERVATIONAL, year=NOW)
    older_strong = score(cosine=0.70, study_type=StudyType.META_ANALYSIS, year=NOW - 11)

    assert older_strong > fresh_weak


def test_the_reordering_budget_is_the_spread_of_the_grade_table() -> None:
    """How far grading can reach, stated as a number.

    Meta-analysis against in-vitro is the widest swing available, so grading
    reorders across a similarity gap of that size and no further. The ceiling
    is deliberate — past it the "review" is about a different subject, and
    promoting it would ground the article in something off-topic.

    Pinned because the number is easy to change by accident while tuning, and
    because it is what makes the exact sort worth its cost: a bonus too small
    to reorder anything would make a SQL LIMIT on cosine equivalent.
    """
    budget = GRADE_BONUS[StudyType.META_ANALYSIS] - GRADE_BONUS[StudyType.IN_VITRO]

    assert budget == pytest.approx(0.25)
    # Just inside the budget: the stronger design wins.
    assert score(0.60, StudyType.META_ANALYSIS, NOW) > score(0.80, StudyType.IN_VITRO, NOW)
    # Just outside it: similarity wins, which is the correct outcome.
    assert score(0.55, StudyType.META_ANALYSIS, NOW) < score(0.85, StudyType.IN_VITRO, NOW)


def test_similarity_still_decides_between_equals() -> None:
    """Grading must not flatten the ranking: two RCTs of the same vintage are
    separated by how well they match the claim, which is what cosine is for."""
    closer = score(cosine=0.80, study_type=StudyType.RCT, year=NOW)
    further = score(cosine=0.60, study_type=StudyType.RCT, year=NOW)

    assert closer > further


# ---------------------------------------------------------------------------
# Handles.
# ---------------------------------------------------------------------------


def test_a_source_shared_by_two_claims_gets_one_handle() -> None:
    """The corroboration bug, from the naming side.

    Numbering per claim shows the model one paper as S1 under one claim and S3
    under another, and it writes as though two studies agreed.
    """
    handled = assign_handles(
        {
            "reduces stress": [_ranked("src-a", "reduces stress", 0.9)],
            "improves sleep": [
                _ranked("src-a", "improves sleep", 0.7),
                _ranked("src-b", "improves sleep", 0.8),
            ],
        }
    )

    shared = {
        entry.citation_handle
        for ranked in handled.values()
        for entry in ranked
        if entry.source_id == "src-a"
    }
    assert shared == {"S1"}


def test_numbering_follows_descending_best_score() -> None:
    """The model leans on earlier handles, so S1 must be the strongest evidence
    available — not whichever claim happened to be processed first."""
    handled = assign_handles(
        {
            "a": [_ranked("weak", "a", 0.10), _ranked("strong", "a", 0.95)],
            "b": [_ranked("middle", "b", 0.50)],
        }
    )

    by_id = {
        entry.source_id: entry.citation_handle
        for ranked in handled.values()
        for entry in ranked
    }
    assert by_id == {"strong": "S1", "middle": "S2", "weak": "S3"}


def test_a_shared_source_is_ranked_by_its_best_score_not_its_last() -> None:
    """A paper that is central to one claim and marginal to another is still
    strong evidence. Numbering it by the weaker appearance would bury it."""
    handled = assign_handles(
        {
            "a": [_ranked("shared", "a", 0.99)],
            "b": [_ranked("shared", "b", 0.10), _ranked("other", "b", 0.50)],
        }
    )

    by_id = {
        entry.source_id: entry.citation_handle
        for ranked in handled.values()
        for entry in ranked
    }
    assert by_id == {"shared": "S1", "other": "S2"}


def test_handles_start_at_one_because_s0_is_the_placeholder() -> None:
    """``rank_for_claim`` emits S0 before assignment. An S0 surviving into a
    prompt would be a handle the model could cite but validation could not
    resolve."""
    handled = assign_handles({"a": [_ranked("only", "a", 0.5)]})

    assert handled["a"][0].citation_handle == "S1"


def test_claims_with_no_survivors_are_preserved_as_empty() -> None:
    """Dropping the key would make the claim vanish from the prompt payload
    rather than appear as a claim with no supporting evidence."""
    handled = assign_handles({"answered": [_ranked("s", "answered", 0.5)], "bare": []})

    assert set(handled) == {"answered", "bare"}
    assert handled["bare"] == []


def test_assigning_handles_to_nothing_is_not_an_error() -> None:
    """The no-evidence path reaches here with every claim empty."""
    assert assign_handles({"a": [], "b": []}) == {"a": [], "b": []}


# ---------------------------------------------------------------------------
# The best matches are always kept.
# ---------------------------------------------------------------------------


def _scored(source_id: str, cosine: float, final_score: float) -> RankedSource:
    entry = _ranked(source_id, "c", final_score)
    return entry.model_copy(update={"cosine_similarity": cosine})


def test_a_best_match_cut_by_the_bonuses_is_kept() -> None:
    """wt_087: the melanoma trial matched second best of 51 (0.863) and fell
    0.013 under the cut, behind reviews matching 0.78 with a larger bonus."""
    reviews = [_scored(f"r{i}", 0.78, 0.96 - i * 0.001) for i in range(12)]
    trial = _scored("trial", 0.863, 0.943)

    top = select_top(sorted([*reviews, trial], key=lambda e: e.final_score, reverse=True), 12)

    assert len(top) == 12
    assert "trial" in {e.source_id for e in top}
    assert "r11" not in {e.source_id for e in top}  # the lowest score made room
    assert [e.final_score for e in top] == sorted((e.final_score for e in top), reverse=True)


def test_nothing_changes_when_the_best_matches_already_made_the_cut() -> None:
    ranked = [_scored(f"p{i}", 0.9 - i * 0.01, 1.0 - i * 0.01) for i in range(20)]

    assert select_top(ranked, 12) == ranked[:12]


def test_only_the_best_few_matches_are_protected() -> None:
    ranked = sorted(
        [_scored(f"r{i}", 0.70, 0.95 - i * 0.001) for i in range(12)]
        + [_scored(f"m{i}", 0.90 - i * 0.01, 0.80 - i * 0.01) for i in range(6)],
        key=lambda e: e.final_score,
        reverse=True,
    )

    kept = {e.source_id for e in select_top(ranked, 12)}

    assert {f"m{i}" for i in range(KEEP_BEST_MATCHES)} <= kept
    assert f"m{KEEP_BEST_MATCHES}" not in kept


def test_a_protected_paper_at_the_cut_is_not_pushed_out_by_the_others() -> None:
    """The best match sits last in the top 12 while the next two are outside it:
    making room for those two must not drop the first."""
    filler = [_scored(f"f{i}", 0.70, 0.99 - i * 0.001) for i in range(11)]
    best = [_scored("A", 0.95, 0.90), _scored("B", 0.94, 0.85), _scored("C", 0.93, 0.84)]
    ranked = sorted([*filler, *best], key=lambda e: e.final_score, reverse=True)

    kept = {e.source_id for e in select_top(ranked, 12)}

    assert {"A", "B", "C"} <= kept
    assert len(kept) == 12


def test_a_short_list_is_returned_whole() -> None:
    ranked = [_scored("a", 0.9, 0.9), _scored("b", 0.95, 0.8)]

    assert select_top(ranked, 12) == ranked


def test_a_best_matching_narrative_review_is_not_protected() -> None:
    """conf_004: a narrative review titled like the question pushed out a 2024
    meta-analysis. Only trials and systematic reviews are protected."""
    strong = [_scored(f"m{i}", 0.87, 1.05 - i * 0.001) for i in range(12)]
    narrative = _scored("narrative", 0.905, 0.884).model_copy(
        update={"paper": _paper(StudyType.NARRATIVE_REVIEW)}
    )

    kept = {e.source_id for e in select_top([*strong, narrative], 12)}

    assert "narrative" not in kept
    assert "m11" in kept
