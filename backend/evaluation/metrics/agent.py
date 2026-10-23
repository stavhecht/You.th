"""Did the pipeline call the right tools, take the right branches, and stop?

The article pipeline is not a free-roaming tool-calling agent. Which tools run
is decided by code: every enabled provider is searched for every anchored
claim, and the graph has exactly three branch points — refine (RANK ->
RETRIEVE), the coverage re-prompt inside SYNTHESIZE, and revise (VALIDATE ->
SYNTHESIZE) — plus two hard stops (an unanchored query, every search failing)
and one model-free branch (zero sources -> the no-evidence template). So "tool
selection" here has two halves, both checked:

* **Tool selection**: were the expected tools called, and were forbidden ones
  not? Expected tools default from the case's behaviour and the enabled
  providers; a provider quietly missing from the run (the compose ``.env``
  trap that dropped OpenAlex) shows up as a missing tool call.
* **Decisions**: at each branch point the design says what *must* happen. Each
  rule below is checked only where it applies, and every one is taken from the
  system's own documentation, not invented here.

Repeated calls are split by cause: an identical request after a 429 or a
throttle body is a *retry* (expected, bounded); after a success it is
*redundant*. Only the second counts as an unnecessary call.
"""

from __future__ import annotations

import itertools
from collections import Counter
from typing import Any

from app.pipeline.graph import MAX_REFINE_ROUNDS, MAX_REVISE_ROUNDS, REVISABLE_FAILURES
from app.pipeline.steps.synthesize import (
    MAX_HANDLES_PER_CITATION,
    MIN_CITED_FRACTION,
    SECTIONS_EXPECTED_FROM,
)
from evaluation.harness.trace import SEARCH_TOOLS
from evaluation.schema import TERMINATED, Behavior, EvalCase, Outcome

#: First draft + one coverage re-prompt + one revision. More is a loop.
MAX_SYNTHESIS_CALLS = 1 + 1 + MAX_REVISE_ROUNDS
#: A run that makes more HTTP calls than this is running away, whatever the cause.
HTTP_CALL_BUDGET = 400
#: Model calls, as the trace names them. Everything else but embeddings is HTTP.
LLM_TOOLS = frozenset({"llm_extraction", "llm_appraisal", "llm_synthesis", "llm_reader_edit"})


def expected_tools(case: EvalCase, enabled: list[str]) -> set[str]:
    if case.expected_tools is not None:
        return set(case.expected_tools)
    tools = {"llm_extraction"}
    if case.expected_behavior in (
        Behavior.ANSWER_WITH_CITATIONS,
        Behavior.ACKNOWLEDGE_UNCERTAINTY,
        Behavior.ACKNOWLEDGE_INSUFFICIENT_EVIDENCE,
        Behavior.REJECT_FALSE_PREMISE,
        Behavior.DEGRADE_GRACEFULLY,
    ):
        tools |= {name for name in enabled if name in SEARCH_TOOLS}
    if case.expected_behavior is Behavior.ANSWER_WITH_CITATIONS:
        tools.add("llm_synthesis")
    return tools


def _calls(trace: dict[str, Any]) -> list[dict[str, Any]]:
    return list(trace.get("tool_calls") or [])


def call_statistics(trace: dict[str, Any]) -> dict[str, Any]:
    calls = _calls(trace)
    http = [c for c in calls if c["tool"] not in LLM_TOOLS | {"embeddings"}]
    # Same request, in order. A repeat right after a throttle is a retry.
    by_request: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for call in http:
        key = (
            call["tool"],
            call["operation"],
            repr(sorted((call.get("request") or {}).items())),
        )
        by_request.setdefault(key, []).append(call)
    retries = redundant = 0
    max_attempts = 0
    for sequence in by_request.values():
        max_attempts = max(max_attempts, len(sequence))
        for previous, _ in itertools.pairwise(sequence):
            throttled = previous.get("http_status") == 429 or previous.get("fault") in {
                "rate_limit",
                "body_throttle",
            }
            if throttled or _failed(previous):
                retries += 1
            else:
                redundant += 1
    per_tool = Counter(c["tool"] for c in calls)
    return {
        "tool_calls": len(calls),
        "http_calls": len(http),
        "http_live": sum(1 for c in http if not c.get("cached") and c["status"] != "fault"),
        "http_cached": sum(1 for c in http if c.get("cached")),
        "llm_calls": sum(1 for c in calls if c["tool"] in LLM_TOOLS),
        "synthesis_calls": per_tool["llm_synthesis"],
        "embedding_calls": per_tool["embeddings"],
        "per_tool": dict(per_tool),
        "retries": retries,
        "redundant_calls": redundant,
        "max_attempts_per_request": max_attempts,
        "failed_calls": sum(1 for c in calls if c["status"] in {"error", "fault"}),
    }


