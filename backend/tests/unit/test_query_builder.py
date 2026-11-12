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

import re

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

    assert terms.startswith('("citicoline"[tiab] OR "citicolines"[tiab])')


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
    terms = _terms("improves endurance", "zone 2 training")
    assert terms.startswith('(("zone 2 training"[tiab] OR ("zone"[tiab] AND "2"[tiab]')


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
    terms = _terms("improves sleep", "regular foam rolling use")
    assert terms.startswith('(("foam rolling"[tiab] OR ("foam"[tiab] AND "rolling"[tiab])))')
    assert _terms("improves skin", "topical niacinamide").startswith(
        '("niacinamide"[tiab] OR "niacinamides"[tiab])'
    )
    # A name of nothing but usage words keeps its subject rather than emptying it.
    terms = _terms("improves sleep", "daily use")
    assert '"daily use"[tiab]' in terms and '"daily uses"[tiab]' in terms
    # A qualifier that means something else alone is never what is left.
    dietary = _terms("improves health", "dietary supplements")
    assert '"dietary supplements"[tiab]' in dietary and '"dietary supplement"[tiab]' in dietary
    assert '"oral microbiome"[tiab]' in _terms("improves health", "oral microbiome")


def test_a_product_naming_examples_searches_the_category_and_each_example() -> None:
    """2026-10-25: "smart rings like the oura ring" as one phrase found 2 papers."""
    terms = _terms("measures sleep accurately", "smart rings like the Oura Ring")
    assert terms.startswith(
        '(("smart rings"[tiab] OR ("smart"[tiab] AND "rings"[tiab])) OR '
        '("smart ring"[tiab] OR'
    )
    assert _terms("improves sleep", "GLP-1 drugs such as semaglutide").startswith(
        '(("glp-1 drugs"[tiab] OR ("glp-1"[tiab] AND "drugs"[tiab]))'
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


@pytest.mark.parametrize(
    ("product", "quoted"),
    [
        ("Smart ring for sleep", '"smart ring"'),
        ("Home gym for strength training", '"home gym"'),
    ],
)
def test_an_unmapped_topic_label_searches_the_thing_not_the_whole_label(
    product: str, quoted: str
) -> None:
    terms = TemplateQueryStrategy().build("measures sleep quality", product, [])[-1].terms
    assert quoted in terms and " for " not in terms
    assert quoted.rstrip('"') + 's"' in terms  # and its plural


def test_a_substance_named_after_for_is_not_the_subject() -> None:
    strategy = TemplateQueryStrategy()
    terms = strategy.build("corrects deficiency", "Smart ring for magnesium", [])[-1].terms
    assert '"smart ring"' in terms and "agnesium" not in terms


@pytest.mark.parametrize(
    ("product", "forms"),
    [
        ("smart rings", ['"smart rings"', '"smart ring"']),
        ("cold therapy", ['"cold therapy"', '"cold therapies"']),
        ("recovery therapies", ['"recovery therapies"', '"recovery therapy"']),
        ("sauna", ['"sauna"', '"saunas"']),
    ],
)
def test_an_unmapped_name_is_searched_in_both_forms(product: str, forms: list[str]) -> None:
    terms = TemplateQueryStrategy().build("improves sleep", product, [])[-1].terms
    assert all(form in terms for form in forms)


@pytest.mark.parametrize(
    "product", ["stress", "vitamin k2", "zone 2", "tinnitus", "foam rolling", "zone 2 training"]
)
def test_a_name_with_no_plural_form_stays_one_phrase(product: str) -> None:
    terms = TemplateQueryStrategy().build("improves sleep", product, [])[-1].terms
    subject = terms.split(" AND (")[0] if " AND (" in terms else terms
    assert f'"{product}"[tiab]' in subject and f'"{product}s"' not in subject, terms


@pytest.mark.parametrize("product", ["dates", "greens", "hops"])
def test_a_lone_plural_is_not_searched_as_another_word(product: str) -> None:
    """ "date" is in every abstract saying "to date", and the subject anchors a search."""
    terms = TemplateQueryStrategy().build("improves sleep", product, [])[-1].terms
    assert terms.startswith(f'("{product}"[tiab])')


def test_how_a_question_words_its_claim_is_not_searched() -> None:
    terms = TemplateQueryStrategy().build("replaces commercial gym", "home gym", [])[-1].terms
    assert "commercial AND gym" in terms and "replaces" not in terms


def _subject_of(product: str, claim: str = "improves sleep") -> str:
    return TemplateQueryStrategy().broaden(claim, product, [])[0].terms


@pytest.mark.parametrize(
    ("product", "expected", "absent"),
    [
        # 2026-11-07: each searched as one literal phrase. "ice baths and cold plunges" had
        # one PubMed hit, against 8 for the two names.
        ("ice baths and cold plunges", ['"ice baths"', '"cold plunges"'], ["and cold"]),
        ("foam rolling or fascia release", ['"foam rolling"', '"fascia release"'], ["or f"]),
        ("saunas, steam rooms", ['"saunas"', '"steam rooms"'], [","]),
        ("creatine and beta-alanine", ["Creatine", '"beta-Alanine"'], [" and "]),
        ("creatine or beta-alanine", ["Creatine", '"beta-Alanine"'], [" or b"]),
        ("fish oil and/or krill oil", ['"Fish Oils"', '"krill oil"'], ["and/"]),
        # A real alternative beside a known substance: this searched turmeric alone.
        ("green juice or turmeric shots", ["Curcuma", '"green juice"'], [" or t"]),
        # Ten words, but a list: this searched turmeric alone (wt_039).
        (
            "green juices or celery juice or ginger and turmeric shots",
            ["Curcuma", "Ginger", '"green juices"', '"celery juice"'],
            [" or c"],
        ),
        # A known word is its own name, not half of "ashwagandha magnesium".
        (
            "sleep magnesium or ashwagandha or melatonin",
            ["Withania", "Melatonin"],
            ["Magnesium OR Withania OR Magnesium"],
        ),
        ("magnesium or vitamin d", ["Magnesium", '"Vitamin D"'], [" or v"]),
    ],
)
def test_two_names_are_searched_as_two_names(
    product: str, expected: list[str], absent: list[str]
) -> None:
    terms = _subject_of(product)
    assert all(term in terms for term in expected), terms
    assert not any(text in terms for text in absent), terms


@pytest.mark.parametrize(
    ("product", "subject"),
    [
        # A known substance anchors alone: every other word here matches off-subject papers.
        ("vitamin d and depression", '("Vitamin D")'),
        ("creatine and resistance training", "(Creatine)"),
        ("2025 trials on vitamin d and immune health", '("Vitamin D")'),
        ("melatonin (low dose)", "(Melatonin)"),
        ("creatine (5 g/day)", "(Creatine)"),
        # A number is not a name.
        ("sauna (3 times a week)", '("sauna" OR "saunas")'),
        # 2026-11-07, PubMed: 357,271 and 104,560 papers before these two.
        ("intermittent fasting (low dose)", '(("intermittent fasting" OR ("intermittent" AND'),
        ("creatine during the menstrual cycle", "(Creatine)"),
        # A long part is half a sentence, not a name: the limit stands for it.
        ("a daily habit of taking creatine or doing some yoga to gain muscle", "(Creatine)"),
        ("16/8 intermittent fasting", '(("16/8 intermittent fasting" OR ("16/8" AND'),
        ("5/2 diet", '("5/2 diet" OR "5/2 diets")'),
        # "and" joins words of one name, or a subject to its outcome.
        ("red and blue light therapy", '(("red and blue light therapy" OR'),
        ("mind and body practices", '(("mind and body" OR ("mind" AND "body"))'),
        ("head and shoulders shampoo", '(("head and shoulders shampoo" OR'),
        ("protein timing and muscle hypertrophy", '(("protein timing and muscle'),
        ("standing desks and posture correction", '(("standing desks and posture'),
        # A lone word beside a longer name is half of that name.
        ("hot or cold compresses", '("hot compresses" OR'),
        ("mobility or functional movement training", '(("mobility training" OR'),
        ("ketogenic versus low-fat diets", '(("ketogenic diets" OR'),
        ("ankle or wrist weights while walking", '(("ankle weights" OR'),
    ],
)
def test_a_search_does_not_drift_off_the_subject(product: str, subject: str) -> None:
    """Measured on eval traces, 2026-11-07: 43 of 242 names split, many into a word that
    is no subject at all. An off-subject paper still gets cited and still validates."""
    terms = _subject_of(product).replace("[tiab]", "")
    assert terms.startswith(subject), terms


@pytest.mark.parametrize(
    ("product", "kept", "dropped"),
    [
        ("drinking warm kale juice in the morning", "drinking warm kale juice", "morning"),
        ("bulletproof coffee with butter or mct oil", "bulletproof coffee", "mct"),
        ("parabens and sulfates in cosmetics", "sulfates", "cosmetics"),
        ("smart water bottles with hydration reminders", "smart water bottle", "reminders"),
    ],
)
def test_when_and_how_are_not_part_of_the_name(product: str, kept: str, dropped: str) -> None:
    terms = _subject_of(product)
    assert kept in terms and dropped not in terms, terms


@pytest.mark.parametrize(
    "product",
    ["sleeping with an onion in your sock", "chanting while lifting weights"],
)
def test_a_practice_defined_by_its_context_stays_whole(product: str) -> None:
    """Bare "sleeping" would find the whole sleep literature for a question nobody has
    studied."""
    assert f'"{product}"' in _subject_of(product)


def test_before_and_after_are_timing_even_after_one_word() -> None:
    """Whole, "stretching before or after a workout" is a phrase no abstract contains."""
    terms = _subject_of("stretching before or after a workout")
    assert '"stretching"' in terms and "workout" not in terms and "after" not in terms, terms


@pytest.mark.parametrize(
    ("product", "kept"),
    [
        ("vagus nerve stimulation devices", '"vagus nerve stimulation"'),
        ("scalp care products", '"scalp care"'),
        ("hydrafacial treatments", '"hydrafacial"'),
    ],
)
def test_a_generic_last_word_is_not_part_of_the_name(product: str, kept: str) -> None:
    assert kept in _subject_of(product)


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        ("GLP-1 drugs (Ozempic, Wegovy)", ['"glp-1 drugs"', '"ozempic"', '"wegovy"']),
        ("one meal a day (OMAD)", ['"one meal a day"', '"omad"']),
    ],
)
def test_a_name_in_brackets_is_searched_too(product: str, expected: list[str]) -> None:
    terms = _subject_of(product)
    assert all(term in terms for term in expected), terms


