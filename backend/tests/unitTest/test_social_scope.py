"""The new discovery modules stay beside the trend search and the MeSH scan, not inside them.

* The trend search and the MeSH scan never import social listening, product
  discovery or the relations layer, so nothing there can move their scores.
* The relations layer (``intel/related.py``) only reads: no insert, update,
  delete, add, flush or commit.
* The trend search's output contract and scoring are pinned: a field added to
  ``TrendSearchResult`` or a weight changed has to change this test, on purpose.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.config import Settings
from app.research.trends.contracts import TREND_SCORING_VERSION, ResultTopic, TrendSearchResult
from app.social.contracts import TopicMetrics

APP = Path(__file__).resolve().parents[2] / "app"
NEW_MODULES = ("app.social", "app.products", "app.intel")


def _imports(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def test_the_trend_search_and_the_mesh_scan_never_import_the_new_modules() -> None:
    offenders = [
        f"{path.relative_to(APP)} imports {name}"
        for package in ("research", "discovery")
        for path in (APP / package).rglob("*.py")
        for name in _imports(path)
        if name.startswith(NEW_MODULES)
    ]
    assert offenders == []


def test_the_relations_layer_writes_nothing() -> None:
    tree = ast.parse((APP / "intel" / "related.py").read_text())
    writes = {"insert", "update", "delete", "add", "flush", "commit", "execute_write"}
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert called & writes == set()


def test_the_trend_search_output_contract_is_unchanged() -> None:
    assert sorted(TrendSearchResult.model_fields) == [
        "collection_window",
        "completed_at",
        "counts",
        "errors",
        "label",
        "notes",
        "run_id",
        "scoring_version",
        "sources",
        "started_at",
        "status",
        "topics",
    ]
    assert sorted(ResultTopic.model_fields) == [
        "candidate_id",
        "category",
        "confidence",
        "data_coverage",
        "downstream",
        "key_questions",
        "limitations",
        "outcome",
        "rank",
        "scientific_assessment",
        "score_breakdown",
        "selection_reason",
        "status",
        "subject",
        "summary",
        "supporting_sources",
        "topic",
        "topic_id",
        "trend_score",
        "trend_shortlisted",
        "trend_status",
    ]


def test_the_trend_scoring_model_is_unchanged() -> None:
    settings = Settings()
    assert TREND_SCORING_VERSION == "trend-v1"
    assert [
        settings.research_weight_momentum,
        settings.research_weight_coverage,
        settings.research_weight_audience,
        settings.research_weight_freshness,
        settings.research_weight_editorial_fit,
    ] == [30, 25, 20, 15, 10]


def test_social_metrics_carry_no_evidence_and_no_single_score() -> None:
    fields = set(TopicMetrics.model_fields)
    assert not any("evidence" in f or f in {"score", "overall"} for f in fields)