def tool_selection(case: EvalCase, trace: dict[str, Any], enabled: list[str]) -> dict[str, Any]:
    called = {c["tool"] for c in _calls(trace)}
    expected = expected_tools(case, enabled)
    # An unanchored refusal correctly stops before any provider is called; it
    # cannot be blamed for not searching.
    if trace.get("outcome") == Outcome.REFUSED_UNANCHORED:
        expected -= SEARCH_TOOLS | {"llm_synthesis"}
    if trace.get("outcome") == Outcome.RETRIEVAL_ONLY:
        expected.discard("llm_synthesis")
    forbidden = set(case.forbidden_tools)
    missing = sorted(expected - called)
    misused = sorted(forbidden & called)
    stats = call_statistics(trace)
    return {
        "expected": sorted(expected),
        "called": sorted(called),
        "missing": missing,
        "forbidden_called": misused,
        "correct": not missing and not misused,
        "unnecessary_calls": len(misused) + stats["redundant_calls"],
        "ideal_tools_unavailable": sorted(set(case.ideal_tools) - called),
    }


def _covered(draft: dict[str, Any], offered: set[str]) -> int:
    """Sources given a citation of their own — SynthesizeStage's own count."""
    from app.domain.contracts import CITATION_RUN_RE, extract_handles

    body = draft.get("body") or {}
    text = "\n\n".join(
        [body.get("beat_1_claim") or "", body.get("beat_2_evidence") or ""]
        + [f"{s.get('heading', '')}\n\n{s.get('body', '')}" for s in body.get("sections") or []]
        + [body.get("beat_3_bottom_line") or ""]
    )
    covered: set[str] = set()
    for citation in CITATION_RUN_RE.findall(text):
        handles = extract_handles(citation)
        if len(handles) <= MAX_HANDLES_PER_CITATION:
            covered |= handles
    return len(covered & offered)


