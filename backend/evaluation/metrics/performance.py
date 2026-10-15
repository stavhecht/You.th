"""Latency, call counts, tokens and cost.

Two latencies, never mixed. **Wall clock** is what this run took, and in
``cached`` mode it is mostly replay — fast and meaningless as a performance
number. **Live latency** is what each external call cost when it was actually
made (recorded with the response and replayed with it), which is the honest
per-call figure whichever mode the run used. The report gives both and says
how much of the run was replayed.

Cost comes from ``llm/pricing.py`` and keeps its rule: an unpriced model is
``None``, never ``0``. Local Ollama models cost nothing to run but most are not
in the price table, so a local run typically reports cost as unpriced.
"""

from __future__ import annotations

import math
from collections import defaultdict
from decimal import Decimal
from typing import Any

from app.llm.base import TokenUsage
from app.llm.pricing import total_cost_usd


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; ``None`` for no data."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def summary(values: list[float]) -> dict[str, Any]:
    """Mean, median, p95 and p99. p95/p99 are only reported with enough samples
    to mean anything (20 and 100), and say so rather than repeating the max."""
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": sum(values) / len(values),
        "median": percentile(values, 50),
        "p95": percentile(values, 95) if len(values) >= 20 else None,
        "p99": percentile(values, 99) if len(values) >= 100 else None,
        "max": max(values),
    }


def case_performance(trace: dict[str, Any]) -> dict[str, Any]:
    calls = trace.get("tool_calls") or []
    stages = trace.get("stages") or []
    by_tool: dict[str, list[float]] = defaultdict(list)
    for call in calls:
        if call.get("live_latency_ms") is not None and call["status"] != "fault":
            by_tool[call["tool"]].append(float(call["live_latency_ms"]))
    by_stage: dict[str, float] = defaultdict(float)
    for span in stages:
        by_stage[span["stage"]] += span.get("duration_ms") or 0.0

    usage: list[tuple[str, TokenUsage]] = []
    tokens_in = tokens_out = 0
    for call in calls:
        if call.get("usage") and call["tool"].startswith("llm_"):
            spent = TokenUsage(**call["usage"])
            tokens_in += spent.input_tokens
            tokens_out += spent.output_tokens
            if call.get("model"):
                usage.append((call["model"], spent))
    cost: Decimal | None = total_cost_usd(usage) if usage else Decimal(0)

    live_calls = [c for c in calls if not c.get("cached") and c["status"] != "fault"]
    search_tools = {"pubmed", "europe_pmc", "openalex", "semantic_scholar"}
    return {
        "wall_clock_s": (trace.get("duration_ms") or 0) / 1000,
        "stage_s": {stage: ms / 1000 for stage, ms in by_stage.items()},
        "live_latency_ms_by_tool": dict(by_tool),
        "search_live_ms": sum(sum(by_tool[t]) for t in search_tools if t in by_tool),
        "llm_live_ms": sum(by_tool.get("llm_extraction", []))
        + sum(by_tool.get("llm_appraisal", []))
        + sum(by_tool.get("llm_synthesis", []))
        + sum(by_tool.get("llm_reader_edit", [])),
        "retrieval_s": (by_stage.get("retrieve", 0) + by_stage.get("rank", 0)) / 1000,
        "calls": len(calls),
        "live_calls": len(live_calls),
        "replayed_calls": sum(1 for c in calls if c.get("cached")),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "models": sorted({model for model, _ in usage}),
        "cost_usd": float(cost) if cost is not None else None,
        "cost_priced": cost is not None,
    }