def test_the_comma_in_a_number_does_not_split_a_name() -> None:
    assert '"walking 10,000 steps a day"' in _subject_of("walking 10,000 steps a day")


def test_timing_words_in_a_claim_do_not_empty_the_search() -> None:
    terms = _general("improves balance when done before exercise", "stretching")
    assert terms.endswith("(balance AND exercise)"), terms


def test_a_long_description_with_or_in_it_is_still_refused() -> None:
    """Split first, and each half would pass as a name of eight words or fewer."""
    product = "Meta-analytic evidence on weekly resistance training volume or muscle gain"
    with pytest.raises(UnanchoredQuery):
        TemplateQueryStrategy().build("improves strength", product, [])


def _general(claim: str, product: str = "continuous glucose monitoring") -> str:
    return TemplateQueryStrategy().build(claim, product, [])[1].terms


def test_who_the_claim_is_about_narrows_the_search() -> None:
    """wt_002: "without diabetes" had no review in PubMed, "nondiabetic" beside it had 9."""
    terms = _general("improves health in people without diabetes")
    assert '"without diabetes"[tiab]' in terms and "nondiabetic[tiab]" in terms


def test_the_population_does_not_become_the_outcome() -> None:
    """As an outcome word, "diabetes" on its own finds the papers about diabetics."""
    terms = _general("improves health in people without diabetes")
    population = terms.index("nondiabetic")
    assert "diabetes AND" not in terms[population:] and "AND people" not in terms
    assert "health" in terms


