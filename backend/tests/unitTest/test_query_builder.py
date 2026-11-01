"""``TemplateQueryStrategy`` — anchoring the search to a substance.

Every test here is about one thing: the query has to name what the article is
about. A query that names only the *outcome* still returns papers, still ranks
them, still gets cited, and still passes citation validation — the article is
simply about a different question, and nothing downstream can tell. This is
the one place where that is still detectable.

The regression these tests pin was live: a run on "creatine improve your
workout" searched ``(workout AND performance) AND (workout AND performance)``
and produced a ``supported`` verdict citing a CrossFit dietary review and a
multi-ingredient pre-workout trial, neither of which mentions creatine.
"""

from __future__ import annotations

import pytest

from app.retrieval.query_builder import (
    GENERAL_PASS_MAX_RESULTS,
    REVIEW_PASS_MAX_RESULTS,
    TemplateQueryStrategy,
    UnanchoredQuery,
)


def _terms(claim: str, product: str, ingredients: list[str] | None = None) -> str:
    """The composed query string, asserted identical across both passes."""
    queries = TemplateQueryStrategy().build(claim, product, ingredients or [])
    assert len({query.terms for query in queries}) == 1
    return queries[0].terms


# ---------------------------------------------------------------------------
# The subject group: what the query is about.
# ---------------------------------------------------------------------------


def test_product_anchors_the_query_when_extraction_returned_no_ingredients() -> None:
    """The regression. ``ingredients`` is optional in ``ExtractionOutput`` and
    small local models leave it empty routinely, so the required ``product``
    field is what has to carry the subject."""
    terms = _terms("improves workout performance", "creatine", [])

    assert "Creatine" in terms
    assert terms == "(Creatine) AND (workout AND performance)"


def test_ingredients_are_preferred_over_the_product_name() -> None:
    """A multi-ingredient product's own name is a brand with no literature
    behind it. ORing it in would only widen the query with a dead term."""
    terms = _terms("reduces stress", "SuperBlend X", ["ashwagandha", "rhodiola"])

    assert terms.startswith("(Withania OR Rhodiola)")
    assert "superblend" not in terms.lower()


def test_a_packaged_product_name_still_maps_to_its_mesh_term() -> None:
    """Product names carry packaging. An exact-key lookup misses all of them,
    and quoting the whole string retrieves nothing — which reads as "no
    literature on this" and is a worse failure than being off-topic, because
    ``no_evidence`` is a publishable verdict."""
    assert _terms("supports muscle", "Creatine Monohydrate Powder").startswith("(Creatine)")
    assert _terms("reduces stress", "Ashwagandha KSM-66 Capsules").startswith("(Withania)")


def test_the_longest_matching_substance_wins() -> None:
    """``withania somnifera`` and ``withania`` are both keys; the specific one
    is not allowed to lose to the prefix."""
    assert _terms("reduces stress", "Withania Somnifera Root").startswith("(Withania)")


def test_a_botanical_name_in_parentheses_is_still_extracted() -> None:
    """Pre-existing behaviour, kept: the model often supplies both names."""
    terms = _terms("reduces stress", "capsules", ["ashwagandha (Withania somnifera)"])

    assert terms.startswith("(Withania)")


def test_an_unmapped_substance_is_searched_as_a_quoted_phrase() -> None:
    """MESH_HINTS is a hand-mapped shortlist, not a lookup service. Anything
    outside it is still a substance and still anchors the query."""
    terms = _terms("supports focus", "citicoline")

    assert terms.startswith('("citicoline")')


# ---------------------------------------------------------------------------
# The failure the old code degraded into silently.
# ---------------------------------------------------------------------------


def test_no_substance_at_all_raises_rather_than_falling_back_to_keywords() -> None:
    """This used to log a warning and search the claim keywords. A warning on
    a worker's stdout is not a control: the run completed and shipped an
    off-topic article into the review queue."""
    with pytest.raises(UnanchoredQuery) as excinfo:
        TemplateQueryStrategy().build("improves workout performance", "   ", [])

    assert "improves workout performance" in str(excinfo.value)


def test_blank_ingredients_do_not_count_as_an_anchor() -> None:
    """``[""]`` is a non-empty list, so it wins the ``ingredients or [product]``
    choice — and then contributes no terms. Guarded explicitly because the
    truthiness check makes this the one input that skips the product."""
    with pytest.raises(UnanchoredQuery):
        TemplateQueryStrategy().build("reduces stress", "ashwagandha", ["", "  "])


def test_the_claim_is_never_the_whole_query() -> None:
    """The composed query always leads with the subject group. If the outcome
    hints miss, the claim keywords narrow the subject — they never replace it."""
    terms = _terms("makes you feel wonderful", "melatonin")

    assert terms.startswith("(Melatonin) AND ")


# ---------------------------------------------------------------------------
# The two passes.
# ---------------------------------------------------------------------------


