"""The evaluation framework's own arithmetic and plumbing.

An evaluator that computes recall wrong, or reads a refusal as a pass, reports
a quality the system does not have — and nothing downstream would notice. These
pin the metric definitions against hand-worked examples, the datasets against
their schema, and the harness seams against the production code they sit on.
No network, no model, no database.
"""

from __future__ import annotations

import httpx
import pytest

from app.domain.enums import Verdict
from evaluation import baseline
from evaluation.config import Threshold, load_config
from evaluation.dataset import load_all, select
from evaluation.evaluators.credibility import scholarly_tier, web_tier
from evaluation.evaluators.text import (
    checkable_numbers,
    number_supported,
    split_sentences,
    statements,
    strip_markers,
)
from evaluation.harness.faults import FaultInjector, fault_response
from evaluation.harness.http import classify, result_count, sanitize
from evaluation.harness.pipeline import NOT_EVALUATED, check_stage_parity, classify_outcome
from evaluation.metrics import agent, retrieval
from evaluation.metrics.answers import behavior_ok, verdict_ok
from evaluation.metrics.performance import percentile, summary
from evaluation.report import status
from evaluation.schema import Behavior, EvalCase, FaultSpec, Outcome

# --- retrieval metrics ---------------------------------------------------------


def test_recall_counts_gold_found_by_either_identifier() -> None:
    ranked = [{"pmid:1", "doi:10.1/a"}, {"pmid:2"}, {"doi:10.1/c"}]
    gold = {"doi:10.1/a", "pmid:2", "pmid:9"}
    assert retrieval.recall_at_k(ranked, gold, 1) == pytest.approx(1 / 3)
    assert retrieval.recall_at_k(ranked, gold, 3) == pytest.approx(2 / 3)
    assert retrieval.recall_at_k(ranked, set(), 3) is None


def test_precision_is_over_the_list_actually_returned() -> None:
    assert retrieval.precision_at_k([2, 0, 2], 5) == pytest.approx(2 / 3)
    assert retrieval.precision_at_k([2, 1, 0, 0, 2, 2], 5) == pytest.approx(2 / 5)
    assert retrieval.precision_at_k([], 5) is None


def test_capped_recall_divides_by_what_could_fit() -> None:
    # Fifty relevant papers in the pool: three in the top five is 3/5, not 3/50.
    assert retrieval.recall_cap_at_k([2, 2, 0, 2, 0], 50, 5) == pytest.approx(3 / 5)
    assert retrieval.recall_cap_at_k([2, 0], 1, 5) == 1.0
    assert retrieval.recall_cap_at_k([0, 0], 0, 5) is None


def test_reciprocal_rank_and_ndcg() -> None:
    assert retrieval.reciprocal_rank([0, 1, 2]) == pytest.approx(1 / 3)
    assert retrieval.reciprocal_rank([0, 0]) == 0.0
    assert retrieval.ndcg_at_k([2, 2], [2, 2, 0], 10) == pytest.approx(1.0)
    worse = retrieval.ndcg_at_k([0, 2], [2, 0], 10)
    assert worse is not None and worse < 1.0
    assert retrieval.ndcg_at_k([0, 0], [0, 0], 10) is None


def test_relevance_rule_needs_the_subject_and_an_outcome() -> None:
    paper = {"title": "Creatine and strength", "abstract": "Resistance training trial."}
    assert retrieval.relevance_grade(paper, ["creatine"], ["strength"]) == 2
    assert retrieval.relevance_grade(paper, ["creatine"], ["sleep"]) == 1
    assert retrieval.relevance_grade(paper, ["caffeine"], ["strength"]) == 0
    # Word-prefix match, so a stem covers its forms ("depress" -> "depression"),
    # but never mid-word: "inc" is not in "zinc".
    assert retrieval.mentions("major depression", ["depress"])
    assert not retrieval.mentions("zinc lozenges", ["inc"])


def test_duplicates_by_identifier_or_near_identical_title() -> None:
    papers = [
        {"pmid": "1", "title": "Creatine and strength in adults"},
        {"doi": "10.1/x", "pmid": "1", "title": "Something else"},
        {"title": "Creatine and strength in adults."},
    ]
    assert retrieval.duplicate_count(papers) == 2