def test_a_claim_that_only_names_a_population_searches_subject_and_population() -> None:
    terms = _general("benefits women", "creatine")
    assert terms.startswith("(Creatine) AND (women[tiab]") and "benefits" not in terms


@pytest.mark.parametrize(
    "claim", ["is safe for pregnant women", "is safe for women during pregnancy"]
)
def test_pregnant_women_does_not_search_all_women(claim: str) -> None:
    terms = _general(claim, "melatonin")
    assert "pregnant[tiab]" in terms and "women[tiab]" not in terms, terms
    assert "women" not in terms.rsplit(" AND ", 1)[-1], "nor as an outcome word"


def test_men_and_women_search_either() -> None:
    terms = _general("improves strength in men and women", "creatine")
    assert "women[tiab]" in terms and "men[tiab]" in terms


def test_men_does_not_match_inside_women() -> None:
    assert "men[tiab] OR male" not in _general("improves strength in women", "creatine")


def test_broadening_keeps_the_population() -> None:
    terms = TemplateQueryStrategy().broaden("improves sleep in older adults", "melatonin", [])
    assert '"older adults"[tiab]' in terms[0].terms and "sleep" not in terms[0].terms


def test_a_claim_with_no_population_searches_as_before() -> None:
    assert _general("improves sleep", "melatonin") == (
        '(Melatonin) AND ("sleep"[MeSH] OR insomnia OR "sleep quality")'
    )


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        # The papers' own words, 2026-11-07: PubMed hits with the outcome beside them went
        # 1 -> 109, 3 -> 105 and 0 -> 147 against the questions' wording.
        ("ice baths or cold plunges", ['"cold water immersion"', '"ice baths"']),
        ("alcohol reduction", ['"alcohol reduction"', '"reduced alcohol"']),
        ("cutting down on alcohol", ['"alcohol reduction"']),
        ("lemon water", ['"lemon water"', '"lemon juice"']),
        ("continuous glucose monitor", ['"continuous glucose monitoring"']),
    ],
)
def test_a_name_with_several_wordings_searches_them_all(
    product: str, expected: list[str]
) -> None:
    terms = _subject_of(product)
    assert all(term in terms for term in expected), terms


