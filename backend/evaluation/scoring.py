"""Score one case from its trace: every check, with the reason it passed or failed.

A case result is a list of named checks, each in a group (query, agent,
retrieval, citations, sources, answers, failure), plus the raw numbers the
aggregate metrics are computed from. The checks are what the per-test debug
report prints — "Citation correctness: FAIL — the article says X, the cited
study only supports Y" — so each carries a detail written for a person.

Scoring reads only the trace (and the evaluators' caches), never the live
system, so ``--rescore`` re-runs exactly this over saved traces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.pipeline.steps import reader_edit
from evaluation.config import EvalConfig
from evaluation.evaluators.credibility import PEER_REVIEWED
from evaluation.evaluators.llm_judge import Judge
from evaluation.evaluators.source_validator import SourceValidator
from evaluation.evaluators.text import (
    body_text,
    contains_any,
    draft_text,
    labelled_article,
    lexical_support,
    number_supported,
    numbers_in,
    statements,
)
from evaluation.harness.trace import SEARCH_TOOLS, Trace
from evaluation.metrics import agent, answers, citations, performance, retrieval, stance
from evaluation.schema import Behavior, EvalCase, Outcome

logger = logging.getLogger(__name__)

#: Per-case pass bars. The aggregate thresholds live in config.yaml; these only
#: decide whether one case's check prints PASS or FAIL in its debug block.
CASE_PRECISION_AT_5 = 0.6
CASE_GOLD_RECALL_AT_10 = 0.5
CASE_CITATION_CORRECTNESS = 0.75
CASE_COMPLETENESS = 0.85
#: A judge "not_supported" counts toward hallucination only when the sentence
#: also shares little vocabulary with its sources — the small judge's labels are
#: noisy, and this keeps one misread from flagging a faithful paraphrase.
HALLUCINATION_LEXICAL_CEILING = 0.35
#: Statements with no judge verdict count as grounded at this lexical support.
GROUNDED_LEXICAL_FLOOR = 0.5

#: Synthesis stand-ins from ``harness/llm.py``. Their drafts are fixtures.
STAND_IN_WRITERS = frozenset({"eval/scripted", "eval/hallucinating"})

EVIDENCE_BEHAVIORS = {
    Behavior.ANSWER_WITH_CITATIONS,
    Behavior.ACKNOWLEDGE_UNCERTAINTY,
    Behavior.DEGRADE_GRACEFULLY,
}


@dataclass
class Evaluators:
    config: EvalConfig
    settings: Settings
    judge: Judge | None
    judge_reason: str | None
    validator: SourceValidator | None
    validator_reason: str | None
    recent_from: int
    #: Gold identifiers confirmed to exist; anything unconfirmed is dropped.
    verified_gold: set[str] = field(default_factory=set)


class _Checks:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, group: str, name: str, passed: bool | None, detail: str) -> None:
        """``passed=None`` records a check that could not be decided (n/a)."""
        self.items.append({"group": group, "name": name, "passed": passed, "detail": detail})


def _claim_only(text: str) -> str:
    """A statement without the reader edit's bracketed explanations.

    "(a study that pools earlier trials)" defines a term for the reader; no
    source states it, and the judge was marking such sentences "partially
    supported". Phrases holding a digit are kept, so effect sizes still count.
    """
    return reader_edit.drop_repeated_explanations(text, reader_edit.explanations(text))


def _source_text(source: dict[str, Any], excerpts: list[dict[str, Any]], words: int) -> str:
    abstract = " ".join(str(source.get("abstract") or "").split()[:words])
    # Whole excerpts: the model saw every word of each chunk (CHUNK_WORDS, 300), and a
    # 200-word cut flagged numbers late in a chunk as misattributed (wt_087, 2026-10-25).
    extra = " ".join(
        f"({e.get('section') or 'excerpt'}) {' '.join(str(e.get('text') or '').split())}"
        for e in excerpts
    )
    return f"{abstract}\n{extra}".strip()


async def score_case(case: EvalCase, trace: Trace, ev: Evaluators) -> dict[str, Any]:
    t = trace.model_dump(mode="json")
    result: dict[str, Any] = {
        "id": case.id,
        "category": str(case.category),
        "subcategory": case.subcategory,
        "query": case.conversation,
        "executed_query": case.executed_query,
        "outcome": t["outcome"],
        "status": "skipped" if t["outcome"] == Outcome.SKIPPED else "executed",
        "skip_reason": t.get("skip_reason"),
        "expected_behavior": str(case.expected_behavior),
        "acceptable_outcomes": sorted(str(o) for o in case.outcomes()),
        "is_failure_case": case.is_failure_case,
        "error": t.get("error"),
    }
    if result["status"] == "skipped":
        result["checks"] = []
        result["passed"] = None
        return result

    checks = _Checks()
    # A retrieval-only suite stops at RANK on purpose; how the run would have
    # ended is not something it observed.
    retrieval_only = t["outcome"] == Outcome.RETRIEVAL_ONLY
    behavior_ok = None if retrieval_only else answers.behavior_ok(case, t["outcome"])
    result["behavior_ok"] = behavior_ok
    checks.add(
        "answers",
        "behavior",
        behavior_ok,
        f"outcome {t['outcome']}; acceptable: {', '.join(result['acceptable_outcomes'])}",
    )

    if case.research_scenario:
        _score_research(case, t, checks, result)
        return _finish(result, checks)

    enabled = list(ev.settings.enabled_providers)
    _score_query(case, t, checks, result)
    _score_agent(case, t, checks, result, enabled)
    _score_retrieval(case, t, checks, result, ev)
    # Recorded, not checked: the labels gate nothing yet, so a case should not
    # pass or fail on them. They are aggregated as INFO metrics.
    result["stance"] = stance.evaluate(
        t,
        {
            claim: {key: str(value) for key, value in labels.items()}
            for claim, labels in case.expected_stances.items()
        },
    )
    if t.get("final_draft"):
        await _score_answer(case, t, checks, result, ev)
    if case.is_failure_case:
        _score_failure(case, t, checks, result, ev)
    result["performance"] = performance.case_performance(t)
    result["degraded_providers"] = [] if case.faults else degraded_providers(t)
    return _finish(result, checks)


def degraded_providers(t: dict[str, Any]) -> list[str]:
    """Search providers that answered none of their calls, with no fault injected.

    The run goes on without them — RETRIEVE only fails when *every* provider
    fails a claim — so the case is still scored, but its numbers describe a
    system with less recall than configured. Tagged so the report can compare
    those cases with the rest rather than silently averaging the two.
    """
    calls = [c for c in t.get("tool_calls") or [] if c["tool"] in SEARCH_TOOLS]
    tools = sorted({c["tool"] for c in calls})
    return [tool for tool in tools if all(agent._failed(c) for c in calls if c["tool"] == tool)]


def _finish(result: dict[str, Any], checks: _Checks) -> dict[str, Any]:
    result["checks"] = checks.items
    decided = [c for c in checks.items if c["passed"] is not None]
    result["passed"] = all(c["passed"] for c in decided)
    result["failed_checks"] = [c["name"] for c in decided if not c["passed"]]
    return result


def _score_query(
    case: EvalCase, t: dict[str, Any], checks: _Checks, result: dict[str, Any]
) -> None:
    extraction = t.get("extraction")
    if extraction is None:
        result["query_understanding"] = {"extracted": False}
        if case.expected_subject and not any(f.target == "llm_extraction" for f in case.faults):
            checks.add("query", "query_understanding", False, "extraction produced nothing")
        return
    subject_text = " ".join([extraction["product"], *extraction.get("ingredients", [])])
    claims_text = " ".join(extraction.get("target_claims", []))
    subject_found = (
        retrieval.mentions(subject_text, case.expected_subject)
        if case.expected_subject
        else None
    )
    outcome_found = (
        retrieval.mentions(claims_text, case.expected_outcome_terms)
        if case.expected_outcome_terms
        else None
    )
    decided = [v for v in (subject_found, outcome_found) if v is not None]
    correct = all(decided) if decided else None
    carryover = None
    if case.conversation_subject:
        carryover = retrieval.mentions(
            f"{subject_text} {claims_text}", case.conversation_subject
        )
    result["query_understanding"] = {
        "extracted": True,
        "product": extraction["product"],
        "claims": extraction.get("target_claims", []),
        "ingredients": extraction.get("ingredients", []),
        "subject_found": subject_found,
        "outcome_found": outcome_found,
        "correct": correct,
        "context_carryover": carryover,
    }
    if correct is not None:
        checks.add(
            "query",
            "query_understanding",
            correct,
            f"product={extraction['product']!r} ingredients={extraction.get('ingredients')} "
            f"claims={extraction.get('target_claims')}; subject "
            f"{'found' if subject_found else 'MISSING' if subject_found is False else 'n/a'}"
            f" ({case.expected_subject}), outcome "
            f"{'found' if outcome_found else 'MISSING' if outcome_found is False else 'n/a'}"
            f" ({case.expected_outcome_terms})",
        )
    if carryover is not None:
        checks.add(
            "query",
            "context_carryover",
            carryover,
            f"conversation subject {case.conversation_subject} "
            f"{'carried into' if carryover else 'absent from'} the extracted topic "
            "(the pipeline receives only the last turn)",
        )


def _score_agent(
    case: EvalCase,
    t: dict[str, Any],
    checks: _Checks,
    result: dict[str, Any],
    enabled: list[str],
) -> None:
    selection = agent.tool_selection(case, t, enabled)
    rules = agent.decisions(t)
    stats = agent.call_statistics(t)
    loop = agent.loop_detected(t)
    term = agent.terminated(t)
    result["agent"] = {
        "tool_selection": selection,
        "decisions": rules,
        "stats": stats,
        "loop_detected": loop,
        "terminated": term,
        "trace_decisions": t.get("decisions") or [],
    }
    detail = f"called {selection['called']}"
    if selection["missing"]:
        detail += f"; MISSING {selection['missing']}"
    if selection["forbidden_called"]:
        detail += f"; FORBIDDEN {selection['forbidden_called']}"
    checks.add("agent", "tool_selection", selection["correct"], detail)
    failed_rules = [r for r in rules if not r["passed"]]
    if rules:
        checks.add(
            "agent",
            "decisions",
            not failed_rules,
            "; ".join(f"{r['rule']}: {r['detail']}" for r in failed_rules)
            or f"{len(rules)} rule(s) honoured: {', '.join(r['rule'] for r in rules)}",
        )
    checks.add("agent", "termination", term, f"outcome {t['outcome']}")
    checks.add(
        "agent",
        "no_loop",
        not loop,
        f"synthesis calls {stats['synthesis_calls']}, http calls {stats['http_calls']}, "
        f"max attempts/request {stats['max_attempts_per_request']}",
    )


def _score_retrieval(
    case: EvalCase, t: dict[str, Any], checks: _Checks, result: dict[str, Any], ev: Evaluators
) -> None:
    gold = {key for key in case.relevant_ids if key in ev.verified_gold}
    metrics = retrieval.evaluate(
        t,
        subject=case.expected_subject,
        outcome=case.expected_outcome_terms,
        gold=gold,
        ks=ev.config.evaluation.retrieval_ks,
        recent_from=ev.recent_from,
    )
    metrics["gold_used"] = sorted(gold)
    metrics["gold_unverified"] = sorted(set(case.relevant_ids) - gold)
    result["retrieval"] = metrics
    if not metrics.get("applicable"):
        return
    expects_evidence = case.expected_behavior in EVIDENCE_BEHAVIORS and not case.faults
    problems = []
    if expects_evidence and metrics["empty"]:
        problems.append("no paper survived ranking")
    p5 = metrics.get("precision@5")
    if p5 is not None and p5 < CASE_PRECISION_AT_5:
        problems.append(f"precision@5 {p5:.2f} < {CASE_PRECISION_AT_5}")
    gr = metrics.get("gold_recall@10")
    if gr is not None and gr < CASE_GOLD_RECALL_AT_10:
        problems.append(f"gold recall@10 {gr:.2f} (missing {_missing_gold(metrics, gold)})")
    checks.add(
        "retrieval",
        "retrieval",
        not problems,
        "; ".join(problems)
        or f"{metrics['ranked_total']} ranked from {metrics['pool_total']} candidates"
        + (f", precision@5 {p5:.2f}" if p5 is not None else "")
        + (f", gold recall@10 {gr:.2f}" if gr is not None else ""),
    )
    if case.requires_recency and metrics.get("recent_fraction") is not None:
        result["retrieval"]["requires_recency"] = True


def _missing_gold(metrics: dict[str, Any], gold: set[str]) -> list[str]:
    found: set[str] = set()
    for row in metrics.get("ranked") or []:
        found |= retrieval.identity_keys(row)
    return sorted(gold - found)


async def _score_answer(
    case: EvalCase, t: dict[str, Any], checks: _Checks, result: dict[str, Any], ev: Evaluators
) -> None:
    draft = t["final_draft"]
    writer = next(
        (
            c.get("model")
            for c in reversed(t.get("tool_calls") or [])
            if c["tool"] == "llm_synthesis"
        ),
        None,
    )
    payload = t.get("synthesis_input") or {}
    papers: dict[str, dict[str, Any]] = t.get("papers") or {}
    excerpts: dict[str, list[dict[str, Any]]] = t.get("excerpts") or {}
    verdict = draft.get("verdict")
    text = draft_text(draft)
    stmts = statements(draft)
    validation = t.get("final_validation") or {}

    verdict_ok = answers.verdict_ok(case, verdict)
    present, total_concepts, missing_concepts = answers.concepts_present(case, text)
    bad_phrases = contains_any(text, case.forbidden_phrases)
    cited = citations.cited_sources(stmts, payload)
    invented = citations.invented_handles(stmts, payload)
    cited_findings, findings = citations.completeness(stmts)

    # --- sources: do the cited papers exist? ---------------------------------
    source_checks: dict[str, dict[str, Any]] = {}
    if ev.validator is not None and cited:
        records = [papers[s["source_id"]] for s in cited.values() if s["source_id"] in papers]
        try:
            found = await ev.validator.validate(records)
            source_checks = {sid: check.as_dict() for sid, check in found.items()}
        except Exception as exc:  # validation is evidence about the run, never fatal
            logger.warning("source validation failed for %s: %s", case.id, exc)

    # --- relevance of what was cited and offered -----------------------------
    def grade(source_id: str) -> int | None:
        if not case.expected_subject or source_id not in papers:
            return None
        return retrieval.relevance_grade(
            papers[source_id], case.expected_subject, case.expected_outcome_terms
        )

    precise = 0
    citation_rows = []
    for handle, source in cited.items():
        check = source_checks.get(source["source_id"], {})
        status = check.get("status", "unchecked")
        valid = status not in {"nonexistent", "malformed", "title_mismatch", "retracted"}
        relevance = grade(source["source_id"])
        on_topic = relevance is None or relevance >= 1
        precise += int(valid and on_topic)
        paper = papers.get(source["source_id"], {})
        citation_rows.append(
            {
                "handle": handle,
                "source_id": source["source_id"],
                "pmid": paper.get("pmid"),
                "doi": paper.get("doi"),
                "title": source.get("title"),
                "study_type": source.get("study_type"),
                "year": source.get("year"),
                "relevance": relevance,
                "registry": status,
                "injected": paper.get("injected", False),
            }
        )
    offered_relevant = [
        s["handle"] for s in payload.get("sources") or [] if (grade(s["source_id"]) or 0) >= 2
    ]
    recall = (
        len([h for h in offered_relevant if h in cited]) / len(offered_relevant)
        if offered_relevant
        else None
    )
    quality = citations.quality(list(cited.values()), papers)

    # --- deterministic support per cited statement ---------------------------
    by_handle = {s["handle"]: s for s in payload.get("sources") or []}
    # Every number in every source the model was shown. A number missing from
    # the cited source but present here was *misattributed* — real, cited to
    # the wrong paper — which needs a different fix from one in no source at
    # all, which was invented.
    prompt_numbers = numbers_in(
        " ".join(
            f"{s.get('title', '')} {s.get('year') or ''} "
            + _source_text(s, excerpts.get(s["source_id"], []), 10_000)
            for s in payload.get("sources") or []
        )
    )
    statement_rows: list[dict[str, Any]] = []
    for index, stmt in enumerate(stmts):
        if not stmt.handles:
            continue
        sources = [by_handle[h] for h in stmt.handles if h in by_handle]
        corpus = "\n".join(
            f"{s.get('title', '')} {s.get('year') or ''} "
            + _source_text(s, excerpts.get(s["source_id"], []), 10_000)
            for s in sources
        )
        source_numbers = numbers_in(corpus)
        unsupported_numbers = [
            n for n in stmt.numbers if not number_supported(n, source_numbers)
        ]
        statement_rows.append(
            {
                "index": index,
                "location": stmt.location,
                "text": stmt.text,
                "raw": stmt.raw,
                "handles": stmt.handles,
                "finding": stmt.is_finding,
                "lexical_support": round(lexical_support(_claim_only(stmt.text), corpus), 3),
                "unsupported_numbers": unsupported_numbers,
                "invented_numbers": [
                    n for n in unsupported_numbers if not number_supported(n, prompt_numbers)
                ],
                "judge": None,
                "judge_explanation": None,
            }
        )

    # --- judge: citation correctness and the answer as a whole ---------------
    judge_answer = None
    if ev.judge is not None:
        for row in _pick_for_judging(statement_rows, ev.config.judge.max_citation_checks):
            sources = [
                {
                    "handle": h,
                    "title": by_handle[h].get("title"),
                    "study_type": by_handle[h].get("study_type"),
                    "year": by_handle[h].get("year"),
                    "text": _source_text(
                        by_handle[h],
                        excerpts.get(by_handle[h]["source_id"], []),
                        ev.config.judge.max_source_words,
                    ),
                }
                for h in row["handles"]
                if h in by_handle
            ]
            if not sources:
                continue
            verdict_j = await ev.judge.check_citation(
                _claim_only(row["raw"] or row["text"]), sources
            )
            if verdict_j is not None:
                row["judge"] = verdict_j.verdict
                row["judge_explanation"] = verdict_j.explanation
        judge_answer = await ev.judge.assess_answer(
            question=case.conversation,
            article=labelled_article(draft),
            sources=[
                {
                    "handle": s["handle"],
                    "title": s.get("title"),
                    "study_type": s.get("study_type"),
                    "year": s.get("year"),
                }
                for s in payload.get("sources") or []
            ],
            expected_claims=case.expected_claims,
            forbidden_claims=case.forbidden_claims,
        )

    judged = [row for row in statement_rows if row["judge"]]
    correctness = (
        sum(citations.SUPPORT_SCORE[row["judge"]] for row in judged) / len(judged)
        if judged
        else None
    )
    lexical_proxy = (
        sum(min(1.0, row["lexical_support"] / GROUNDED_LEXICAL_FLOOR) for row in statement_rows)
        / len(statement_rows)
        if statement_rows
        else None
    )
    grounded_rows = [_grounded(row) for row in statement_rows]
    groundedness = sum(grounded_rows) / len(grounded_rows) if grounded_rows else None

    expected_status: list[dict[str, Any]] = []
    forbidden_status: list[dict[str, Any]] = []
    if judge_answer is not None:
        by_index = {item.index: item.status for item in judge_answer.expected_claims}
        expected_status = [
            {"claim": claim, "status": by_index.get(i)}
            for i, claim in enumerate(case.expected_claims, start=1)
        ]
        asserted = {item.index: item.asserted for item in judge_answer.forbidden_claims}
        forbidden_status = [
            {"claim": claim, "asserted": asserted.get(i)}
            for i, claim in enumerate(case.forbidden_claims, start=1)
        ]

    # --- hallucination ------------------------------------------------------
    reasons: list[str] = []
    if invented:
        reasons.append(f"cites handles never provided: {', '.join(invented)}")
    for row in statement_rows:
        if row["unsupported_numbers"]:
            invented_numbers = row["invented_numbers"]
            moved = [n for n in row["unsupported_numbers"] if n not in invented_numbers]
            if moved:
                reasons.append(
                    f"numbers {moved} in “{_short(row['text'])}” are not in "
                    f"{', '.join(row['handles'])} but are in another source (misattributed)"
                )
            if invented_numbers:
                reasons.append(
                    f"numbers {invented_numbers} in “{_short(row['text'])}” are in no source "
                    "the model was shown (invented)"
                )
        if row["judge"] == "contradicted":
            reasons.append(f"{', '.join(row['handles'])} contradicts “{_short(row['text'])}”")
        elif (
            row["judge"] == "not_supported"
            and row["lexical_support"] < HALLUCINATION_LEXICAL_CEILING
        ):
            reasons.append(
                f"“{_short(row['text'])}” is not in {', '.join(row['handles'])} "
                f"(judge: {row['judge_explanation']})"
            )
    for item in forbidden_status:
        if item["asserted"]:
            reasons.append(f"asserts forbidden claim: {item['claim']}")
    if bad_phrases:
        reasons.append(f"contains forbidden phrase(s): {bad_phrases}")
    nonexistent = [
        row["handle"]
        for row in citation_rows
        if row["registry"] in {"nonexistent", "malformed"} and not row["injected"]
    ]
    if nonexistent:
        reasons.append(f"cites sources no registry knows: {', '.join(nonexistent)}")

    certainty = judge_answer.certainty if judge_answer is not None else None
    uncertainty_ok = answers.uncertainty_appropriate(case, verdict, text, certainty)
    recent_cited = [
        row for row in citation_rows if row.get("year") and row["year"] >= ev.recent_from
    ]
    with_year = [row for row in citation_rows if row.get("year")]
    recency = len(recent_cited) / len(with_year) if with_year else None
    tiers = quality["tiers"]
    types_cited = {str(row["study_type"]) for row in citation_rows} | set(tiers)
    if any(t in PEER_REVIEWED for t in tiers):
        types_cited.add("peer_reviewed")

    validated = bool(validation.get("passed"))
    judge_correct = (judge_answer.correctness - 1) / 4 if judge_answer is not None else None
    answer_correct = (
        bool(result.get("behavior_ok"))
        and verdict_ok is not False
        and not bad_phrases
        and not any(item["asserted"] for item in forbidden_status)
        and (judge_correct is None or judge_correct >= 0.75)
    )

    result["answer"] = {
        "headline": draft.get("headline"),
        "verdict": verdict,
        "verdict_qualifier": draft.get("verdict_qualifier"),
        "summary": draft.get("summary"),
        "text": draft_text(draft, markers=True),
        "body": body_text(draft),
        "written_by": writer,
        "validated": validated,
        "validation_failures": sorted({f["code"] for f in validation.get("failures") or []}),
        "verdict_ceiling": validation.get("verdict_ceiling"),
        "verdict_ok": verdict_ok,
        "acceptable_verdicts": sorted(str(v) for v in case.verdicts() or []),
        "concepts": {"present": present, "total": total_concepts, "missing": missing_concepts},
        "forbidden_phrases_found": bad_phrases,
        "expected_claims": expected_status,
        "forbidden_claims": forbidden_status,
        "judge": judge_answer.model_dump() if judge_answer is not None else None,
        "uncertainty_appropriate": uncertainty_ok,
        "hallucination": bool(reasons),
        "hallucination_reasons": reasons,
        "groundedness": groundedness,
        "answer_correct": answer_correct,
        "recent_cited_fraction": recency if case.requires_recency else None,
        "drafts": len(t.get("drafts") or []),
    }
    result["citations"] = {
        "cited": citation_rows,
        "count": len(cited),
        "invented_handles": invented,
        "precision": precise / len(cited) if cited else None,
        "precise": precise,
        "correctness": correctness,
        "lexical_support_proxy": lexical_proxy,
        "judged": len(judged),
        "statements": statement_rows,
        "findings": findings,
        "cited_findings": cited_findings,
        "completeness": cited_findings / findings if findings else None,
        "recall": recall,
        "quality": quality["score"],
        "tiers": tiers,
    }
    result["sources"] = {"checks": source_checks}

    # --- checks --------------------------------------------------------------
    if writer in STAND_IN_WRITERS:
        # The draft came from the eval's own scripted or adversarial writer, not
        # from the system's model: grading its prose would grade the test
        # fixture. What the case checks is how the system handled it, which
        # the behaviour and failure checks cover.
        return
    evidence_expected = case.expected_behavior in EVIDENCE_BEHAVIORS
    if verdict_ok is not None:
        checks.add(
            "answers",
            "verdict",
            verdict_ok,
            f"verdict {verdict}; acceptable {result['answer']['acceptable_verdicts']}"
            + (f"; ceiling {validation.get('verdict_ceiling')}" if validation else ""),
        )
    checks.add(
        "answers",
        "validation",
        validated or not evidence_expected,
        "passed" if validated else f"failed: {result['answer']['validation_failures']}",
    )
    if evidence_expected:
        checks.add(
            "citations", "citation_existence", bool(cited), f"{len(cited)} source(s) cited"
        )
    if cited:
        invalid = {"nonexistent", "malformed", "title_mismatch", "retracted"}
        imprecise = [
            r["handle"]
            for r in citation_rows
            if r["relevance"] == 0 or r["registry"] in invalid
        ]
        checks.add(
            "citations",
            "citation_precision",
            precise == len(cited),
            f"{precise}/{len(cited)} cited sources are valid and on topic"
            + (f"; off-topic or invalid: {imprecise}" if imprecise else ""),
        )
    if correctness is not None:
        unsupported = [r for r in judged if r["judge"] in {"not_supported", "contradicted"}]
        checks.add(
            "citations",
            "citation_correctness",
            correctness >= CASE_CITATION_CORRECTNESS
            and not any(r["judge"] == "contradicted" for r in judged),
            f"support {correctness:.2f} over {len(judged)} judged statement(s)"
            + "".join(
                f"; {', '.join(r['handles'])} {r['judge']}: “{_short(r['text'])}” — "
                f"{r['judge_explanation']}"
                for r in unsupported[:3]
            ),
        )
    if findings:
        uncited = [s.text for s in stmts if s.evidential and s.is_finding and not s.handles]
        checks.add(
            "citations",
            "citation_completeness",
            cited_findings / findings >= CASE_COMPLETENESS,
            f"{cited_findings}/{findings} finding sentences cited"
            + (f"; uncited e.g. “{_short(uncited[0])}”" if uncited else ""),
        )
    if source_checks:
        bad = [
            f"{r['handle']}={r['registry']}"
            for r in citation_rows
            if r["registry"] not in {"verified", "unverifiable", "unchecked"}
            and not r["injected"]
        ]
        checks.add(
            "sources",
            "sources_exist",
            not bad,
            "; ".join(bad)
            or f"{len(source_checks)} cited source(s) verified in PubMed/Crossref",
        )
    if groundedness is not None:
        checks.add(
            "answers",
            "groundedness",
            groundedness >= CASE_COMPLETENESS,
            f"{sum(grounded_rows)}/{len(grounded_rows)} cited statements grounded",
        )
    checks.add(
        "answers",
        "no_hallucination",
        not reasons,
        "; ".join(reasons[:4]) or "no unsupported number, invented handle or unsupported claim",
    )
    if total_concepts:
        checks.add(
            "answers",
            "required_concepts",
            not missing_concepts,
            f"{present}/{total_concepts} present"
            + (f"; missing {missing_concepts}" if missing_concepts else ""),
        )
    if expected_status:
        present_claims = sum(1 for s in expected_status if s["status"] == "present")
        checks.add(
            "answers",
            "expected_claims",
            present_claims >= max(1, len(expected_status) // 2)
            and not any(s["status"] == "contradicted" for s in expected_status),
            "; ".join(f"{s['status']}: {s['claim']}" for s in expected_status),
        )
    if case.forbidden_claims or case.forbidden_phrases:
        checks.add(
            "answers",
            "forbidden_claims",
            not bad_phrases and not any(s["asserted"] for s in forbidden_status),
            "; ".join(f"asserted: {s['claim']}" for s in forbidden_status if s["asserted"])
            or (f"phrases {bad_phrases}" if bad_phrases else "none asserted"),
        )
    if uncertainty_ok is not None:
        checks.add(
            "answers",
            "uncertainty",
            uncertainty_ok,
            f"verdict {verdict}, judge certainty {certainty}, hedged prose "
            f"{'yes' if answers.hedged(text) else 'no'}",
        )
    if case.expected_source_types:
        matched = sorted(types_cited & set(case.expected_source_types))
        checks.add(
            "citations",
            "source_types",
            bool(matched),
            f"cited {sorted(types_cited)}; expected any of {case.expected_source_types}",
        )
    if case.min_citations is not None:
        checks.add(
            "citations",
            "min_citations",
            len(cited) >= case.min_citations,
            f"{len(cited)} cited, at least {case.min_citations} expected",
        )
    if case.requires_recency and recency is not None:
        checks.add(
            "retrieval",
            "recency",
            recency >= 0.5,
            f"{len(recent_cited)}/{len(with_year)} cited sources from {ev.recent_from} on",
        )


def _grounded(row: dict[str, Any]) -> bool:
    if row["unsupported_numbers"]:
        return False
    if row["judge"] is not None:
        return row["judge"] in {"supported", "partially_supported"}
    return row["lexical_support"] >= GROUNDED_LEXICAL_FLOOR


def _pick_for_judging(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Findings first (they carry the claims), in reading order, up to ``limit``."""
    findings = [r for r in rows if r["finding"]]
    others = [r for r in rows if not r["finding"]]
    return (findings + others)[:limit]


