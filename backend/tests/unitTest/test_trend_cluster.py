"""After triage: what a candidate may count as coverage, and what is no topic at all."""

from __future__ import annotations

import uuid

from app.research.contracts import CandidateTopic, Category
from app.research.normalize import is_not_a_topic
from app.research.trends.cluster import attribute, too_broad


def _candidate(
    subject: str, outcome: str = "", queries: list[str] | None = None
) -> CandidateTopic:
    return CandidateTopic(
        topic_id=str(uuid.uuid4()),
        canonical_topic=f"{subject} {outcome}".strip(),
        category=Category.SUPPLEMENTS,
        subject=subject,
        outcome=outcome,
        queries=queries or [subject],
    )


def test_a_query_triage_grouped_wrongly_does_not_count_as_coverage() -> None:
    """Measured: "coneflower for immune support" was handed a phrase about carbs."""
    coneflower = _candidate(
        "coneflower", "immune support", ["coneflower supplement", "echinacea immune", "carbs"]
    )
    items = {"coneflower supplement": {1, 2}, "echinacea immune": {3}, "carbs": {4, 5}}
    ignored = attribute([coneflower], items)
    assert coneflower.item_ids == [1, 2, 3]  # "immune" shares a word with the outcome
    assert ignored == 1
    assert "carbs" in coneflower.queries  # kept as triage grouped it, not counted


def test_a_bare_category_is_too_broad_and_an_angle_is_not() -> None:
    seeds = frozenset({"protein", "strength training"})
    assert too_broad(_candidate("protein"), seeds)
    assert too_broad(_candidate("strength training"), seeds)
    assert too_broad(_candidate("weight loss"), seeds)
    assert too_broad(_candidate("mobility"), seeds)
    assert not too_broad(_candidate("protein", "older adults"), seeds)
    assert not too_broad(_candidate("creatine"), frozenset())
    assert not too_broad(_candidate("heart attack"), seeds)


def test_products_and_shops_are_shopping_intent() -> None:
    assert is_not_a_topic("running shoes")
    assert is_not_a_topic("supplement store near me")
    assert is_not_a_topic("best creatine brands")
    assert not is_not_a_topic("barefoot running and knee pain")