@pytest.mark.parametrize(
    "claim",
    [
        "reduces skin aging as effectively as retinol",
        "reduces skin aging compared with retinol",
        "reduces skin aging more than retinol",
    ],
)
def test_a_comparison_does_not_blur_the_outcome(claim: str) -> None:
    """Its words joined the claim's phrase, and "skin aging retinol" is in no abstract. The
    comparator still reaches ranking and the article, through the claim itself."""
    terms = _general(claim, "bakuchiol")
    assert '"skin aging"[tiab]' in terms and "retinol" not in terms, terms


def test_a_part_that_is_only_a_usage_word_is_not_a_second_name() -> None:
    """wt_088: "hyaluronic acid serums or supplements" searched the word "supplements"."""
    terms = _subject_of("hyaluronic acid serums or supplements")
    assert '"hyaluronic acid"' in terms and "supplement" not in terms, terms


def test_a_field_of_only_usage_words_is_still_searched() -> None:
    assert '"herbal supplements"' in _subject_of("herbal supplements")


def test_a_claim_that_is_only_a_comparison_keeps_its_words() -> None:
    """wt_083: with the comparison gone nothing was left, so it searched the subject alone,
    and all five of its top papers were off-topic."""
    terms = _general("is as effective as a multi-step routine", "simplified skincare routine")
    assert "multi-step" in terms and "routine" in terms.split(" AND ", 1)[1], terms


@pytest.mark.parametrize(
    "product",
    ["simplified skincare routine", "glass skin", "sound baths or wild swimming", "ice bath"],
)
def test_every_quoted_name_is_held_to_title_and_abstract(product: str) -> None:
    """PubMed drops the quotes from a phrase it has not indexed and searches the loose words:
    "glazed-donut skin" found 1,059,975 papers, on skin, and "simplified skincares" 125,885
    (2026-11-07). With [tiab] those are 0, and a real phrase keeps its count (1,149 both
    ways for "cold water immersion")."""
    terms = _subject_of(product)
    quoted = re.findall(r'"[^"]+"(\[tiab\])?', terms)
    assert quoted and all(quoted), terms


def test_a_name_of_several_words_is_found_with_its_words_apart() -> None:
    """As a phrase, "whole-body vibration plate training" matches no abstract, and "wall
    pilates" none; with every word required, 46 and 15, while "glazed-donut skin" stays at 0
    where PubMed's own fallback found 1,059,975 (2026-11-07)."""
    terms = _subject_of("wall pilates")
    assert '"wall pilates"[tiab]' in terms and '("wall"[tiab] AND "pilates"[tiab])' in terms


def test_small_words_are_not_required() -> None:
    terms = _subject_of("mind and body")
    assert '("mind"[tiab] AND "body"[tiab])' in terms and '"and"[tiab]' not in terms


@pytest.mark.parametrize(
    ("product", "names"),
    [
        ("plant-based or flexitarian diets", ["plant-based diets", "flexitarian diets"]),
        ("exosome or polynucleotide skin treatments", ["exosome", "polynucleotide skin"]),
        ("kefir or probiotic yogurt", ["kefir yogurt", "probiotic yogurt"]),
    ],
)
def test_a_lone_word_beside_a_longer_name_takes_its_last_word(
    product: str, names: list[str]
) -> None:
    """Kept whole, "plant-based or flexitarian diets" is a phrase in no abstract."""
    terms = _subject_of(product)
    assert all(f'"{name}"[tiab]' in terms for name in names), terms


def test_a_name_in_brackets_does_not_count_toward_a_description_s_length() -> None:
    """wt_035, refused for nine words, two of them its own abbreviation."""
    assert "omad" in _subject_of("one meal a day (omad) or 16:8 time-restricted eating")


@pytest.mark.parametrize("product", ["one meal a day", "red meat", "hot yoga"])
def test_common_short_words_do_not_stand_alone_as_a_name(product: str) -> None:
    """"one" AND "meal" AND "day" found 4,411 papers, the phrase 21 (2026-11-07). A name
    needs two words of some length before its words can be searched apart."""
    assert " AND " not in _subject_of(product), _subject_of(product)
