"""The evidence check's arithmetic, and the selection rules. Pure functions.

The trend score — momentum, coverage, audience, freshness, editorial fit —
lives in ``trends/scoring.py`` and contains nothing about the literature.
What is here answers the downstream question: *can we responsibly say
anything?* — from PubMed and Europe PMC counts only (``evidence_score``,
``evidence_status``) — and turns the trend ranking into a selection:

* the **evidence gate**: a topic below ``min_evidence_status`` is never
  proposed, however high its trend score (popularity is not evidence);
* **ranking** by trend score, ties broken by evidence;
* **diversity**: one topic per subject, at most ``max_per_category`` per
  category.

Evidence never adds to a topic's score. It is a gate and a tie-break, so a
proposal's number always means "how much is this trending".
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlparse

from app.research.contracts import (
    EVIDENCE_RANK,
    CandidateTopic,
    EvidenceCounts,
    EvidenceStatus,
    WebResult,
    WebSignal,
)
from app.research.normalize import query_key


def evidence_score(counts: EvidenceCounts | None) -> float | None:
    """40 for reviews/meta-analyses (full at 3), 35 for RCTs (full at 5), 25 for
    the size of the literature (full at 30 papers). Orders deep-research claims;
    never part of a trend score."""
    if counts is None:
        return None
    return round(
        40.0 * min(1.0, counts.reviews_or_meta / 3.0)
        + 35.0 * min(1.0, counts.rcts / 5.0)
        + 25.0 * min(1.0, counts.total / 30.0),
        1,
    )


def evidence_status(counts: EvidenceCounts | None) -> EvidenceStatus | None:
    """What can responsibly be said, from counts alone.

    * strong: at least 2 reviews/meta-analyses and 3 randomised trials
    * moderate: at least 1 review/meta-analysis, or 3 trials
    * emerging: at least 1 trial, or 10+ papers with 3+ from the last year
    * limited: some literature, none of the above
    * none: nothing at all
    """
    if counts is None:
        return None
    if counts.reviews_or_meta >= 2 and counts.rcts >= 3:
        return EvidenceStatus.STRONG
    if counts.reviews_or_meta >= 1 or counts.rcts >= 3:
        return EvidenceStatus.MODERATE
    if counts.rcts >= 1 or (counts.total >= 10 and counts.recent >= 3):
        return EvidenceStatus.EMERGING
    if counts.total >= 1:
        return EvidenceStatus.LIMITED
    return EvidenceStatus.NONE


def meets(status: EvidenceStatus | None, minimum: EvidenceStatus) -> bool:
    return status is not None and EVIDENCE_RANK[status] >= EVIDENCE_RANK[minimum]


def web_signal(
    results: list[WebResult],
    *,
    source: str,
    subject: str,
    authoritative: Iterable[str],
) -> WebSignal:
    """Summarise one search: how much, how relevant, from how many places.

    A result is relevant when its title or snippet names the subject — every
    word of it — which is crude and deliberately so: it only has to separate
    "a page about creatine" from a page the search engine padded the list with.
    """
    domains = [d.lower() for d in authoritative]
    relevant = [r for r in results if mentions(f"{r.title} {r.snippet}", subject)]
    hosts = {host(result.url) for result in relevant}
    trusted = [result for result in relevant if is_authoritative(host(result.url), domains)]
    return WebSignal(
        source=source,
        results_found=len(results),
        relevant_results=len(relevant),
        distinct_domains=len(hosts - {""}),
        authoritative_results=len(trusted),
        top_results=(trusted + [r for r in relevant if r not in trusted])[:5],
    )


def mentions(text: str, subject: str) -> bool:
    """True when every word of ``subject`` appears in ``text`` (case-insensitive)."""
    words = [word for word in subject.lower().split() if word]
    haystack = text.lower()
    return bool(words) and all(word in haystack for word in words)


def host(url: str) -> str:
    hostname = urlparse(url).hostname or ""
    return hostname.lower().removeprefix("www.")


def is_authoritative(hostname: str, domains: list[str]) -> bool:
    for domain in domains:
        if domain.startswith("."):
            if hostname.endswith(domain):
                return True
        elif hostname == domain or hostname.endswith("." + domain):
            return True
    return False


def gate_reason(candidate: CandidateTopic, *, min_status: EvidenceStatus) -> str | None:
    """Why the evidence check refuses a candidate, or None.

    The gate is what keeps a viral topic with no literature off the desk
    however high its trend score: popularity is not evidence.
    """
    if candidate.science.best is None:
        return "scientific evidence could not be checked (PubMed and Europe PMC failed)"
    status = candidate.evidence_status
    if not meets(status, min_status):
        counts = candidate.science.best
        return (
            f"evidence {status.value if status else 'unknown'} "
            f"({counts.total} papers, {counts.reviews_or_meta} reviews/meta-analyses, "
            f"{counts.rcts} RCTs); needs {min_status.value}"
        )
    return None


def rank(candidates: list[CandidateTopic]) -> list[CandidateTopic]:
    """Live, trend-scored candidates, best first.

    Ties break on evidence (when checked), then on coverage, then on name, so
    the order never depends on the order candidates were built in.
    """
    scored = [c for c in candidates if c.live and c.scores.overall is not None]
    return sorted(
        scored,
        key=lambda c: (
            -(c.scores.overall or 0.0),
            -EVIDENCE_RANK.get(c.evidence_status, -1) if c.evidence_status else 1,
            -c.scores.components.get("coverage", 0.0),
            c.canonical_topic.lower(),
        ),
    )


def select(
    ranked: list[CandidateTopic],
    *,
    target: int,
    max_per_subject: int = 1,
    max_per_category: int | None = None,
) -> list[CandidateTopic]:
    """The top ``target`` candidates, at most ``max_per_subject`` per subject
    and (when given) ``max_per_category`` per category.

    One per subject by default: two creatine angles in one week's proposals is
    one topic proposed twice, and it crowds out a different subject.
    """
    chosen: list[CandidateTopic] = []
    for candidate in ranked:
        words = _subject_words(candidate.subject)
        # One subject containing the other's words is the same subject:
        # measured, "blood pressure" and "blood pressure medication" were both
        # selected under an exact-match rule.
        same = sum(
            1
            for other in chosen
            if words <= _subject_words(other.subject) or _subject_words(other.subject) <= words
        )
        if same >= max_per_subject:
            continue
        if max_per_category is not None and (
            sum(1 for other in chosen if other.category is candidate.category)
            >= max_per_category
        ):
            continue
        chosen.append(candidate)
        if len(chosen) == target:
            break
    return chosen


def _subject_words(subject: str) -> frozenset[str]:
    return frozenset(query_key(subject).split()) or frozenset({subject.strip().lower()})