def _short(text: str, limit: int = 110) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _score_failure(
    case: EvalCase, t: dict[str, Any], checks: _Checks, result: dict[str, Any], ev: Evaluators
) -> None:
    calls = t.get("tool_calls") or []
    stats = (result.get("agent") or {}).get("stats") or agent.call_statistics(t)
    retrieve_search = [
        c for c in calls if c["tool"] in SEARCH_TOOLS and c["stage"] == "retrieve"
    ]
    all_search_failed = bool(retrieve_search) and all(agent._failed(c) for c in retrieve_search)
    synthesis_called = any(c["tool"] == "llm_synthesis" for c in calls)
    article = answers.produced_article(t["outcome"])
    answer = result.get("answer") or {}
    # A stand-in writer's draft is a fixture: what it "hallucinates" says
    # nothing about the system, which is only judged on whether it let it through.
    hallucinated = bool(answer.get("hallucination")) and (
        answer.get("written_by") not in STAND_IN_WRITERS
    )
    reached_queue = t["outcome"] in {Outcome.ANSWERED, Outcome.NO_EVIDENCE}
    fabricated_reasons = []
    if all_search_failed and (synthesis_called or article):
        fabricated_reasons.append("an article was produced after every search failed")
    if reached_queue and hallucinated:
        fabricated_reasons.append("a hallucinating draft passed validation")
    fabricated = bool(fabricated_reasons)

    max_retries = ev.config.failure_suite.provider_max_retries
    bounded = stats["max_attempts_per_request"] <= max_retries + 1
    fallback_rows = []
    for tool in case.expected_fallback:
        answered = [
            c
            for c in calls
            if c["tool"] == tool and c["status"] == "ok" and (c.get("result_count") or 0) > 0
        ]
        fallback_rows.append({"tool": tool, "answered": len(answered)})
    fallback_ok = all(row["answered"] for row in fallback_rows) if fallback_rows else None

    error = t.get("error") or {}
    retry_ok = None
    if case.expect_retryable is not None and error:
        retry_ok = bool(error.get("retryable")) == case.expect_retryable

    recovered = (
        bool(result.get("behavior_ok"))
        and agent.terminated(t)
        and not agent.loop_detected(t)
        and not fabricated
        and bounded
    )
    result["failure"] = {
        "recovered": recovered,
        "fabricated": fabricated,
        "fabricated_reasons": fabricated_reasons,
        "all_search_failed": all_search_failed,
        "bounded_retries": bounded,
        "max_attempts_per_request": stats["max_attempts_per_request"],
        "fallback": fallback_rows,
        "fallback_ok": fallback_ok,
        "retry_classification_ok": retry_ok,
        "error": error,
        "loop": agent.loop_detected(t),
        # Only output that would reach the review queue counts: a hallucinating
        # draft that validation stopped is the guard working, not a failure.
        "hallucinated_after_failure": fabricated,
    }
    checks.add(
        "failure",
        "failure_recovery",
        recovered,
        f"outcome {t['outcome']}, bounded retries {bounded} "
        f"(max {stats['max_attempts_per_request']} attempts/request)"
        + (f"; {'; '.join(fabricated_reasons)}" if fabricated_reasons else ""),
    )
    if fallback_ok is not None:
        checks.add(
            "failure",
            "fallback",
            fallback_ok,
            ", ".join(f"{r['tool']}: {r['answered']} answered call(s)" for r in fallback_rows),
        )
    if retry_ok is not None:
        checks.add(
            "failure",
            "retry_classification",
            retry_ok,
            f"failure retryable={error.get('retryable')}, expected {case.expect_retryable} "
            f"({error.get('type')}: {str(error.get('message'))[:120]})",
        )


