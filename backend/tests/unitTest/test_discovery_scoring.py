"""The ranking, and the two ways it is allowed to say nothing.

No database and no network — the scorer takes counts and thresholds as
arguments precisely so the whole ranking is reproducible from a table of
numbers. Every constant it uses is a guess made before seeing real data, so
these tests pin the *properties* the formula has to keep rather than the values
it currently produces.
"""

from __future__ import annotations

from datetime import date

from app.discovery.scoring import (
    baseline_from_buckets,
    lift,
    quality_weight,
    rank_candidates,
    score,
    study_mix_of,
    window_starts,
)
from app.discovery.vocab import is_too_common
from app.domain.enums import StudyType


def _rank(**overrides: object) -> list:
    """rank_candidates with a plentiful baseline and no floor in the way."""
    kwargs: dict = {
        "current_counts": {},
        "baseline_counts": {},
        "descriptor_names": {},
        "study_mixes": {},
        "outcome_cooccurrence": {},
        "pmids": {},
        "min_papers": 4,
        "min_papers_per_angle": 3,
        "max_angles_per_substance": 2,
        "baseline_windows_available": 6,
        "min_baseline_windows": 4,
        "limit": 8,
    }
    kwargs.update(overrides)
    return rank_candidates(**kwargs)  # type: ignore[arg-type]


# --- the anti-flooding guarantee -------------------------------------------


def test_no_candidates_without_enough_baseline() -> None:
    """The single most important behaviour in the package.

    A scan with no history can only rank by raw volume, which on day one
    proposes the three substances any reviewer would have named unaided — with
    the authority of a ranking. Empty is a better first impression, and there is
    deliberately no volume fallback to fall back to.
    """
    candidates = _rank(
        current_counts={"D014807": 90, "D003401": 40},
        descriptor_names={"D014807": "Vitamin D", "D003401": "Creatine"},
        baseline_windows_available=1,
        min_baseline_windows=4,
    )
    assert candidates == []


def test_candidates_appear_once_the_baseline_is_deep_enough() -> None:
    candidates = _rank(
        current_counts={"D003401": 17},
        baseline_counts={"D003401": 3.0},
        descriptor_names={"D003401": "Creatine"},
        baseline_windows_available=4,
        min_baseline_windows=4,
    )
    assert [candidate.substance_ui for candidate in candidates] == ["D003401"]


# --- the floor --------------------------------------------------------------


def test_paper_floor_excludes_a_single_lab() -> None:
    """Three papers is one group publishing a series, not a trend."""
    candidates = _rank(
        current_counts={"D000001": 3, "D000002": 4},
        descriptor_names={"D000001": "Thin", "D000002": "Real"},
        min_papers=4,
    )
    assert [candidate.substance_ui for candidate in candidates] == ["D000002"]


def test_quality_weight_cannot_rescue_a_below_floor_substance() -> None:
    """Multiplicative, so strong evidence reorders but never promotes.

    One systematic review about a substance nobody else studies is a finding,
    not a trend — the same reasoning as invariant #3's quorum.
    """
    candidates = _rank(
        current_counts={"D000001": 2},
        study_mixes={"D000001": {StudyType.SYSTEMATIC_REVIEW.value: 2}},
        descriptor_names={"D000001": "Thin"},
        min_papers=4,
    )
    assert candidates == []


# --- the formula's shape ----------------------------------------------------


def test_lift_is_smoothed_against_a_zero_baseline() -> None:
    """A zero baseline is the common case for anything genuinely new."""
    assert lift(4, 0.0) == 5.0
    assert lift(0, 0.0) == 1.0


def test_smoothing_tames_a_tiny_denominator() -> None:
    """0.2 papers per fortnight into four is not a 20x discovery."""
    assert lift(4, 0.2) < 5.0


def test_volume_beats_a_tiny_fluctuation() -> None:
    """30 -> 90 must outrank 1 -> 4; pure lift gets this backwards.

    This is the single property that keeps the top of the list from being
    permanently occupied by three-paper noise.
    """
    big = score(90, 30.0, {})
    small = score(4, 1.0, {})
    assert big > small


def test_growth_beats_equal_volume_without_it() -> None:
    """And the converse: at equal size, the one that is growing wins."""
    growing = score(40, 10.0, {})
    flat = score(40, 40.0, {})
    assert growing > flat


def test_a_shrinking_literature_scores_below_zero() -> None:
    """Meaningful, not a bug — it sorts to the bottom, which is correct."""
    assert score(10, 40.0, {}) < 0


def test_quality_weight_is_bounded() -> None:
    assert quality_weight({}) == 1.0
    assert quality_weight({StudyType.CASE_REPORT.value: 10}) == 1.0
    assert quality_weight({StudyType.RCT.value: 10}) == 1.5
    mixed = quality_weight(
        {StudyType.RCT.value: 5, StudyType.OBSERVATIONAL.value: 5}
    )
    assert mixed == 1.25


