"""Composing the one string discovery contributes to an article.

The output of ``compose_topic`` becomes ``pipeline_runs.topic`` verbatim, and
everything downstream — product, ingredients, the PubMed query, the verdict — is
derived from it by the extraction model. The last test in this file is the one
that matters most: it checks that a composed topic still anchors a query, because
``UnanchoredQuery`` is a non-retryable stage failure and a topic that trips it
burns a model call for nothing.
"""

from __future__ import annotations

import pytest

from app.discovery.topics import (
    compose_topic,
    humanise_descriptor,
    outcome_keyword_for,
    rank_outcomes,
)
from app.discovery.vocab import BOTANICAL_UIS
from app.retrieval.query_builder import TemplateQueryStrategy, UnanchoredQuery

# --- humanising a MeSH heading ----------------------------------------------


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Fatty Acids, Omega-3", "omega-3 fatty acids"),
        ("Creatine", "creatine"),
        ("Withania", "withania"),
        ("Vitamin D", "vitamin d"),
        # Only the first comma is un-inverted. MeSH uses later ones for
        # qualifiers that are not part of the name, and reassembling all of them
        # produces word salad.
        ("Tea, Herbal, Preparation", "herbal tea"),
        # Parenthetical glosses are index apparatus, not prose.
        ("Panax (plant)", "panax"),
        ("  Spaced   Out  ", "spaced out"),
    ],
)
def test_humanise_descriptor(heading: str, expected: str) -> None:
    assert humanise_descriptor(heading) == expected


# --- picking the outcome -----------------------------------------------------


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Sleep Quality", "sleep"),
        ("Cognition", "cognition"),
        # Longest match wins, so a compound key is not shadowed by a fragment
        # of itself. "Muscle Strength" legitimately matches both `muscle` and
        # `strength`; either is a fine hint, and picking one keeps the ranking
        # deterministic.
        ("Muscle Strength", "strength"),
        ("Blood Sugar Regulation", "blood sugar"),
        # No substring in either direction connects these to their spoken name,
        # which is what _HEADING_SYNONYMS exists for. The microbiome case is not
        # an edge case — it is one of the eight seeds.
        ("Gastrointestinal Microbiome", "gut"),
        ("Insulin Resistance", "blood sugar"),
        ("Body Composition", "weight"),
        ("Physiological Phenomena", None),
    ],
)
def test_outcome_keyword_for(heading: str, expected: str | None) -> None:
    assert outcome_keyword_for(heading) == expected


def test_recognised_outcome_beats_a_more_frequent_unrecognised_one() -> None:
    """The recognised word is what a reader searches for.

    It is also what ``TemplateQueryStrategy`` can expand into real MeSH search
    terms, so preferring it improves the eventual retrieval as well as the
    sentence.
    """
    ranked = rank_outcomes(
        {"D000001": 9, "D012890": 4},
        {"D000001": "Physiological Phenomena", "D012890": "Sleep"},
    )
    assert ranked[0] == ("D012890", "Sleep")


def test_rank_outcomes_is_deterministic() -> None:
    counts = {"D000001": 5, "D000002": 5}
    names = {"D000001": "Alpha", "D000002": "Beta"}
    assert rank_outcomes(counts, names) == rank_outcomes(dict(reversed(counts.items())), names)


def test_rank_outcomes_of_nothing_is_empty() -> None:
    assert rank_outcomes({}, {}) == []


# --- the topic ---------------------------------------------------------------


def test_compose_topic_puts_the_substance_first() -> None:
    """The shape CreateRunRequest documents and extraction was tuned on."""
    assert compose_topic("Eurycoma", "Testosterone") == "eurycoma for testosterone"


def test_compose_topic_without_an_outcome_is_bare() -> None:
    """A guessed outcome would put a claim in the topic nothing suggested.

    A bare topic still extracts and still retrieves — the query is anchored on
    the subject with no outcome group, which ``_compose`` supports explicitly.
    """
    assert compose_topic("Creatine", None) == "creatine"


def test_compose_topic_refuses_to_repeat_itself() -> None:
    assert compose_topic("Creatine", "Creatine") == "creatine"


# --- the failure this module exists to avoid ---------------------------------


def test_every_botanical_composes_an_anchorable_topic() -> None:
    """A composed topic must survive ``TemplateQueryStrategy``.

    ``UnanchoredQuery`` is a **non-retryable** stage failure: a promoted
    candidate whose topic cannot anchor a query burns the extraction call and
    dies at retrieval, with nothing recoverable by a retry. Botanicals are the
    sharpest case because their MeSH headings are organism names that look
    nothing like a product.

    Standing in for the extraction model here: whatever it returns for
    ``ingredients`` — and a small local model routinely returns nothing — the
    bare substance must still work as ``product``.
    """
    strategy = TemplateQueryStrategy()
    for name in BOTANICAL_UIS.values():
        topic = compose_topic(name, "Fatigue")
        product = humanise_descriptor(name)
        queries = strategy.build(claim=topic, product=product, ingredients=[])
        assert queries, f"{name!r} produced no query"


def test_an_empty_product_still_raises() -> None:
    """The guard above is only meaningful because the failure is real."""
    strategy = TemplateQueryStrategy()
    with pytest.raises(UnanchoredQuery):
        strategy.build(claim="something", product="   ", ingredients=[])
