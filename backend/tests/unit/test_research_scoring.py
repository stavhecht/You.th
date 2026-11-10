"""The evidence check's arithmetic and the selection rules.

The trend score has its own suite (``test_trend_scoring.py``). What is here:
evidence tiers from counts, the evidence gate, ranking by trend score with
evidence only as a tie-break, the diversity rules, and the settings that
guard all of it.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import Settings
from app.domain.enums import ResearchCandidateStatus
from app.research import scoring
from app.research.contracts import (
    CandidateTopic,
    Category,
    EvidenceCounts,
    EvidenceStatus,
    ResearchConfig,
    ResearchParams,
    ScienceSignal,
    WebResult,
    WebSignal,
)
from app.research.trends.contracts import TrendScores


def _counts(
    total: int = 40, recent: int = 5, reviews: int = 2, rcts: int = 4
) -> EvidenceCounts:
    return EvidenceCounts(
        source="pubmed",
        query="q",
        total=total,
        recent=recent,
        reviews_or_meta=reviews,
        rcts=rcts,
    )


def _candidate(
    subject: str = "creatine",
    *,
    overall: float | None = 60.0,
    counts: EvidenceCounts | None = None,
    category: Category = Category.SUPPLEMENTS,
    coverage: float = 50.0,
) -> CandidateTopic:
    candidate = CandidateTopic(
        topic_id=str(uuid.uuid4()),
        canonical_topic=f"{subject} topic",
        category=category,
        subject=subject,
        queries=[f"{subject} topic"],
        science=ScienceSignal(primary=counts),
        scores=TrendScores(overall=overall, components={"coverage": coverage}),
    )
    candidate.evidence_status = scoring.evidence_status(counts)
    return candidate


# --- evidence --------------------------------------------------------------


@pytest.mark.parametrize(
    ("counts", "status"),
    [
        (_counts(total=0, recent=0, reviews=0, rcts=0), EvidenceStatus.NONE),
        (_counts(total=4, recent=0, reviews=0, rcts=0), EvidenceStatus.LIMITED),
        (_counts(total=12, recent=4, reviews=0, rcts=0), EvidenceStatus.EMERGING),
        (_counts(total=12, recent=0, reviews=0, rcts=1), EvidenceStatus.EMERGING),
        (_counts(total=30, recent=0, reviews=1, rcts=0), EvidenceStatus.MODERATE),
        (_counts(total=30, recent=0, reviews=0, rcts=3), EvidenceStatus.MODERATE),
        (_counts(total=30, recent=0, reviews=2, rcts=3), EvidenceStatus.STRONG),
    ],
)
def test_evidence_status_tiers(counts: EvidenceCounts, status: EvidenceStatus) -> None:
    assert scoring.evidence_status(counts) is status


def test_evidence_score_rewards_reviews_and_trials_over_volume() -> None:
    many_papers = scoring.evidence_score(_counts(total=3000, reviews=0, rcts=0))
    few_but_strong = scoring.evidence_score(_counts(total=25, reviews=3, rcts=5))
    assert many_papers == 25.0
    assert few_but_strong is not None and few_but_strong > 90


def test_a_viral_topic_without_evidence_is_gated_out_however_it_trends() -> None:
    viral = _candidate(
        "miracle gummies", overall=99.0, counts=_counts(total=2, reviews=0, rcts=0)
    )
    reason = scoring.gate_reason(viral, min_status=EvidenceStatus.EMERGING)
    assert reason is not None and reason.startswith("evidence limited")


def test_an_unchecked_topic_is_gated_with_its_own_reason() -> None:
    unchecked = _candidate("creatine", counts=None)
    reason = scoring.gate_reason(unchecked, min_status=EvidenceStatus.EMERGING)
    assert reason is not None and "could not be checked" in reason


def test_a_topic_with_enough_literature_passes_the_gate() -> None:
    passing = _candidate("creatine", counts=_counts())
    assert scoring.gate_reason(passing, min_status=EvidenceStatus.EMERGING) is None


# --- ranking and selection -------------------------------------------------


def test_ranking_is_by_trend_score_and_evidence_only_breaks_ties() -> None:
    weak_evidence_high_trend = _candidate("a", overall=80.0, counts=_counts(reviews=0, rcts=1))
    strong_evidence_low_trend = _candidate("b", overall=60.0, counts=_counts(reviews=3, rcts=6))
    tie_strong = _candidate("c", overall=70.0, counts=_counts(reviews=3, rcts=6))
    tie_weak = _candidate("d", overall=70.0, counts=_counts(reviews=0, rcts=1))
    unscored = _candidate("e", overall=None, counts=_counts())

    ranked = scoring.rank(
        [strong_evidence_low_trend, tie_weak, unscored, weak_evidence_high_trend, tie_strong]
    )
    assert [c.subject for c in ranked] == ["a", "c", "d", "b"]


def test_selection_takes_the_target_with_one_topic_per_subject() -> None:
    ranked = [
        _candidate("creatine", overall=90),
        _candidate("creatine", overall=85),
        _candidate("magnesium", overall=80),
        _candidate("sauna", overall=70),
    ]
    assert [c.subject for c in scoring.select(ranked, target=3)] == [
        "creatine",
        "magnesium",
        "sauna",
    ]


def test_selection_returns_fewer_when_fewer_qualify() -> None:
    assert len(scoring.select([_candidate("creatine")], target=5)) == 1


def test_a_narrower_subject_is_still_the_same_subject() -> None:
    """Measured: "blood pressure" and "blood pressure medication" both selected."""
    ranked = [
        _candidate("blood pressure", overall=90),
        _candidate("blood pressure medication", overall=85),
        _candidate("magnesium", overall=80),
    ]
    assert [c.subject for c in scoring.select(ranked, target=3)] == [
        "blood pressure",
        "magnesium",
    ]


def test_selection_keeps_categories_varied_when_asked() -> None:
    ranked = [
        _candidate("creatine", overall=90, category=Category.SUPPLEMENTS),
        _candidate("magnesium", overall=85, category=Category.SUPPLEMENTS),
        _candidate("ashwagandha", overall=80, category=Category.SUPPLEMENTS),
        _candidate("zone 2", overall=70, category=Category.FITNESS),
    ]
    chosen = scoring.select(ranked, target=3, max_per_category=2)
    assert [c.subject for c in chosen] == ["creatine", "magnesium", "zone 2"]
    unbounded = scoring.select(ranked, target=3)
    assert [c.subject for c in unbounded] == ["creatine", "magnesium", "ashwagandha"]


# --- web signal ------------------------------------------------------------


def test_web_signal_counts_relevance_and_authority() -> None:
    results = [
        WebResult(title="Creatine and sleep", url="https://www.nih.gov/a", snippet=""),
        WebResult(title="Creatine timing", url="https://health.harvard.edu/b", snippet=""),
        WebResult(title="Creatine gummies review", url="https://shop.example.com/c"),
        WebResult(title="Best protein bars", url="https://blog.example.com/d", snippet=""),
    ]
    signal = scoring.web_signal(
        results, source="w", subject="creatine", authoritative=[".gov", "health.harvard.edu"]
    )
    assert signal.results_found == 4
    assert signal.relevant_results == 3
    assert signal.authoritative_results == 2
    assert signal.distinct_domains == 3
    assert signal.top_results[0].url.endswith("nih.gov/a")


def test_a_discarded_candidate_is_stored_without_its_examples() -> None:
    candidate = _candidate("creatine", counts=_counts())
    candidate.web = WebSignal(
        source="w",
        results_found=2,
        relevant_results=2,
        distinct_domains=2,
        authoritative_results=1,
        top_results=[WebResult(title="Creatine", url="https://nih.gov/x")],
    )
    kept = candidate.signals_json()
    assert kept["web"]["top_results"]

    candidate.status = ResearchCandidateStatus.DISCARDED
    slim = candidate.signals_json()
    assert "top_results" not in slim["web"]
    assert slim["web"]["relevant_results"] == 2  # the counts stay
    assert slim["supporting_sources"] == []


# --- config ----------------------------------------------------------------


def test_trend_weights_that_do_not_sum_to_100_are_refused() -> None:
    with pytest.raises(ValueError, match="summing to 100"):
        Settings(research_weight_momentum=50)


def test_an_unknown_enabled_source_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown sources"):
        Settings(research_enabled_sources=["rss", "tiktok"])


def test_enabled_sources_accept_a_comma_separated_env_value() -> None:
    settings = Settings(research_enabled_sources="rss, gdelt")
    assert settings.research_enabled_sources == ["rss", "gdelt"]


def test_a_short_trigger_token_is_refused() -> None:
    with pytest.raises(ValueError, match="32 characters"):
        Settings(research_trigger_token="short")


def test_a_request_for_more_articles_than_the_max_is_clamped() -> None:
    config = ResearchConfig.resolve(Settings(), ResearchParams(target_article_count=10))
    assert config.target_article_count == 6
    small = ResearchConfig.resolve(Settings(), ResearchParams(target_article_count=2))
    assert (small.target_article_count, small.min_article_count) == (4, 4)


def test_a_trigger_can_narrow_sources_and_raise_bars_but_never_widen_or_lower() -> None:
    settings = Settings(
        research_enabled_sources=["rss", "gdelt"],
        research_min_data_coverage=0.6,
        research_max_requests_per_source=40,
    )
    narrowed = ResearchConfig.resolve(
        settings,
        ResearchParams(
            enabled_sources=["rss", "youtube"],
            min_data_coverage=0.3,
            min_trend_score=50,
            max_requests_per_source=100,
        ),
    )
    assert narrowed.enabled_sources == ["rss"]  # youtube was never enabled
    assert narrowed.min_data_coverage == 0.6  # settings are the floor
    assert narrowed.min_trend_score == 50
    assert narrowed.max_requests_per_source == 40  # the ceiling holds


def test_a_config_stored_before_the_split_still_loads() -> None:
    """A run queued before deploy carries the old six float weights."""
    stored = ResearchConfig.resolve(Settings(), ResearchParams()).model_dump(mode="json")
    stored["weights"] = {"trend_growth": 0.3, "scientific_evidence": 0.25}
    stored.pop("enabled_sources")
    loaded = ResearchConfig.model_validate(stored)
    assert sum(loaded.weights.values()) == 100
    assert loaded.enabled_sources  # the default


def test_history_retention_shorter_than_its_readers_is_refused() -> None:
    with pytest.raises(ValueError, match="research_history_retention_days"):
        Settings(research_history_retention_days=30)


def test_a_retention_shorter_than_the_baseline_is_refused() -> None:
    with pytest.raises(ValueError, match="retention"):
        Settings(discovery_observation_retention_days=30)


def test_source_lists_in_config_and_contracts_agree() -> None:
    from app import config
    from app.research.contracts import SOURCE_NAMES

    assert config._TREND_SOURCES == SOURCE_NAMES