def test_quality_weight_reorders_two_equal_surges() -> None:
    strong = score(20, 5.0, {StudyType.RCT.value: 20})
    weak = score(20, 5.0, {StudyType.CASE_REPORT.value: 20})
    assert strong > weak


# --- the baseline ------------------------------------------------------------


def test_baseline_is_a_mean_not_a_median() -> None:
    """Six mostly-empty buckets must not all baseline to zero.

    A median would make every newcomer's lift identical and destroy the
    ranking's ability to order them.
    """
    assert baseline_from_buckets([0, 0, 0, 0, 6, 6]) == 2.0


def test_baseline_of_no_history_is_zero() -> None:
    assert baseline_from_buckets([]) == 0.0


def test_window_starts_exclude_the_current_window() -> None:
    """Including it would compare a surge against itself."""
    starts = window_starts(date(2026, 9, 4), 14, 3)
    assert starts == [date(2026, 8, 21), date(2026, 8, 7), date(2026, 7, 24)]


# --- the document-frequency backstop -----------------------------------------


def test_ubiquitous_descriptors_are_stoplisted_by_frequency() -> None:
    """The backstop that keeps the curated stoplist from having to be complete."""
    assert is_too_common(41, 100, 0.35) is True
    assert is_too_common(30, 100, 0.35) is False


def test_frequency_ceiling_survives_an_empty_corpus() -> None:
    """A seed can legitimately match nothing over a fortnight."""
    assert is_too_common(0, 0, 0.35) is False


# --- ordering and output -----------------------------------------------------


def test_ties_break_deterministically() -> None:
    """A dry run is a preview, so a rerun has to produce the same list."""
    counts = {"D000002": 10, "D000001": 10}
    baselines = {"D000001": 2.0, "D000002": 2.0}
    first = _rank(current_counts=counts, baseline_counts=baselines)
    second = _rank(current_counts=dict(reversed(counts.items())), baseline_counts=baselines)
    assert [c.substance_ui for c in first] == [c.substance_ui for c in second]


def test_limit_caps_the_desk() -> None:
    counts = {f"D00000{n}": 10 + n for n in range(1, 6)}
    candidates = _rank(current_counts=counts, limit=2)
    assert len(candidates) == 2


def test_candidate_carries_its_arithmetic() -> None:
    """A ranking nobody can audit is a ranking nobody can tune."""
    candidates = _rank(
        current_counts={"D003401": 17},
        baseline_counts={"D003401": 3.0},
        descriptor_names={"D003401": "Creatine", "D003071": "Cognition"},
        study_mixes={"D003401": {StudyType.RCT.value: 3}},
        outcome_cooccurrence={"D003401": {"D003071": 9}},
        pmids={"D003401": [str(n) for n in range(40_000_000, 40_000_010)]},
    )
    candidate = candidates[0]
    assert candidate.topic == "creatine for cognition"
    assert candidate.paper_count == 17
    assert candidate.baseline_count == 3.0
    assert candidate.lift == lift(17, 3.0)
    assert candidate.study_mix == {StudyType.RCT.value: 3}
    # Trimmed: a reviewer checks a couple, and the whole window would bloat
    # every rationale blob on the desk.
    assert len(candidate.top_pmids) == 5

    rationale = candidate.rationale(date(2026, 8, 21), date(2026, 9, 4))
    assert rationale["window"] == {"start": "2026-08-21", "end": "2026-09-04"}
    # Duplicated from the column on purpose: the dismissal rule compares a later
    # scan against what was known at dismissal, and the column will have moved.
    assert rationale["paper_count"] == 17


def test_study_mix_counts_by_stored_value() -> None:
    """JSON as it stands — it goes straight into rationale and out to the UI."""
    mix = study_mix_of([StudyType.RCT, StudyType.RCT, StudyType.OBSERVATIONAL])
    assert mix == {StudyType.RCT.value: 2, StudyType.OBSERVATIONAL.value: 1}


# --- angles: several questions from one surge --------------------------------


def _omega3(**overrides: object) -> list:
    """Omega-3 as it actually appeared: 8 papers over several outcomes.

    Measured 2026-09-06 on the live corpus — 8 papers across 55 outcome
    descriptors, of which 3 carried 4+ papers of their own. The shape below is
    that, trimmed.
    """
    return _rank(
        current_counts={"D015525": 8},
        baseline_counts={"D015525": 3.0},
        descriptor_names={
            "D015525": "Fatty Acids, Omega-3",
            "D009103": "Muscle Strength",
            "D012867": "Skin Aging",
            "D002318": "Cognition",
            "D000067877": "Rare Outcome",
        },
        outcome_cooccurrence={
            "D015525": {
                "D009103": 5,
                "D012867": 4,
                "D002318": 3,
                "D000067877": 1,
            }
        },
        **overrides,
    )


