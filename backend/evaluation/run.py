"""``python -m evaluation.run`` — execute a suite, score it, report, compare.

Examples (from ``backend/`` with the venv active)::

    python -m evaluation.run                         # every quality + failure case
    python -m evaluation.run --suite retrieval       # stops after RANK; no synthesis
    python -m evaluation.run --suite citations
    python -m evaluation.run --test basic_001 --test adv_003
    python -m evaluation.run --limit 20
    python -m evaluation.run --save-baseline         # this run becomes the suite's baseline
    python -m evaluation.run --mode replay           # recordings only, no network (CI)
    python -m evaluation.run --rescore evaluation/reports/<run>   # re-score saved traces
    python -m evaluation.run --resume evaluation/reports/<run>    # finish an interrupted run
    python -m evaluation.run --list

Exit status: 0 when every critical threshold is met, 1 when one is missed (or,
with ``--fail-on-regression``, when a metric regressed past tolerance), 2 for a
usage or configuration error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.logging_setup import configure_logging
from app.pipeline.stages import StageName
from evaluation import aggregate as agg
from evaluation import baseline, html_report, report
from evaluation.config import BACKEND_ROOT, DEFAULT_CONFIG, EvalConfig, load_config
from evaluation.dataset import DatasetError, load_all, select
from evaluation.evaluators.llm_judge import Judge, judge_unavailable_reason
from evaluation.evaluators.source_validator import SourceValidator
from evaluation.harness.http import EvalHttp
from evaluation.harness.pipeline import EvalEnvironment, execute_case
from evaluation.harness.trace import Trace, TraceRecorder
from evaluation.schema import EvalCase
from evaluation.scoring import Evaluators, score_case

logger = logging.getLogger("evaluation")


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.run",
        description="Evaluate the EvidWell article pipeline and research agent.",
    )
    parser.add_argument(
        "--suite", default="all", help="suite name from config.yaml (default: all)"
    )
    parser.add_argument("--test", action="append", default=[], help="case id (repeatable)")
    parser.add_argument("--category", action="append", default=[], help="only this category")
    parser.add_argument("--limit", type=int, help="run at most N cases")
    parser.add_argument(
        "--mode", choices=["cached", "live", "replay", "frozen"], help="override run.mode"
    )
    parser.add_argument(
        "--save-baseline",
        "--baseline",
        dest="save_baseline",
        action="store_true",
        help="save this run's metrics as the suite's baseline",
    )
    parser.add_argument("--no-judge", action="store_true", help="skip the LLM judge")
    parser.add_argument(
        "--no-source-validation", action="store_true", help="skip registry checks of citations"
    )
    parser.add_argument("--rescore", type=Path, help="re-score the traces in a run directory")
    parser.add_argument(
        "--resume", type=Path, help="continue a run directory, skipping done cases"
    )
    parser.add_argument("--fail-on-regression", action="store_true")
    parser.add_argument("--list", action="store_true", help="list suites and cases, then exit")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--verbose", action="store_true", help="log at INFO")
    return parser.parse_args(argv)


def _git() -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=BACKEND_ROOT,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=BACKEND_ROOT,
            check=True,
        ).stdout.strip()
        return f"{commit}{'+dirty' if dirty else ''}"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _list(config: EvalConfig) -> int:
    datasets = load_all()
    print("Suites:")
    for name, suite in config.suites.items():
        cases = select(datasets, suite.datasets)
        extra = f", stops after {suite.stop_after}" if suite.stop_after else ""
        print(f"  {name:<12} {len(cases):>4} cases  datasets={suite.datasets}{extra}")
    print("\nDatasets:")
    for name, cases in datasets.items():
        categories = Counter(str(case.category) for case in cases)
        print(f"  {name:<14} {len(cases):>4}  {dict(categories)}")
    return 0


async def _evaluators(
    config: EvalConfig, env: EvalEnvironment, args: argparse.Namespace, cases: list[EvalCase]
) -> Evaluators:
    settings = env.settings
    judge = None
    reason = "disabled with --no-judge" if args.no_judge else None
    if reason is None:
        installed: set[str] | None = None
        if env.mode != "replay":
            try:
                response = await env.http.get(
                    f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=5
                )
                installed = {m["name"] for m in response.json().get("models", [])}
            except Exception as exc:
                reason = f"ollama unreachable for the judge: {exc}"
        if reason is None:
            reason = judge_unavailable_reason(
                config.judge,
                generator_models=[settings.ollama_synthesis_model],
                installed=installed,
            )
    if reason is None:
        judge = Judge(
            config.judge,
            env.cassette,
            base_url=settings.ollama_base_url,
            timeout=settings.ollama_timeout_seconds,
            mode=env.mode,
        )

    validator = None
    validator_reason = (
        "disabled with --no-source-validation" if args.no_source_validation else None
    )
    if validator_reason is None and not config.source_validation.enabled:
        validator_reason = "disabled in config.yaml"
    # Its own recorder: evaluator requests are cached like the system's, but
    # must never be counted as the system's tool calls.
    side_recorder = TraceRecorder.start("evaluators", "", env.mode, {})
    side_http = EvalHttp(env.http, env.cassette, env.mode, side_recorder)
    if validator_reason is None:
        validator = SourceValidator(
            side_http,
            settings,
            check_urls=config.source_validation.check_urls,  # type: ignore[arg-type]
        )

    verified: set[str] = set()
    gold = sorted({key for case in cases for key in case.relevant_ids})
    if gold:
        checker = validator or SourceValidator(side_http, settings, check_urls=False)  # type: ignore[arg-type]
        found = await checker.verify_identifiers(gold)
        verified = {key for key, exists in found.items() if exists}
        dropped = sorted(set(gold) - verified)
        if dropped:
            env.notes.append(
                f"{len(dropped)} gold identifier(s) could not be confirmed and were dropped: "
                f"{', '.join(dropped)}"
            )
    return Evaluators(
        config=config,
        settings=settings,
        judge=judge,
        judge_reason=None if judge else reason,
        validator=validator,
        validator_reason=validator_reason,
        recent_from=datetime.now(UTC).year - config.evaluation.recency_years,
        verified_gold=verified,
    )


async def _execute(case: EvalCase, env: EvalEnvironment, stop_after: StageName | None) -> Trace:
    if case.research_scenario:
        # Imported here so the article suites still run while the research
        # harness lags a change to app/research.
        from evaluation.harness.research import run_scenario

        return await run_scenario(case.id, case.research_scenario)
    return await execute_case(case, env, stop_after=stop_after)


def _progress(index: int, total: int, result: dict[str, Any], seconds: float) -> str:
    checks = [c for c in result.get("checks") or [] if c["passed"] is not None]
    ok = sum(1 for c in checks if c["passed"])
    verdict = (result.get("answer") or {}).get("verdict")
    state = (
        "SKIP " + str(result.get("skip_reason"))[:80]
        if result["status"] == "skipped"
        else (
            "PASS"
            if result["passed"]
            else "FAIL " + ",".join(result.get("failed_checks") or [])
        )
    )
    return (
        f"[{index:>3}/{total}] {result['id']:<22} {result['outcome']:<22} "
        f"{('verdict=' + verdict) if verdict else '':<20} {seconds:6.1f}s  "
        f"{ok}/{len(checks)}  {state}"
    )


def _capability_gaps(
    results: list[dict[str, Any]], cases: dict[str, EvalCase]
) -> list[dict[str, str]]:
    gaps = []
    followups = [
        r for r in results if r["category"] == "follow_up" and r["status"] == "executed"
    ]
    if followups:
        carried = sum(
            1
            for r in followups
            if (r.get("query_understanding") or {}).get("context_carryover")
        )
        gaps.append(
            {
                "gap": "conversation memory",
                "detail": (
                    "the pipeline receives one topic and no history (PipelineContext has no "
                    f"conversation field), so {len(followups)} follow-up case(s) ran on their "
                    f"last turn alone; {carried} stayed on the conversation's subject"
                ),
            }
        )
    ambiguous = [
        r for r in results if r["category"] == "ambiguous" and r["status"] == "executed"
    ]
    if ambiguous:
        outcomes = Counter(r["outcome"] for r in ambiguous)
        gaps.append(
            {
                "gap": "clarifying questions",
                "detail": (
                    "there is no path that asks the user anything; ambiguous queries end as "
                    + ", ".join(f"{n} x {o}" for o, n in outcomes.items())
                ),
            }
        )
    wanted: Counter[str] = Counter()
    for r in results:
        selection = (r.get("agent") or {}).get("tool_selection") or {}
        wanted.update(selection.get("ideal_tools_unavailable") or [])
    if wanted:
        gaps.append(
            {
                "gap": "tools the article pipeline does not have",
                "detail": ", ".join(
                    f"{tool} (wanted by {n} case(s))" for tool, n in wanted.items()
                )
                + "; web and news search exist only in the research agent, as trend signals",
            }
        )
    recency = [
        (r.get("answer") or {}).get("recent_cited_fraction")
        for r in results
        if cases[r["id"]].requires_recency and r.get("answer")
    ]
    recency = [v for v in recency if v is not None]
    if recency:
        gaps.append(
            {
                "gap": "freshness-aware retrieval",
                "detail": (
                    "'latest research' queries get no special handling (retrieval_min_year "
                    "is unset; recency is a +0.03 score bonus); on those cases "
                    f"{sum(recency) / len(recency) * 100:.0f}% of cited sources were recent"
                ),
            }
        )
    return gaps


#: Metrics compared between cases that ran with every provider and cases that
#: lost one mid-run.
_OUTAGE_METRICS = (
    "retrieval_recall_at_10",
    "rank_precision_at_5",
    "empty_retrieval_rate",
    "behavior_accuracy",
    "verdict_accuracy",
    "citation_correctness",
    "groundedness",
    "hallucination_rate",
)


def _provider_outages(
    results: list[dict[str, Any]], config: EvalConfig
) -> dict[str, Any] | None:
    """Cases run while a provider was down for reasons outside the eval's faults."""
    hit = [r for r in results if r.get("degraded_providers")]
    if not hit:
        return None
    clean = [
        r
        for r in results
        if r["status"] == "executed"
        and not r.get("degraded_providers")
        and not r["is_failure_case"]
    ]
    ks = config.evaluation.retrieval_ks
    with_outage = agg.aggregate(hit, ks)
    without = agg.aggregate(clean, ks)
    providers = Counter(p for r in hit for p in r["degraded_providers"])
    return {
        "providers": dict(providers),
        "cases": [r["id"] for r in hit],
        "comparison": {
            name: {
                "all_providers": without[name]["value"],
                "all_providers_n": without[name]["n"],
                "provider_down": with_outage[name]["value"],
                "provider_down_n": with_outage[name]["n"],
            }
            for name in _OUTAGE_METRICS
        },
    }


