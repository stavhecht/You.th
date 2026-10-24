"""Candidate normalisation and clustering: deterministic, no model."""

from __future__ import annotations

from app.research.contracts import Category, RawTrend
from app.research.normalize import (
    fallback_subject,
    is_not_a_topic,
    merge_exact,
    merge_similar,
    normalize_query,
    query_key,
)


def _trend(
    query: str, rising: int = 100, *, breakout: bool = False, depth: int = 0
) -> RawTrend:
    return RawTrend(
        query=query,
        seed="creatine",
        category=Category.SUPPLEMENTS,
        source="google_trends",
        rising_percent=rising,
        is_breakout=breakout,
        depth=depth,
    )


def test_normalisation_strips_case_accents_years_and_punctuation() -> None:
    assert normalize_query("Créatine, before BED 2026!") == "creatine before bed"


def test_wording_variants_share_a_key() -> None:
    assert query_key("best creatine gummies") == query_key("creatine gummy")
    assert query_key("gummies creatine") == query_key("creatine gummy")
    assert query_key("creatine before bed 2026") == query_key("Creatine before bed")


def test_different_topics_keep_different_keys() -> None:
    assert query_key("creatine before bed") != query_key("creatine for women")


def test_shopping_and_litigation_queries_are_not_topics() -> None:
    assert is_not_a_topic("creatine gummies amazon")
    assert is_not_a_topic("magnesium price")
    assert is_not_a_topic("creatine lawsuit")
    assert not is_not_a_topic("magnesium glycinate for sleep")
    # All filler, no subject.
    assert is_not_a_topic("what is the best")


def test_exact_merge_keeps_the_strongest_query_as_the_name() -> None:
    clusters = merge_exact(
        [
            _trend("creatine gummy", 150),
            _trend("best creatine gummies", 900),
            _trend("magnesium price", 5000),
            _trend("creatine for women", 300),
        ]
    )

    names = [cluster.representative.query for cluster in clusters]
    assert names == ["best creatine gummies", "creatine for women"]
    assert clusters[0].queries == ["best creatine gummies", "creatine gummy"]


def test_breakouts_rank_above_any_rising_percent() -> None:
    clusters = merge_exact(
        [_trend("creatine for women", 4900), _trend("creatine hair", breakout=True)]
    )

    assert clusters[0].representative.query == "creatine hair"


def test_similar_clusters_merge_into_the_earlier_stronger_one() -> None:
    clusters = merge_exact(
        [
            _trend("creatine before bed", 900),
            _trend("creatine at night", 400),
            _trend("creatine for women", 300),
        ]
    )
    vectors = [[1.0, 0.0], [0.95, 0.05], [0.0, 1.0]]

    merged = merge_similar(clusters, vectors, threshold=0.9)

    assert [c.representative.query for c in merged] == [
        "creatine before bed",
        "creatine for women",
    ]
    assert merged[0].queries == ["creatine before bed", "creatine at night"]
    assert [c.cluster_id for c in merged] == [0, 1]


def test_fallback_subject_drops_filler() -> None:
    assert fallback_subject("what is the best creatine for women") == "creatine women"


def test_spam_in_a_rising_list_is_off_topic() -> None:
    """Measured 2026-09-30: Trends' rising list for "fitness" and "protein"."""
    from app.research.normalize import is_on_topic

    for spam in ("learn golang", "cheap flights new york", "home security camera"):
        assert not is_on_topic(spam, "fitness")
    assert is_on_topic("creatine before bed", "creatine")
    assert is_on_topic("mouth taping", "sleep") is False
    assert is_on_topic("mouth taping for sleep", "sleep")
    assert is_on_topic("zone 2 cardio", "running")


def test_product_review_queries_are_not_topics() -> None:
    assert is_not_a_topic("protein powder review")
    assert is_not_a_topic("tax software comparison")
    assert not is_not_a_topic("protein before bed")


def test_merge_drops_off_topic_trends() -> None:
    from app.research.normalize import off_topic

    trends = [
        RawTrend(
            query="learn golang",
            seed="protein",
            category=Category.NUTRITION,
            source="google_trends",
            rising_percent=9900,
        ),
        RawTrend(
            query="whey protein isolate",
            seed="protein",
            category=Category.NUTRITION,
            source="google_trends",
            rising_percent=300,
        ),
    ]
    assert [t.query for t in off_topic(trends)] == ["learn golang"]
    assert [c.representative.query for c in merge_exact(trends)] == ["whey protein isolate"]


def test_the_seed_itself_is_not_a_trend() -> None:
    from app.research.normalize import off_topic

    trends = [
        RawTrend(
            query="Sleep",
            seed="sleep",
            category=Category.SLEEP_RECOVERY,
            source="google_trends",
            rising_percent=200,
        ),
        RawTrend(
            query="sleep apnea mouth tape",
            seed="sleep",
            category=Category.SLEEP_RECOVERY,
            source="google_trends",
            rising_percent=150,
        ),
    ]
    assert [t.query for t in off_topic(trends)] == ["Sleep"]