def test_no_answer_topics_leave_precision_undefined() -> None:
    trace = {
        "papers": {"a": {"source_id": "a", "title": "Fish oil", "abstract": "Omega-3."}},
        "rounds": [
            {
                "round": 0,
                "candidates": {"c": ["a"]},
                "ranked": {"c": [{"source_id": "a", "handle": "S1", "score": 1.0}]},
                "thin_claims": [],
            }
        ],
    }
    result = retrieval.evaluate(
        trace, subject=["zorbanol"], outcome=["focus"], gold=set(), ks=[5], recent_from=2021
    )
    assert result["precision@5"] is None
    assert result["mrr"] is None
    assert result["irrelevant_in_top"] == 1


# --- text: statements, citations, numbers -------------------------------------


def test_a_leading_marker_closes_the_previous_sentence_unless_it_is_the_subject() -> None:
    text = (
        "Cortisol fell. [S1] Another trial disagreed [S3]. "
        "Sleep improved. [S6] also found gains."
    )
    assert split_sentences(text) == [
        "Cortisol fell. [S1]",
        "Another trial disagreed [S3].",
        "Sleep improved.",
        "[S6] also found gains.",
    ]


def test_abbreviations_do_not_split_sentences() -> None:
    assert len(split_sentences("Doses (e.g. 5 g) worked, per Smith et al. in 2020.")) == 1


def test_stripping_markers_leaves_clean_punctuation() -> None:
    assert strip_markers("Cortisol fell [S1, S2]. It rose [S3][S4] , then fell.") == (
        "Cortisol fell. It rose, then fell."
    )


def test_counts_of_sources_are_not_checked_as_quoted_numbers() -> None:
    assert checkable_numbers("Three trials of 4,812 adults found 87% improvement") == [
        "4,812",
        "87",
    ]
    assert checkable_numbers("Across 3 trials and 2 meta-analyses") == []


def test_numbers_match_exactly_or_as_a_rounding() -> None:
    source = [27.9, 0.54, 4812.0]
    assert number_supported("28", source)
    assert number_supported("27.9", source)
    assert number_supported("0.5", source)
    assert number_supported("4,812", source)
    assert not number_supported("87", source)


def test_statements_attribute_handles_and_mark_evidential_blocks() -> None:
    draft = {
        "body": {
            "beat_1_claim": "It claims to reduce stress.",
            "beat_2_evidence": "One trial found lower cortisol [S1]. A second found nothing.",
            "sections": [{"heading": "Dose", "body": "Doses of 600 mg were used [S2]."}],
            "beat_3_bottom_line": "The evidence is early.",
        }
    }
    found = statements(draft)
    by_text = {s.text: s for s in found}
    assert by_text["One trial found lower cortisol."].handles == ["S1"]
    assert by_text["One trial found lower cortisol."].evidential
    assert by_text["A second found nothing."].handles == []
    assert by_text["Doses of 600 mg were used."].numbers == ["600"]
    assert not by_text["It claims to reduce stress."].evidential


# --- credibility ---------------------------------------------------------------


def test_source_tiers() -> None:
    assert scholarly_tier("meta_analysis") == "systematic_review_or_meta_analysis"
    assert scholarly_tier("rct", doi="10.1101/2024.01.01") == "preprint"
    assert (
        scholarly_tier("narrative_review", title="ISSN position stand: creatine")
        == "clinical_guideline"
    )
    assert web_tier("https://www.nih.gov/health") == "government_or_institutional"
    assert web_tier("https://www.bbc.com/news/health") == "news"
    assert web_tier("https://someone.substack.com/p/x") == "blog"


# --- behaviour and verdicts ------------------------------------------------------


def _case(**fields: object) -> EvalCase:
    return EvalCase.model_validate(
        {"id": "t_1", "category": "simple_factual", "query": "q", **fields}
    )


def test_default_behaviour_ranges() -> None:
    refuse = _case(expected_behavior="refuse_or_clarify")
    assert behavior_ok(refuse, "refused_unanchored")
    assert not behavior_ok(refuse, "answered")
    uncertain = _case(expected_behavior="acknowledge_uncertainty")
    assert verdict_ok(uncertain, "mixed") is True
    assert verdict_ok(uncertain, "supported") is False
    assert verdict_ok(_case(), "supported") is None  # no verdict constraint stated


