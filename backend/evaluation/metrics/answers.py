"""Answer-level checks that need no model: behaviour, verdict, concepts, hedging.

The verdict is the system's one-word answer — ``supported`` / ``mixed`` /
``weak`` / ``no_evidence`` — so it is the most checkable thing an article says.
A case states which verdicts the literature allows (written by the case author
from the state of the evidence, conservatively, as a range), and a verdict
outside it is wrong however well the prose reads.
"""

from __future__ import annotations

import re

from app.domain.enums import Verdict
from evaluation.schema import EvalCase, Outcome

#: Language that marks a conclusion as provisional.
HEDGES = re.compile(
    r"\b(may|might|could|limited|uncertain|unclear|inconsistent|mixed|conflicting|"
    r"small (?:studies|trials|sample)|more research|further research|not (?:yet )?"
    r"(?:clear|established|proven)|preliminary|modest|weak|insufficient|few studies|"
    r"no (?:published )?(?:evidence|studies)|varied|caution)\b",
    re.I,
)


def behavior_ok(case: EvalCase, outcome: str) -> bool:
    return outcome in {str(o) for o in case.outcomes()}


def verdict_ok(case: EvalCase, verdict: str | None) -> bool | None:
    allowed = case.verdicts()
    if allowed is None or verdict is None:
        return None
    return verdict in {str(v) for v in allowed}


def concepts_present(case: EvalCase, text: str) -> tuple[int, int, list[list[str]]]:
    """(groups present, groups, missing groups)."""
    lowered = text.lower()
    missing = [
        group
        for group in case.required_concepts
        if not any(term.lower() in lowered for term in group)
    ]
    total = len(case.required_concepts)
    return total - len(missing), total, missing


def hedged(text: str) -> bool:
    return len(HEDGES.findall(text)) >= 2


def uncertainty_appropriate(
    case: EvalCase, verdict: str | None, text: str, judge_certainty: str | None
) -> bool | None:
    """Certainty proportionate to the evidence.

    For cases whose ground truth is "uncertain" (conflicting evidence, thin
    evidence, a false premise) the deterministic test is the verdict being in
    the hedged range *and* the prose not overconfident (the judge's call, or a hedge-word
    count when the judge is off). For every other article only the
    judge can say, and only "overconfident" fails: the verdict cap already
    stops the structured verdict overclaiming, so this is about the prose.
    """
    if verdict is None:
        return None
    hedge_cases = {
        "acknowledge_uncertainty",
        "acknowledge_insufficient_evidence",
        "reject_false_premise",
    }
    if case.expected_behavior.value in hedge_cases:
        allowed = case.verdicts()
        in_range = allowed is None or verdict in {str(v) for v in allowed}
        if verdict == Verdict.NO_EVIDENCE:
            return in_range
        # The judge reads the whole article; a word count missed four plainly hedged ones on
        # 2026-11-10 (wt_001/025/044/055: "Lack Proof", "the evidence says no"). The word
        # count only decides when there is no judge.
        prose_ok = judge_certainty != "overconfident" if judge_certainty else hedged(text)
        return in_range and prose_ok
    if judge_certainty is None:
        return None
    return judge_certainty != "overconfident"


def produced_article(outcome: str) -> bool:
    return outcome in {
        Outcome.ANSWERED,
        Outcome.NO_EVIDENCE,
        Outcome.ANSWERED_UNVALIDATED,
    }