def test_one_substance_yields_several_angles() -> None:
    """The whole point: omega-3 for muscle *and* omega-3 for skin.

    Under the old one-proposal-per-substance identity, promoting either silenced
    the substance and the other angle was never offered — even though they rest
    on different papers and reach different verdicts.
    """
    candidates = _omega3()
    assert len(candidates) == 2
    assert {c.outcome_name for c in candidates} == {"Muscle Strength", "Skin Aging"}
    assert all(c.substance_ui == "D015525" for c in candidates)


def test_angles_are_capped_per_substance() -> None:
    """A surge must not take the whole desk.

    Three outcomes clear the per-angle floor here; only two are emitted. This is
    the counterweight to the change above — without it "more angles" becomes
    "one substance, eight ways".
    """
    assert len(_omega3(max_angles_per_substance=3)) == 3
    assert len(_omega3(max_angles_per_substance=1)) == 1


def test_a_shallow_angle_is_not_proposed() -> None:
    """One paper is not a question worth an article of its own."""
    topics = {c.outcome_name for c in _omega3(max_angles_per_substance=99)}
    assert "Rare Outcome" not in topics


def test_the_angle_floor_is_lower_than_the_substance_floor() -> None:
    """They ask different questions, and conflating them makes angles a no-op.

    ``min_papers`` asks whether the substance is moving; ``min_papers_per_angle``
    asks whether there is enough to write one specific piece. Set equal, omega-3's
    8 papers over 55 outcomes collapse back to a single angle — which is what the
    live data showed before this was split.
    """
    assert len(_omega3(min_papers_per_angle=3)) == 2
    assert len(_omega3(min_papers_per_angle=5)) == 1


def test_deeper_angles_rank_first() -> None:
    """Angles of one substance share its score, so depth is what orders them."""
    candidates = _omega3(max_angles_per_substance=3)
    assert [c.angle_paper_count for c in candidates] == [5, 4, 3]


def test_every_angle_carries_both_counts() -> None:
    """8 papers is why omega-3 is on the desk; 5 is what the article rests on.

    A reviewer needs both, and one number cannot say both.
    """
    candidate = _omega3()[0]
    assert candidate.paper_count == 8
    assert candidate.angle_paper_count == 5
    assert candidate.lift == lift(8, 3.0)
    # The rationale carries the angle count too, because suppression compares a
    # later scan against what was known at the decision.
    assert candidate.rationale(date(2026, 8, 21), date(2026, 9, 4))["angle_paper_count"] == 5


def test_a_substance_with_no_usable_angle_still_proposes() -> None:
    """The pre-angle behaviour, preserved: the substance is still moving."""
    candidates = _rank(
        current_counts={"D003401": 17},
        baseline_counts={"D003401": 3.0},
        descriptor_names={"D003401": "Creatine", "D000001": "Thin Outcome"},
        outcome_cooccurrence={"D003401": {"D000001": 1}},
    )
    assert len(candidates) == 1
    assert candidates[0].outcome_ui is None
    assert candidates[0].topic == "creatine"
    # Falls back to the substance count, so the desk still shows real depth.
    assert candidates[0].angle_paper_count == 17


def test_an_unrecognisable_outcome_does_not_become_an_angle() -> None:
    """Depth is not enough — the outcome has to name a question.

    All three of these were real proposals from the 2026-09-06 scan before this
    rule existed. Anything not identified as a substance falls through to
    "outcome", which is fine for counting papers and far too permissive for
    naming an article.

    Rejection is not refusal: the substance is still proposed under a bare
    topic, exactly as it was before angles existed.
    """
    for outcome in ("Cross-Sectional Studies", "Tea", "Physiological Phenomena"):
        candidates = _rank(
            current_counts={"D015525": 8},
            baseline_counts={"D015525": 3.0},
            descriptor_names={"D015525": "Fatty Acids, Omega-3", "D000001": outcome},
            outcome_cooccurrence={"D015525": {"D000001": 6}},
        )
        assert len(candidates) == 1, outcome
        assert candidates[0].outcome_ui is None, outcome
        assert candidates[0].topic == "omega-3 fatty acids", outcome


def test_a_tautological_angle_is_rejected() -> None:
    """"vitamin d for vitamin d deficiency" asks nothing.

    A real proposal, and one no list catches: the outcome maps to a keyword and
    is deep enough, and still restates the substance.
    """
    candidates = _rank(
        current_counts={"D014807": 9},
        baseline_counts={"D014807": 4.0},
        descriptor_names={"D014807": "Vitamin D", "D014808": "Vitamin D Deficiency"},
        outcome_cooccurrence={"D014807": {"D014808": 7}},
    )
    assert candidates[0].topic == "vitamin d"