def test_a_case_needs_an_input_and_forbids_unknown_fields() -> None:
    with pytest.raises(ValueError):
        EvalCase.model_validate({"id": "x", "category": "simple_factual"})
    with pytest.raises(ValueError):
        _case(expected_tool=["pubmed"])  # misspelt field


def test_injected_documents_must_use_the_reserved_prefix() -> None:
    with pytest.raises(ValueError):
        _case(inject_documents=[{"title": "t", "abstract": "a", "doi": "10.1000/real"}])


# --- datasets --------------------------------------------------------------------


def test_every_dataset_loads_and_every_suite_resolves() -> None:
    datasets = load_all()
    config = load_config()
    for name, suite in config.suites.items():
        assert select(datasets, suite.datasets), name
    every = [case for cases in datasets.values() for case in cases]
    quality = {c.id for c in every if not c.is_failure_case}
    assert len(quality) >= 100


def test_the_dataset_covers_every_requested_category() -> None:
    categories = {c.category.value for cases in load_all().values() for c in cases}
    assert {
        "simple_factual",
        "multi_part",
        "latest_research",
        "academic_research",
        "general_web",
        "ambiguous",
        "follow_up",
        "no_answer",
        "conflicting_evidence",
        "adversarial",
        "failure",
    } <= categories


def test_the_wellness_suites_cover_every_trend_in_the_list() -> None:
    datasets = load_all()
    full = datasets["wellness_trends"]
    covered = {tag for case in full for tag in case.tags if tag.startswith("wt_")}
    assert covered == {f"wt_{n:03d}" for n in range(1, 101)}
    # Each stage is a subset of the next, and the pilot touches all five trend categories.
    pilot, core = ({c.id for c in datasets[n]} for n in ("wellness_pilot", "wellness_core"))
    assert pilot <= core <= {c.id for c in full}
    assert 15 <= len(pilot) <= 20 and 40 <= len(core) <= 60
    assert len({c.subcategory for c in datasets["wellness_pilot"]}) == 5


# --- harness seams -----------------------------------------------------------------


def test_requests_are_classified_into_tools() -> None:
    assert classify("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", {}) == (
        "pubmed",
        "esearch",
    )
    epmc = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    assert classify(epmc, {"resultType": "core"}) == ("europe_pmc", "search")
    assert classify(epmc, {"resultType": "lite"}) == ("europe_pmc_lookup", "lookup")
    assert classify(f"{epmc[:-7]}/PMC123/fullTextXML", None) == (
        "europe_pmc_fulltext",
        "fulltext",
    )
    assert classify("https://api.openalex.org/works", {}) == ("openalex", "search")


def test_credentials_never_reach_a_trace_or_a_key() -> None:
    assert sanitize({"term": "x", "api_key": "secret", "mailto": "me@x"}) == {"term": "x"}


def test_faults_respect_target_operation_and_count() -> None:
    injector = FaultInjector(
        [FaultSpec(target="pubmed", kind="status", status=503, operation="efetch", times=1)]
    )
    assert injector.match("pubmed", "esearch") is None
    assert injector.match("pubmed", "efetch") is not None
    assert injector.match("pubmed", "efetch") is None  # fired its one time
    everything = FaultInjector([FaultSpec(target="all_search", kind="timeout")])
    assert everything.match("openalex", "search") is not None
    assert everything.match("europe_pmc_lookup", "lookup") is None


def test_an_empty_fault_is_a_well_formed_empty_answer() -> None:
    spec = FaultSpec(target="pubmed", kind="empty")
    response = fault_response(
        spec, "https://eutils.ncbi.nlm.nih.gov/x/esearch.fcgi", {}, "pubmed", "esearch"
    )
    assert response.status_code == 200
    assert result_count("pubmed", "esearch", response) == 0
    with pytest.raises(httpx.ReadTimeout):
        fault_response(
            FaultSpec(target="pubmed", kind="timeout"), "https://x", {}, "pubmed", "esearch"
        )