def decisions(trace: dict[str, Any]) -> list[dict[str, Any]]:
    """Each applicable design rule, and whether the run honoured it."""
    checks: list[dict[str, Any]] = []
    calls = _calls(trace)
    stages = trace.get("stages") or []
    rounds = trace.get("rounds") or []
    metrics = trace.get("stage_metrics") or {}
    outcome = trace.get("outcome")
    synth_calls = [c for c in calls if c["tool"] == "llm_synthesis"]
    payload = trace.get("synthesis_input") or {}
    offered = {s["handle"] for s in payload.get("sources") or []}

    def rule(name: str, passed: bool, detail: str) -> None:
        checks.append({"rule": name, "passed": passed, "detail": detail})

    if payload and not offered:
        rule(
            "no_generation_without_sources",
            not synth_calls,
            "zero sources must take the deterministic no-evidence branch "
            f"(synthesis calls: {len(synth_calls)})",
        )

    if outcome == Outcome.REFUSED_UNANCHORED:
        searched = [c for c in calls if c["tool"] in SEARCH_TOOLS]
        rule(
            "unanchored_query_stops_before_search",
            not searched,
            f"an unanchored query must not reach any provider (calls: {len(searched)})",
        )

    retrieve_spans = [s for s in stages if s["stage"] == "retrieve"]
    if rounds:
        first_thin = rounds[0].get("thin_claims") or []
        refined = any(s["refine_round"] > 0 for s in retrieve_spans)
        if first_thin and MAX_REFINE_ROUNDS > 0:
            rule(
                "refine_thin_claims",
                refined,
                f"{len(first_thin)} thin claim(s) after the first RANK must be re-searched",
            )
        else:
            rule("no_refinement_when_not_thin", not refined, "no thin claims, no second pass")
    if any(s["refine_round"] > MAX_REFINE_ROUNDS for s in stages):
        rule("refinement_bounded", False, f"refine_round exceeded {MAX_REFINE_ROUNDS}")

    # Read off the drafts rather than SYNTHESIZE's metrics: a revision pass
    # overwrites those, and the coverage decision belongs to the first pass.
    first = next((d for d in trace.get("drafts") or [] if d["kind"] == "first"), None)
    if first is not None and offered:
        thin = (
            len(offered) >= SECTIONS_EXPECTED_FROM
            and _covered(first["output"], offered) < len(offered) * MIN_CITED_FRACTION
        )
        reprompted = any(d["kind"] == "coverage" for d in trace.get("drafts") or [])
        rule(
            "coverage_reprompt_rule",
            reprompted == thin,
            f"re-prompt {'expected' if thin else 'not expected'} "
            f"({len(offered)} sources offered), {'made' if reprompted else 'not made'}",
        )

    validations = trace.get("validations") or []
    if validations:
        first_report = validations[0]
        codes = {f["code"] for f in first_report.get("failures") or []}
        # A targeted repair (``SynthesizeStage._repair``) revises without a synthesis call.
        synthesize = (trace.get("stage_metrics") or {}).get("synthesize") or {}
        repaired = bool(synthesize.get("repair"))
        revised = repaired or any(d["kind"] == "revision" for d in trace.get("drafts") or [])
        if not first_report.get("passed") and codes and codes <= REVISABLE_FAILURES:
            rule(
                "revise_fixable_failures",
                revised and len(validations) == 2,
                f"first draft failed only revisable checks ({', '.join(sorted(codes))})",
            )
        elif first_report.get("passed"):
            rule(
                "stop_when_validated",
                not revised and len(synth_calls) <= 2,
                "a validated draft must not be rewritten",
            )

    if len(synth_calls) > MAX_SYNTHESIS_CALLS:
        rule(
            "synthesis_calls_bounded",
            False,
            f"{len(synth_calls)} synthesis calls (max {MAX_SYNTHESIS_CALLS})",
        )

    ranked_any = any(entries for r in rounds for entries in (r.get("ranked") or {}).values())
    if ranked_any and any(s["stage"] == "full_text" for s in stages):
        looked_up = any(
            c["tool"] == "europe_pmc_lookup" and c["stage"] == "full_text" for c in calls
        )
        rule(
            "full_text_lookup_for_ranked_papers",
            looked_up or (metrics.get("full_text") or {}).get("cause") == "disabled",
            "ranked papers exist, so FULL_TEXT must ask Europe PMC for open access",
        )

    search_calls = [c for c in calls if c["tool"] in SEARCH_TOOLS and c["stage"] == "retrieve"]
    if search_calls and all(_failed(c) for c in search_calls):
        rule(
            "total_search_failure_stops_the_run",
            outcome == Outcome.FAILED_RETRYABLE and not synth_calls,
            "every search failed: the run must fail retryably, never write an article",
        )
    return checks


def _failed(call: dict[str, Any]) -> bool:
    """A call that got no usable answer. An injected *empty* result is an
    answer — "searched, found nothing" — and must not count as a failure."""
    if call.get("fault") == "empty":
        return False
    return call["status"] in {"error", "fault"} or (call.get("http_status") or 0) >= 400


def loop_detected(trace: dict[str, Any]) -> bool:
    stats = call_statistics(trace)
    return (
        trace.get("outcome") in {Outcome.LOOP_LIMIT, Outcome.TIMEOUT}
        or stats["synthesis_calls"] > MAX_SYNTHESIS_CALLS
        or stats["http_calls"] > HTTP_CALL_BUDGET
        or any(s["refine_round"] > MAX_REFINE_ROUNDS for s in trace.get("stages") or [])
        or any(s["revise_round"] > MAX_REVISE_ROUNDS for s in trace.get("stages") or [])
    )


def terminated(trace: dict[str, Any]) -> bool:
    return trace.get("outcome") in {str(o) for o in TERMINATED}
