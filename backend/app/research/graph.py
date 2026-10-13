"""A research run as one LangGraph workflow: trend search, then the evidence check.

```
TREND SEARCH (trends/graph.py)
load_config -> collect_sources -> normalize_items -> cluster_topics
  -> check_history -> enrich_topics -> score_topics -> select_trends
                                                          |
     (an evidence check is wired, and nothing failed)  ---+---> END otherwise
                                                          v
EVIDENCE CHECK (evidence.py)
research_science -> deep_research -> select
  select --(fewer than the minimum, reserve left, round 0)--> backfill -> research_science
  select --> END
```

Every edge also has a way out: a node that sets ``state.failed`` ends the run.
The one loop (backfill) runs at most ``MAX_BACKFILL_ROUNDS`` times.

**The split is the point.** The trend half finds and scores what people are
getting interested in and never consults the literature; the evidence half
asks PubMed and Europe PMC about the trend half's best topics only, and is the
only thing that can mark a topic ``selected``. So a trend score always means
"how much is this trending", and a proposal on the desk always means
"trending, *and* there is literature to write from". Built without the
evidence half (``evidence=None``) the same class is the standalone Trend
Search Agent, which is what ``scripts/trend_search.py`` runs.

**Nothing in here generates an article or enqueues a run.** A reviewer
promoting a selected topic is what spends a generation run. The graph decides
what runs next; the one state key is replaced wholesale by each node — the
same shape as ``pipeline/graph.py``.
"""

from __future__ import annotations

import itertools
from typing import Any

from langgraph.graph import END, StateGraph

from app.research import evidence as evidence_module
from app.research.contracts import ResearchStage, ResearchState
from app.research.evidence import MAX_BACKFILL_ROUNDS, EvidenceDeps, EvidenceNodes
from app.research.runtime import Checkpoint, GraphState, node, unless_failed
from app.research.trends import graph as trends_graph
from app.research.trends.graph import TrendDeps, TrendSearchNodes

__all__ = ["MAX_BACKFILL_ROUNDS", "ResearchAgent", "recursion_limit"]


class ResearchAgent:
    def __init__(
        self,
        trend: TrendDeps,
        *,
        evidence: EvidenceDeps | None = None,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        self._evidence = EvidenceNodes(evidence) if evidence is not None else None
        # The evidence check's providers get their status rows when the run
        # loads its config, so the desk lists every dependency from the start.
        self._trends = TrendSearchNodes(
            trend,
            extra_providers=self._evidence.provider_names() if self._evidence else None,
        )
        self._checkpoint = checkpoint
        self._now = trend.now
        self._graph = self._build()

    async def run(self, state: ResearchState) -> ResearchState:
        final = await self._graph.ainvoke(
            {"state": state},
            config={"recursion_limit": recursion_limit(self._evidence is not None)},
        )
        result: ResearchState = final["state"]
        if result.failed is None:
            result.stage = ResearchStage.COMPLETED
        return result

    def _build(self) -> Any:
        trend_steps = self._trends.steps()
        evidence_steps = self._evidence.steps() if self._evidence is not None else []
        graph: StateGraph[GraphState, None, GraphState, GraphState] = StateGraph(GraphState)
        for name, stage, fn in [*trend_steps, *evidence_steps]:
            graph.add_node(name, node(stage, fn, checkpoint=self._checkpoint, now=self._now))
        graph.set_entry_point(trend_steps[0][0])

        trend_names = [name for name, _, _ in trend_steps]
        for current, following in itertools.pairwise(trend_names):
            graph.add_conditional_edges(
                current, unless_failed(following), {following: following, END: END}
            )
        last_trend = trend_names[-1]
        if self._evidence is None:
            graph.add_edge(last_trend, END)
            return graph.compile()

        graph.add_conditional_edges(
            last_trend,
            unless_failed("research_science"),
            {"research_science": "research_science", END: END},
        )
        for current, following in (
            ("research_science", "deep_research"),
            ("deep_research", "select"),
        ):
            graph.add_conditional_edges(
                current, unless_failed(following), {following: following, END: END}
            )
        graph.add_conditional_edges(
            "select", EvidenceNodes.after_select, {"backfill": "backfill", END: END}
        )
        graph.add_conditional_edges(
            "backfill",
            unless_failed("research_science"),
            {"research_science": "research_science", END: END},
        )
        return graph.compile()


def recursion_limit(with_evidence: bool = True) -> int:
    """Every node once, plus each backfill round's three revisits, plus slack."""
    trend = trends_graph.NODES
    if not with_evidence:
        return trend + 2
    return trend + evidence_module.NODES + MAX_BACKFILL_ROUNDS * 4 + 2
