"""The evidence check: what the literature holds on the trend agent's best topics.

Downstream of the Trend Search Agent and separate from it. The trend phase
ranks topics by how much they are trending and marks the best
``evidence_pool_size`` as ``in_evidence_pool``; this phase asks PubMed and
Europe PMC about those alone, and only those that clear the evidence gate are
**selected** for the desk.

```
research_science -> deep_research -> select
  select --(fewer than the minimum, reserve left, round 0)--> backfill -> research_science
  select --> END
```

* ``research_science``: literature counts on each pooled topic's subject and
  outcome. Nothing at all in either index discards the topic; the rest become
  ``shortlisted``. If neither index could be reached for any topic, the run
  fails — an unchecked topic must never reach the desk looking checked.
* ``deep_research``: the article pipeline's own extraction and anchored query,
  counted in PubMed. A topic whose query cannot be anchored is discarded here
  rather than failing at RETRIEVE after a reviewer spent a run on it.
* ``select``: the gate (``min_evidence_status``), then the trend ranking, one
  topic per subject. Passing topics not chosen stay ``shortlisted``
  ("outscored"), which a reviewer may still promote.
* ``backfill``: when too few passed, the next trend-ranked topics join the
  pool and go through the same steps — once.

Evidence never adds to a trend score. It is a gate and a tie-break.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from langgraph.graph import END

from app.domain.contracts import ExtractionInput
from app.domain.enums import ResearchCandidateStatus
from app.llm.base import ExtractionClient, LLMError
from app.research import scoring
from app.research.contracts import (
    CandidateTopic,
    DeepEvidence,
    EvidenceCounts,
    EvidenceStatus,
    PaperRef,
    ProviderStatus,
    ResearchStage,
    ResearchState,
)
from app.research.providers.base import (
    EvidenceQuery,
    ProviderUnavailable,
    ScientificEvidenceProvider,
)
from app.research.runtime import GraphState, Step, settle
from app.retrieval.base import ProviderError, ScholarlyProvider, SearchQuery
from app.retrieval.query_builder import (
    QueryStrategy,
    TemplateQueryStrategy,
    UnanchoredQuery,
)

logger = logging.getLogger(__name__)

#: Extra evidence rounds when too few topics pass. One: the reserve is the
#: rest of the trend ranking, and a topic that did not make the first pool
#: rarely beats the ones that did.
MAX_BACKFILL_ROUNDS = 1
#: Claims per topic checked in deep research. The first is the topic's own
#: question; a second catches a topic whose first claim was phrased narrowly.
DEEP_CLAIMS = 2
#: Nodes in this fragment, for the graph's recursion limit.
NODES = 4


class TermsEvidence(ScientificEvidenceProvider, Protocol):
    """A science provider that can also count ready-made PubMed terms."""

    async def profile_terms(self, terms: str, recent_since: str) -> EvidenceCounts: ...


@dataclass
class EvidenceDeps:
    """What the evidence check talks to. Any provider may be None (not configured)."""

    #: Primary literature counts (PubMed) and the cross-check (Europe PMC).
    #: Each is the other's fallback.
    science: ScientificEvidenceProvider | None
    cross_check: ScientificEvidenceProvider | None
    #: PubMed counts for the article pipeline's own query terms.
    deep_counts: TermsEvidence | None
    #: PubMed search, for the top papers a reviewer is shown.
    papers: ScholarlyProvider | None
    extraction: ExtractionClient | None
    query_strategy: QueryStrategy = field(default_factory=TemplateQueryStrategy)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


class EvidenceNodes:
    """The evidence check's steps, for ``research/graph.py`` to wire."""

    def __init__(self, deps: EvidenceDeps) -> None:
        self._deps = deps

    def steps(self) -> list[Step]:
        return [
            ("research_science", ResearchStage.RESEARCHING_SCIENCE, self.research_science),
            ("deep_research", ResearchStage.DEEP_RESEARCH, self.deep_research),
            ("select", ResearchStage.SELECTING_TOPICS, self.select),
            ("backfill", ResearchStage.BACKFILLING, self.backfill),
        ]

    def provider_names(self) -> dict[str, object | None]:
        deps = self._deps
        return {
            "pubmed": deps.science,
            "europe_pmc": deps.cross_check,
            "extraction": deps.extraction,
        }

    # --- nodes ---------------------------------------------------------------

    async def research_science(self, state: ResearchState) -> dict[str, Any]:
        primary, cross = self._deps.science, self._deps.cross_check
        recent_since = state.today - timedelta(days=state.config.scientific_window_days)
        pubmed, epmc = state.provider("pubmed"), state.provider("europe_pmc")
        pool = [
            c
            for c in state.live()
            if c.in_evidence_pool and c.science.primary is None and not c.science.failures
        ]
        checked = 0
        for candidate in pool:
            query = EvidenceQuery(
                subject=candidate.subject, outcome=candidate.outcome, recent_since=recent_since
            )
            try:
                main = await _profile(primary, query, pubmed, candidate)
                other = await _profile(cross, query, epmc, candidate)
            except ValueError:
                candidate.discard(f"no searchable subject in {candidate.subject!r}")
                continue
            # Each is the other's fallback: an unchecked topic needs both down.
            candidate.science.primary = main or other
            candidate.science.cross_check = other if main is not None else None
            candidate.evidence_status = scoring.evidence_status(candidate.science.best)
            if candidate.science.primary is None:
                continue
            checked += 1
            if candidate.evidence_status is EvidenceStatus.NONE:
                candidate.discard("no literature found for this topic in PubMed or Europe PMC")
            else:
                candidate.status = ResearchCandidateStatus.SHORTLISTED
        for status in (pubmed, epmc):
            settle(status, [status.detail] if status.failures and status.detail else [])
        if pool and checked == 0:
            state.failed = (
                "scientific_evidence_unavailable: neither PubMed nor Europe PMC could "
                "be reached, so no topic's evidence was checked"
            )
            return {"pool": len(pool), "checked": 0, "unchecked": len(pool)}
        unchecked = 0
        for candidate in pool:
            if candidate.live and candidate.science.primary is None:
                # Both indexes failed for this topic alone: shown with a reason,
                # never left looking like an unexamined candidate.
                candidate.discard(
                    "scientific evidence could not be checked (PubMed and Europe PMC failed)"
                )
                unchecked += 1
        return {"pool": len(pool), "checked": checked, "unchecked": unchecked}

    async def deep_research(self, state: ResearchState) -> dict[str, Any]:
        done = unanchored = failed = 0
        for candidate in state.with_status(ResearchCandidateStatus.SHORTLISTED):
            if candidate.science.deep is not None:
                continue
            outcome = await self._deep(state, candidate)
            if outcome == "unanchored":
                unanchored += 1
            elif outcome == "failed":
                failed += 1
            else:
                done += 1
        return {"researched": done, "unanchored": unanchored, "failed": failed}

    async def _deep(self, state: ResearchState, candidate: CandidateTopic) -> str:
        """Ask what the article pipeline would ask, the way it would ask it.

        Extraction on the canonical topic, then ``TemplateQueryStrategy`` for the
        first claims — the same calls RETRIEVE makes — counted in PubMed. A topic
        whose query cannot be anchored is discarded here: promoted, it would fail
        at RETRIEVE as a non-retryable ``UnanchoredQuery`` having spent a model
        call, which is the pipeline's sharpest documented risk.
        """
        deps = self._deps
        topic = candidate.canonical_topic
        if deps.extraction is None or deps.deep_counts is None:
            return "failed"
        try:
            extracted = (
                await deps.extraction.extract(ExtractionInput(topic=topic))
            ).output
        except LLMError as exc:
            logger.warning("deep research: extraction failed for %r: %s", topic, exc)
            candidate.science.failures.append(f"extraction: {exc}")
            return "failed"

        claims = extracted.target_claims[:DEEP_CLAIMS]
        try:
            built = [
                deps.query_strategy.build(claim, extracted.product, extracted.ingredients)
                for claim in claims
            ]
        except UnanchoredQuery:
            candidate.discard(
                "the article pipeline could not anchor a literature query "
                f"(product {extracted.product!r}, no searchable ingredient)"
            )
            return "unanchored"

        recent_since = (
            state.today - timedelta(days=state.config.scientific_window_days)
        ).isoformat()
        counts: list[EvidenceCounts] = []
        terms_used: list[str] = []
        for queries in built:
            terms = queries[-1].terms  # the general pass; the review pass shares its terms
            terms_used.append(terms)
            try:
                counts.append(await deps.deep_counts.profile_terms(terms, recent_since))
            except ProviderUnavailable as exc:
                logger.warning("deep research: counts unavailable for %r: %s", topic, exc)
                candidate.science.failures.append(f"deep counts: {exc}")
        if not counts:
            return "failed"

        papers: list[PaperRef] = []
        if deps.papers is not None and built:
            try:
                found = await deps.papers.search(
                    SearchQuery(
                        claim=claims[0], terms=terms_used[0], reviews_only=True, max_results=5
                    )
                )
                papers = [
                    PaperRef(
                        pmid=p.pmid, title=p.title, year=p.year, study_type=str(p.study_type)
                    )
                    for p in found
                ]
            except ProviderError as exc:
                logger.warning("deep research: top papers unavailable for %r: %s", topic, exc)
                candidate.science.failures.append(f"top papers: {exc}")

        # The best-evidenced claim sizes the topic. The article's verdict is
        # still capped per claim at VALIDATE; this only decides what to propose.
        best = max(counts, key=lambda c: (scoring.evidence_score(c) or 0.0, c.total))
        candidate.science.deep = DeepEvidence(
            product=extracted.product,
            ingredients=extracted.ingredients,
            claims=claims,
            queries=terms_used,
            counts=best,
            top_papers=papers,
        )
        candidate.evidence_status = scoring.evidence_status(best)
        return "done"

    async def select(self, state: ResearchState) -> dict[str, Any]:
        config = state.config
        for candidate in state.with_status(ResearchCandidateStatus.SELECTED):
            candidate.status = ResearchCandidateStatus.SHORTLISTED

        gated = 0
        for candidate in state.with_status(ResearchCandidateStatus.SHORTLISTED):
            reason = scoring.gate_reason(candidate, min_status=config.min_evidence_status)
            if reason is not None:
                candidate.discard(reason)
                gated += 1

        ranked = scoring.rank(state.with_status(ResearchCandidateStatus.SHORTLISTED))
        chosen = scoring.select(
            ranked,
            target=config.target_article_count,
            max_per_category=_category_cap(state),
        )
        for candidate in chosen:
            candidate.status = ResearchCandidateStatus.SELECTED

        backfilling = (
            len(chosen) < config.min_article_count
            and state.backfill_round < MAX_BACKFILL_ROUNDS
            and bool(reserve(state))
        )
        if len(chosen) < config.target_article_count and not backfilling:
            state.notes.append(
                f"selected {len(chosen)} of {config.target_article_count} requested: "
                "no other trending topic passed the evidence check"
            )
        return {"selected": len(chosen), "discarded_by_gate": gated}

    async def backfill(self, state: ResearchState) -> dict[str, Any]:
        config = state.config
        state.backfill_round += 1
        missing = config.target_article_count - len(
            state.with_status(ResearchCandidateStatus.SELECTED)
        )
        added = reserve(state)[: max(2, 2 * missing)]
        for candidate in added:
            candidate.in_evidence_pool = True
        return {"round": state.backfill_round, "added": len(added)}

    @staticmethod
    def after_select(graph_state: GraphState) -> str:
        state = graph_state["state"]
        selected = len(state.with_status(ResearchCandidateStatus.SELECTED))
        if (
            state.failed is None
            and selected < state.config.min_article_count
            and state.backfill_round < MAX_BACKFILL_ROUNDS
            and reserve(state)
        ):
            return "backfill"
        return END


def reserve(state: ResearchState) -> list[CandidateTopic]:
    """Trend-ranked, eligible topics never sent to the evidence check: backfill's supply."""
    return sorted(
        (
            c
            for c in state.with_status(ResearchCandidateStatus.CANDIDATE)
            if c.rank is not None and not c.in_evidence_pool
        ),
        key=lambda c: c.rank or 0,
    )


def _category_cap(state: ResearchState) -> int | None:
    """``max_per_category``, unless the run asked for a single category."""
    if len(state.config.categories) <= 1:
        return None
    return state.config.max_per_category


async def _profile(
    provider: ScientificEvidenceProvider | None,
    query: EvidenceQuery,
    status: ProviderStatus,
    candidate: CandidateTopic,
) -> EvidenceCounts | None:
    if provider is None:
        return None
    status.calls += 1
    try:
        return await provider.profile(query)
    except ProviderUnavailable as exc:
        logger.warning("evidence profile unavailable for %r: %s", candidate.subject, exc)
        status.failures += 1
        status.detail = str(exc)
        candidate.science.failures.append(str(exc))
        return None