def test_both_passes_are_built_reviews_first_with_identical_terms() -> None:
    """Ranking bias alone cannot promote a review that never entered the pool,
    so the review-filtered pass runs as its own query."""
    reviews, general = TemplateQueryStrategy().build("reduces stress", "ashwagandha", [])

    assert reviews.reviews_only is True
    assert reviews.max_results == REVIEW_PASS_MAX_RESULTS
    assert general.reviews_only is False
    assert general.max_results == GENERAL_PASS_MAX_RESULTS
    assert reviews.terms == general.terms


def test_min_year_reaches_both_passes() -> None:
    strategy = TemplateQueryStrategy(min_year=2015)
    for query in strategy.build("reduces stress", "ashwagandha", []):
        assert query.min_year == 2015


# ---------------------------------------------------------------------------
# A question is not a product name.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "product",
    [
        "Is it bad for you?",
        "Does stretching before exercise prevent injuries",
        "Meta-analytic evidence on weekly resistance training volume and muscle hypertrophy",
        "unspecified",
    ],
)
def test_a_question_or_sentence_never_anchors_a_query(product: str) -> None:
    """Searched as a quoted phrase it finds nothing, and the run publishes a
    false "no evidence" verdict. Unanchored, it fails visibly instead."""
    with pytest.raises(UnanchoredQuery):
        TemplateQueryStrategy().build("reduces injury risk", product, [])


def test_a_known_substance_inside_a_question_still_anchors() -> None:
    assert _terms("improves strength", "Does creatine improve muscle strength?").startswith(
        "(Creatine)"
    )


def test_a_short_unmapped_practice_still_anchors() -> None:
    assert _terms("improves endurance", "zone 2 training").startswith('("zone 2 training")')


def test_a_specific_outcome_keeps_its_own_phrase_and_the_direction_is_dropped() -> None:
    """2026-10-23: "reduces sleep latency" searched only generic sleep vocabulary,
    and melatonin's meta-analysis on sleep onset never reached the pool."""
    from app.retrieval.query_builder import REVIEW_PASS_MAX_RESULTS, TemplateQueryStrategy

    reviews, _ = TemplateQueryStrategy().build("reduces sleep latency", "melatonin", [])
    assert '"sleep latency"[tiab]' in reviews.terms
    assert reviews.max_results == REVIEW_PASS_MAX_RESULTS == 30

    lowers, _ = TemplateQueryStrategy().build("lowers blood pressure", "beetroot", [])
    assert "lowers" not in lowers.terms


def test_usage_words_are_dropped_from_an_unmapped_product_before_quoting() -> None:
    """2026-10-25: "daily sunscreen use" as one phrase missed the trial titled
    "regular sunscreen use"."""
    assert _terms("improves sleep", "regular foam rolling use").startswith('("foam rolling")')
    assert _terms("improves skin", "topical niacinamide").startswith('("niacinamide")')
    # A name of nothing but usage words keeps its subject rather than emptying it.
    assert _terms("improves sleep", "daily use").startswith('("daily use")')
    # A qualifier that means something else alone is never what is left.
    dietary = _terms("improves health", "dietary supplements")
    assert dietary.startswith('("dietary supplements")')
    assert _terms("improves health", "oral microbiome").startswith('("oral microbiome")')


def test_a_product_naming_examples_searches_the_category_and_each_example() -> None:
    """2026-10-25: "smart rings like the oura ring" as one phrase found 2 papers."""
    terms = _terms("measures sleep accurately", "smart rings like the Oura Ring")
    assert terms.startswith('("smart rings" OR "oura ring")')
    assert _terms("improves sleep", "GLP-1 drugs such as semaglutide").startswith(
        '("glp-1 drugs" OR "semaglutide")'
    )


def test_menstrual_cycle_training_searches_indexed_terms_not_the_phrase() -> None:
    """2026-10-25: the quoted practice phrase found nothing; these reach the
    phase-and-performance meta-analysis (PMID 32661839)."""
    terms = _terms("improves fitness results", "syncing workouts to the menstrual cycle")
    assert terms.startswith('("Menstrual Cycle")')
    assert "athletic performance" in terms and "fitness AND results" not in terms


def test_sunscreen_maps_to_its_mesh_term_and_skin_cancer_to_its_own_vocabulary() -> None:
    terms = _terms("prevents skin cancer", "daily sunscreen use")
    assert terms.startswith('("Sunscreening Agents")')
    assert "skin neoplasms" in terms and "melanoma" in terms
    # The shorter "skin" key inside "skin cancer" must not add skin-aging papers.
    assert "skin aging" not in terms
    assert "skin aging" in _terms("reduces skin aging", "retinol")


def test_a_harm_claim_also_searches_the_substances_safety_record() -> None:
    """2026-10-26: each side effect searched alone missed the safety reviews."""
    terms = _terms("increases kidney injury risk", "creatine")
    assert '"adverse effects"[sh]' in terms and "kidney" in terms
    assert "adverse effects" not in _terms("improves memory", "creatine")


def test_an_outcome_word_inside_another_word_does_not_match() -> None:
    """"distress" searched anxiety papers through the "stress" hint."""
    assert "cortisol" not in _terms("causes gastrointestinal distress", "ashwagandha")
    assert "cortisol" in _terms("reduces stress", "ashwagandha")
    assert "osteoarthritis" in _terms("relieves sore joints", "glucosamine")
