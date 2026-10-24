"""The Trend Search Agent's output: schema, provenance, and what it must not say.

Runs the fake world from ``test_research_graph.py`` and checks the frozen
result (``state.trend_summary``) and the read-time result
(``service.build_result``) against the contract:

* it validates as ``TrendSearchResult``, with aware timestamps and http(s) URLs;
* every selected topic carries supporting sources, and every one of them is a
  record a source actually returned;
* nothing in it is a scientific evidence score — a topic's
  ``scientific_assessment`` is ``not_assessed``, and the evidence check's
  outcome appears only as a status.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.domain.enums import ResearchCandidateStatus, ResearchRunMode, ResearchRunStatus
from app.domain.models import ResearchCandidate, ResearchRun
from app.research import service
from app.research.trends.contracts import TrendSearchResult
from tests.unitTest.test_research_graph import World, _run, _state


def _walk_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [*value, *(k for v in value.values() for k in _walk_keys(v))]
    if isinstance(value, list):
        return [k for v in value for k in _walk_keys(v)]
    return []


async def test_the_frozen_result_validates_and_is_aware_and_linked() -> None:
    state = await _run(evidence=False)
    assert state.trend_summary is not None
    result = TrendSearchResult.model_validate(state.trend_summary)

    assert result.status == "completed"
    assert result.collection_window.start < result.collection_window.end
    assert result.collection_window.end.tzinfo is not None
    assert result.counts["items_collected"] > 0
    assert "rss" in result.sources.successful
    assert "youtube" in result.sources.skipped  # unavailable: no key
    for topic in result.topics:
        assert 0 <= topic.trend_score <= 100
        assert 0 <= topic.data_coverage <= 1
        assert topic.summary and topic.selection_reason
        assert topic.supporting_sources
        for source in topic.supporting_sources:
            assert source.url.startswith(("http://", "https://"))
            assert source.published_at is None or source.published_at.tzinfo is not None


async def test_no_supporting_source_is_invented() -> None:
    state = await _run(evidence=False)
    collected = {(i.url, i.title) for i in state.items}
    assert state.trend_summary is not None
    for topic in state.trend_summary["topics"]:
        for source in topic["supporting_sources"]:
            assert (source["url"], source["title"]) in collected


async def test_the_output_carries_no_scientific_evidence_score() -> None:
    state = await _run()  # with the evidence check, so evidence exists in the run
    assert state.trend_summary is not None
    keys = set(_walk_keys(state.trend_summary))
    assert not {k for k in keys if "evidence" in k and k != "evidence_gate"}
    assert all(
        t["scientific_assessment"] == "not_assessed" for t in state.trend_summary["topics"]
    )
    schema = json.dumps(TrendSearchResult.model_json_schema())
    assert "evidence_score" not in schema and "scientific_evidence" not in schema


async def test_the_read_time_result_overlays_the_evidence_gate_as_a_status() -> None:
    state = await _run(state=_state(research_evidence_pool_size=8))
    run = ResearchRun(
        id=state.run_id,
        label=state.label,
        mode=ResearchRunMode.MANUAL,
        status=ResearchRunStatus.COMPLETED,
        stage="completed",
        params={},
        config=state.config.model_dump(mode="json"),
        provider_status={},
        stage_log=[],
        notes=[],
        error=None,
        created_at=datetime(2026, 10, 12, tzinfo=UTC),
        started_at=datetime(2026, 10, 12, tzinfo=UTC),
        finished_at=datetime(2026, 10, 12, 0, 10, tzinfo=UTC),
        summary=service.run_summary(state),
    )
    rows = [
        ResearchCandidate(**{k: v for k, v in service.candidate_row(c, state.run_id).items()})
        for c in state.candidates
    ]
    result = service.build_result(run, rows)

    by_subject = {t.subject: t for t in result.topics}
    assert by_subject["creatine"].status == "selected"
    assert by_subject["creatine"].downstream.evidence_gate == "passed"
    viral = next((t for t in result.topics if t.subject == "miracle mushroom"), None)
    if viral is not None:  # shortlisted by trend, refused by evidence
        assert viral.status == "discarded"
        assert viral.downstream.evidence_gate == "failed"
        assert viral.downstream.reason and viral.downstream.reason.startswith("evidence")
    assert all(
        t.trend_shortlisted or t.status in ("selected", "promoted") for t in result.topics
    )
    assert result.status == "completed"


async def test_a_failed_evidence_check_reports_partial_with_the_trends_kept() -> None:
    state = await _run(World(pubmed_down=True, epmc_down=True))
    run = ResearchRun(
        id=state.run_id,
        label=state.label,
        mode=ResearchRunMode.MANUAL,
        status=ResearchRunStatus.FAILED,
        stage="failed",
        params={},
        config=state.config.model_dump(mode="json"),
        provider_status={},
        stage_log=[],
        notes=[],
        error={"message": state.failed},
        created_at=datetime(2026, 10, 12, tzinfo=UTC),
        started_at=None,
        finished_at=None,
        summary=service.run_summary(state),
    )
    result = service.build_result(run, [])
    assert result.status == "partial"
    assert result.topics
    assert any("scientific_evidence_unavailable" in e for e in result.errors)


def test_a_run_without_a_trend_phase_reports_failed_and_empty() -> None:
    run = ResearchRun(
        id="00000000-0000-0000-0000-00000000000f",
        label="research_x",
        mode=ResearchRunMode.WEEKLY,
        status=ResearchRunStatus.FAILED,
        stage="failed",
        params={},
        config={"trend_window_days": 7},
        provider_status={},
        stage_log=[],
        notes=[],
        error={"message": "no_trend_data: every source failed"},
        created_at=datetime(2026, 10, 12, tzinfo=UTC),
        started_at=None,
        finished_at=None,
        summary=None,
    )
    result = service.build_result(run, [])
    assert result.status == "failed"
    assert result.topics == []
    assert result.errors == ["no_trend_data: every source failed"]


async def test_a_discarded_candidate_is_never_presented_as_a_proposal() -> None:
    state = await _run()
    assert state.trend_summary is not None
    statuses = {c.topic_id: c.status for c in state.candidates}
    for topic in state.trend_summary["topics"]:
        if topic["trend_shortlisted"]:
            assert statuses[topic["candidate_id"]] is not ResearchCandidateStatus.CANDIDATE or (
                topic["status"] == "candidate"
            )