async def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.list:
        return _list(config)
    if args.suite not in config.suites:
        print(
            f"unknown suite {args.suite!r}; choose from {', '.join(config.suites)}",
            file=sys.stderr,
        )
        return 2
    suite = config.suites[args.suite]
    datasets = load_all()
    try:
        cases = select(
            datasets,
            suite.datasets,
            test_ids=[t for arg in args.test for t in arg.split(",") if t] or None,
            categories=args.category or None,
            limit=args.limit,
        )
    except DatasetError as exc:
        print(f"dataset error: {exc}", file=sys.stderr)
        return 2
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2

    mode = args.mode or config.run.mode
    stop_after = StageName.RANK if suite.stop_after == "rank" else None
    started = datetime.now(UTC)
    run_dir = (
        args.rescore
        or args.resume
        or (config.path(config.run.reports_dir) / f"{started:%Y%m%d-%H%M%S}_{args.suite}")
    )
    # Resolved, so a relative --rescore/--resume path still sits under backend/
    # when the report records where it was written.
    run_dir = run_dir.resolve()
    (run_dir / "traces").mkdir(parents=True, exist_ok=True)

    env = await EvalEnvironment.create(config, mode)
    try:
        ev = await _evaluators(config, env, args, cases)
        print(
            f"Evaluating {len(cases)} case(s) · suite {args.suite} · mode {mode} · "
            f"judge {ev.judge.model if ev.judge else 'off (' + str(ev.judge_reason) + ')'} · "
            f"source validation {'on' if ev.validator else 'off'}"
        )
        for note in env.notes:
            print(f"note: {note}")
        if env.unavailable:
            print(f"unavailable: {env.unavailable}")

        results: list[dict[str, Any]] = []
        with (run_dir / "cases.jsonl").open("w") as stream:
            for index, case in enumerate(cases, start=1):
                trace_path = run_dir / "traces" / f"{case.id}.json"
                clock = datetime.now(UTC)
                if (args.rescore or args.resume) and trace_path.exists():
                    trace = Trace.model_validate_json(trace_path.read_text())
                elif args.rescore:
                    continue
                else:
                    trace = await _execute(case, env, stop_after)
                    trace_path.write_text(trace.model_dump_json(indent=1))
                result = await score_case(case, trace, ev)
                results.append(result)
                stream.write(json.dumps(result, default=str) + "\n")
                stream.flush()
                elapsed = (datetime.now(UTC) - clock).total_seconds()
                print(_progress(index, len(cases), result, elapsed), flush=True)

        metrics = agg.aggregate(results, config.evaluation.retrieval_ks)
        if suite.groups:
            metrics = {n: m for n, m in metrics.items() if m["group"] in suite.groups}
        executed = [r for r in results if r["status"] == "executed"]
        counts = {
            "selected": len(cases),
            "executed": len(executed),
            "skipped": len(results) - len(executed),
            "passed": sum(1 for r in executed if r["passed"]),
            "failed": sum(1 for r in executed if not r["passed"]),
            "outcomes": dict(Counter(r["outcome"] for r in results)),
        }
        environment = env.describe()
        states = {
            name: report.status(name, m, config.thresholds) for name, m in metrics.items()
        }
        critical_failures = [name for name, state in states.items() if state == "FAIL"]

        regression: dict[str, Any] | None
        base_path = baseline.baseline_path(config.path(config.run.baselines_dir), args.suite)
        previous = baseline.load(base_path)
        if previous is None:
            regression = {
                "note": f"no baseline yet for suite '{args.suite}' ({base_path.name}); "
                "save one with --save-baseline"
            }
        else:
            changed = {
                key: (previous["environment"].get(key), environment.get(key))
                for key in (
                    "extraction_model",
                    "synthesis_model",
                    "embedding_model",
                    "enabled_providers",
                    "retrieval_top_k",
                )
                if previous["environment"].get(key) != environment.get(key)
            }
            regression = {
                "baseline_created_at": previous["created_at"],
                "baseline_git": previous.get("git"),
                "environment_changed": changed or None,
                "rows": baseline.compare(metrics, previous, config.regression.tolerance),
                "cases": baseline.case_changes(results, previous),
            }
        regressed = [
            row["metric"] for row in (regression or {}).get("rows") or [] if row["regression"]
        ]
        fail_on_regression = args.fail_on_regression or config.regression.fail_on_regression
        verdict = "FAIL" if critical_failures or (regressed and fail_on_regression) else "PASS"
        if verdict == "PASS" and any(state == "WARN" for state in states.values()):
            verdict = "PASS (with warnings)"
        summary_line = (
            f"{verdict}: {len(critical_failures)} critical metric(s) below threshold"
            + (f" ({', '.join(critical_failures)})" if critical_failures else "")
            + (f"; {len(regressed)} regression(s)" if regressed else "")
        )

        judge_info: dict[str, Any] = {
            "model": ev.judge.model if ev.judge else None,
            "reason": ev.judge_reason,
            "calls": ev.judge.calls if ev.judge else 0,
            "cached_calls": ev.judge.cached_calls if ev.judge else 0,
            "failures": ev.judge.failures if ev.judge else 0,
        }
        results_doc = {
            "suite": args.suite,
            "mode": mode,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "git": _git(),
            "environment": environment,
            "judge": judge_info,
            "source_validation": ev.validator_reason or "on",
            "counts": counts,
            "verdict": summary_line,
            "critical_failures": critical_failures,
            "metrics": metrics,
            "metric_status": states,
            "thresholds": {name: t.model_dump() for name, t in config.thresholds.items()},
            "performance": agg.performance_breakdown(results),
            "regression": regression,
            "capability_gaps": _capability_gaps(results, {c.id: c for c in cases}),
            "provider_outages": _provider_outages(results, config),
            "notes": env.notes,
            "skipped": [
                {"id": r["id"], "reason": r.get("skip_reason")}
                for r in results
                if r["status"] == "skipped"
            ],
            "report_path": str((run_dir / "report.md").relative_to(BACKEND_ROOT)),
            "cases": results,
        }
        (run_dir / "results.json").write_text(json.dumps(results_doc, indent=1, default=str))
        (run_dir / "report.md").write_text(report.markdown(results_doc, config.thresholds))
        html_report.write(run_dir, results_doc, config.thresholds)
        latest = config.path(config.run.reports_dir) / "LATEST"
        latest.write_text(str(run_dir.relative_to(BACKEND_ROOT)) + "\n")
        print(report.console(results_doc, config.thresholds))

        if args.save_baseline:
            baseline.save(base_path, results_doc)
            print(f"Baseline saved: {base_path.relative_to(BACKEND_ROOT)}")
        return 1 if verdict == "FAIL" else 0
    finally:
        await env.aclose()


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    configure_logging(logging.INFO if args.verbose else logging.WARNING)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\ninterrupted; resume with --resume <run dir>", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
