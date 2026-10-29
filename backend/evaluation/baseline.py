"""Baselines: one saved result per suite, and the comparison against it.

A baseline is a run's metrics plus enough context to know whether comparing
against it is fair — the models, the providers, the dataset size, the mode.
The comparison prints that context beside the deltas, because a "regression"
between two runs on different synthesis models is a model change, not a code
change, and should read as one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Metrics where a smaller number is the better one.
LOWER_IS_BETTER = frozenset(
    {
        "agent_loop_rate",
        "unnecessary_tool_calls_per_case",
        "empty_retrieval_rate",
        "duplicate_rate",
        "irrelevant_in_top_k_per_case",
        "unsupported_citation_rate",
        "retracted_citation_rate",
        "hallucination_rate",
        "draft_hallucination_rate",
        "misattributed_number_rate",
        "invented_number_rate",
        "forbidden_claim_rate",
        "infinite_loop_rate",
        "hallucination_after_tool_failure_rate",
        "latency_mean_s",
        "latency_median_s",
        "latency_p95_s",
        "latency_p99_s",
    }
)
#: Relative slowdown in a latency metric that counts as a regression.
LATENCY_TOLERANCE = 0.25
#: Reported but not judged better or worse when they move.
NEUTRAL = frozenset({"tool_call_count"})


def baseline_path(directory: Path, suite: str) -> Path:
    return directory / f"{suite}.json"


def save(path: Path, results: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "suite": results["suite"],
        "created_at": results["finished_at"],
        "git": results["git"],
        "environment": results["environment"],
        "cases": results["counts"],
        "metrics": {
            name: {"value": metric["value"], "n": metric["n"]}
            for name, metric in results["metrics"].items()
        },
        "case_status": {
            row["id"]: {"passed": row["passed"], "outcome": row["outcome"]}
            for row in results["cases"]
        },
    }
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True))


def load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def other_cases(results: list[dict[str, Any]], baseline: dict[str, Any]) -> str | None:
    """Why this run cannot be compared with the baseline, or None when it can.

    Metrics over a different set of cases move with the case mix, not the code: 8
    hand-picked hard cases read as 9 regressions against all 107 (2026-10-28). A
    baseline saved before ``case_status`` existed cannot say, and is compared as before.
    """
    saved = set(baseline.get("case_status") or ())
    ran = {row["id"] for row in results}
    if not saved or saved == ran:
        return None
    return (
        f"baseline covers different cases ({len(saved)} saved, {len(ran)} run, "
        f"{len(saved & ran)} in both); no regression comparison"
    )


def compare(
    current: dict[str, dict[str, Any]], baseline: dict[str, Any], tolerance: float
) -> list[dict[str, Any]]:
    rows = []
    for name, metric in current.items():
        before = (baseline.get("metrics") or {}).get(name)
        if before is None or before.get("value") is None or metric["value"] is None:
            continue
        delta = metric["value"] - before["value"]
        if name in NEUTRAL:
            direction = "neutral"
            regressed = False
        else:
            lower = name in LOWER_IS_BETTER
            direction = "lower" if lower else "higher"
            worse = delta > 0 if lower else delta < 0
            if name.startswith("latency"):
                # Wall clock is noisy run to run; only a 25% slowdown counts.
                regressed = worse and abs(delta) > LATENCY_TOLERANCE * abs(before["value"])
            else:
                regressed = worse and abs(delta) > tolerance
        rows.append(
            {
                "metric": name,
                "current": metric["value"],
                "baseline": before["value"],
                "delta": delta,
                "better": direction,
                "regression": regressed,
                "n": metric["n"],
                "baseline_n": before.get("n"),
            }
        )
    return rows


def case_changes(
    results: list[dict[str, Any]], baseline: dict[str, Any]
) -> dict[str, list[str]]:
    """Cases that flipped between pass and fail since the baseline."""
    before = baseline.get("case_status") or {}
    newly_failing, newly_passing = [], []
    for row in results:
        previous = before.get(row["id"])
        if previous is None or row["passed"] is None or previous["passed"] is None:
            continue
        if previous["passed"] and not row["passed"]:
            newly_failing.append(row["id"])
        elif not previous["passed"] and row["passed"]:
            newly_passing.append(row["id"])
    return {"newly_failing": newly_failing, "newly_passing": newly_passing}