def _score_research(
    case: EvalCase, t: dict[str, Any], checks: _Checks, result: dict[str, Any]
) -> None:
    research = t.get("research") or {}
    fabrication = research.get("checks") or []
    fallback = research.get("fallback")
    for item in fabrication:
        checks.add("failure", item["name"], item["passed"], item["detail"])
    if fallback is not None:
        checks.add("failure", "fallback", fallback["passed"], fallback["detail"])
    terminated = t["outcome"] in {Outcome.RESEARCH_COMPLETED, Outcome.RESEARCH_FAILED}
    recovered = (
        bool(result.get("behavior_ok"))
        and terminated
        and all(item["passed"] for item in fabrication)
    )
    checks.add(
        "failure",
        "failure_recovery",
        recovered,
        (f"run failed: {research['failed']}" if research.get("failed") else "run completed")
        + f"; selected {len(research.get('selected') or [])}",
    )
    result["research"] = research
    result["failure"] = {
        "recovered": recovered,
        "fabricated": not all(item["passed"] for item in fabrication),
        "fallback_ok": fallback["passed"] if fallback else None,
        "loop": not terminated,
        "hallucinated_after_failure": not all(item["passed"] for item in fabrication),
        "retry_classification_ok": None,
    }
    result["agent"] = {"terminated": terminated, "loop_detected": not terminated}