def test_the_eval_runs_the_production_stage_order() -> None:
    from app.config import get_settings

    assert check_stage_parity(get_settings()) == []
    assert {str(name) for name in NOT_EVALUATED} == {"illustrate", "persist"}


def test_outcomes_are_read_from_the_error_not_from_a_message() -> None:
    from app.pipeline.stages import StageError, StageName
    from app.retrieval.query_builder import UnanchoredQuery

    unanchored = StageError(StageName.RETRIEVE, "no subject", retryable=False)
    unanchored.__cause__ = UnanchoredQuery("c", "p", [])
    assert (
        classify_outcome(None, unanchored, retrieval_only=False) is Outcome.REFUSED_UNANCHORED
    )
    throttled = StageError(StageName.RETRIEVE, "429", retryable=True)
    assert classify_outcome(None, throttled, retrieval_only=False) is Outcome.FAILED_RETRYABLE
    assert (
        classify_outcome(None, KeyError("x"), retrieval_only=False) is Outcome.FAILED_UNEXPECTED
    )


def test_a_retry_after_a_throttle_is_not_a_redundant_call() -> None:
    trace = {
        "tool_calls": [
            {"tool": "pubmed", "operation": "esearch", "request": {"t": 1}, "status": "fault",
             "fault": "rate_limit", "http_status": 429},
            {"tool": "pubmed", "operation": "esearch", "request": {"t": 1}, "status": "ok",
             "http_status": 200},
            {"tool": "pubmed", "operation": "esearch", "request": {"t": 1}, "status": "ok",
             "http_status": 200},
        ]
    }  # fmt: skip
    stats = agent.call_statistics(trace)
    assert stats["retries"] == 1
    assert stats["redundant_calls"] == 1
    assert stats["max_attempts_per_request"] == 3


# --- reporting and regression --------------------------------------------------------


def test_status_bands() -> None:
    critical = {"m": Threshold(direction="min", value=0.9, critical=True)}
    assert status("m", {"value": 0.97, "n": 3}, critical) == "PASS"
    assert status("m", {"value": 0.92, "n": 3}, critical) == "WARN"
    assert status("m", {"value": 0.80, "n": 3}, critical) == "FAIL"
    assert status("m", {"value": None, "n": 0}, critical) == "N/A"
    tight = {"m": Threshold(direction="min", value=0.98, critical=True)}
    assert status("m", {"value": 1.0, "n": 3}, tight) == "PASS"  # perfect is never "close"
    assert status("other", {"value": 0.5, "n": 3}, critical) == "INFO"


def test_regressions_respect_direction_and_tolerance() -> None:
    current = {
        "citation_precision": {"value": 0.85, "n": 10},
        "hallucination_rate": {"value": 0.10, "n": 10},
        "verdict_accuracy": {"value": 0.79, "n": 10},
    }
    before = {
        "metrics": {
            "citation_precision": {"value": 0.90, "n": 10},
            "hallucination_rate": {"value": 0.05, "n": 10},
            "verdict_accuracy": {"value": 0.80, "n": 10},
        }
    }
    rows = {row["metric"]: row for row in baseline.compare(current, before, tolerance=0.02)}
    assert rows["citation_precision"]["regression"]
    assert rows["hallucination_rate"]["regression"]  # lower is better, and it rose
    assert not rows["verdict_accuracy"]["regression"]  # within tolerance


def test_percentiles_need_enough_samples() -> None:
    assert percentile([1, 2, 3, 4], 50) == 2
    stats = summary([float(x) for x in range(10)])
    assert stats["p95"] is None and stats["p99"] is None
    assert summary([float(x) for x in range(1, 101)])["p95"] == 95.0


def test_behaviour_enum_covers_every_default() -> None:
    for behavior in Behavior:
        assert _case(expected_behavior=behavior.value).outcomes()
    assert Verdict.NO_EVIDENCE in (
        _case(expected_behavior="reject_false_premise").verdicts() or set()
    )


def test_a_number_late_in_a_full_text_excerpt_counts_as_in_the_source() -> None:
    from evaluation.scoring import _source_text

    excerpt = {"section": "Results", "text": "word " * 250 + "a 73% reduction"}
    assert "73%" in _source_text({"abstract": "short"}, [excerpt], 10_000)
