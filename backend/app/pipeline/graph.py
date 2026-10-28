"""The stage sequence as a LangGraph state machine.

Split from ``orchestrator.py`` because the two answer different questions. This
file knows *what runs next*; the orchestrator knows *what is committed and what
is recorded*, and that division is the reason the port is safe. Every
transaction boundary, every ``pipeline_stage_runs`` row and every failure path
stays where it was; what moved here is the ``for`` loop that used to decide the
order, and it moved because a loop cannot express the one thing the pipeline now
needs to do that is not a straight line.

**The refinement edge is the whole reason this is a graph.** RANK can tell that
a claim has fewer than ``QUORUM_FOR_SUPPORTED`` supported-tier sources, which
means the article's verdict is already capped by what retrieval found rather
than by what the evidence says. Sending that claim back through RETRIEVE with a
broader query is a cycle, and a cycle is what the old shape could not hold: the
stage list is ordered and each stage runs once. DESIGN.md §5 anticipated exactly
this ("a loop *around* RETRIEVE and RANK") and deferred it.

Two properties are load-bearing and easy to lose:

* **The context is carried by reference, not merged.** ``GraphState`` holds one
  key whose value is the whole ``PipelineContext``, so LangGraph replaces it
  wholesale and never reduces its fields. ``usage_by_stage`` and ``metrics`` are
  mutated in place on purpose — a stage that raises returns no context, and its
  burned tokens would otherwise vanish from exactly the runs whose cost is least
  visible — and a per-field reducer would quietly turn those mutations into
  writes that get dropped on the failure path.
* **Node names are positional.** The pipeline is a list, tests pass lists of one
  stage, and nothing guarantees a given ``StageName`` is present. Keying nodes on
  the enum would make the graph undefined for any list that is not the full
  seven.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.pipeline.stages import PipelineContext, Stage, StageName

#: How many extra passes through RETRIEVE a run may make. One, deliberately:
#: each pass spends real provider budget against a per-IP ceiling shared with
#: every other worker, and the second look either finds the missing trials or
#: they are not there. A claim still thin after broadening is a fact about the
#: literature, and the honest response to it is the cautious verdict the quorum
#: already produces, not a third query.
MAX_REFINE_ROUNDS = 1

#: How many times a draft that failed validation is sent back to SYNTHESIZE
#: with its failures. One: the failures are specific ("S9 was never provided",
#: "beat 2 cites nothing"), so a model that can fix them does so on the first
#: try, and a second failure is a draft that stays ``validation_failed`` — the
#: invariant is that a failing draft never reaches the queue, not that every
#: topic produces one.
MAX_REVISE_ROUNDS = 1

#: Failure codes a rewrite can fix. ``unresolvable_source`` is not one: it
#: means a cited handle's ``sources`` row is missing, which is ours to fix.
REVISABLE_FAILURES = frozenset(
    {
        "hallucinated_handle",
        "uncited_beat",
        "uncited_section",
        "verdict_exceeds_grade",
        "malformed_body",
        "unsourced_number",
        "no_evidence_with_citations",
        "misattributed_citation",
    }
)

#: Extra supersteps allowed beyond the stages themselves, covering the refine
#: node and the re-run of RETRIEVE and RANK on each round. Set from the shape of
#: the graph rather than left at LangGraph's default, so that adding a stage
#: cannot silently walk into a recursion limit that has nothing to say about the
#: actual problem.
_STEPS_PER_REFINEMENT = 3


class GraphState(TypedDict):
    """The context, plus the attempt number the stage rows are written under.

    ``run_id`` is not here because ``PipelineContext`` already carries it, and
    two copies of an identifier is one more than can be kept consistent.
    ``attempt`` has no home on the context — it is a fact about this execution
    of the run, not about the article being built — and threading it through
    state rather than storing it on the orchestrator is what keeps ``run``
    reentrant.
    """

    ctx: PipelineContext
    attempt: int


#: ``(ordinal, stage, ctx, attempt) -> ctx``. The orchestrator's per-stage
#: wrapper: it records the stage row, runs the stage, commits, and records the
#: end. Passed in rather than imported so this module needs no session and no
#: database.
StageRunner = Callable[
    [int, Stage, PipelineContext, int], Awaitable[PipelineContext]
]


def build_pipeline_graph(
    stages: list[Stage],
    runner: StageRunner,
    *,
    max_refine_rounds: int = MAX_REFINE_ROUNDS,
    max_revise_rounds: int = MAX_REVISE_ROUNDS,
) -> Any:
    """Compile the stage list into a graph, with the refinement cycle if it fits.

    The cycle is added only when RETRIEVE and RANK are both present and RETRIEVE
    comes first. Any other list — a single fake stage in a test, a future subset
    — compiles as the plain chain it already was.
    """
    graph: StateGraph = StateGraph(GraphState)  # pyrefly: ignore[bad-specialization]
    names = [_node_name(index) for index in range(len(stages))]

    for index, stage in enumerate(stages):
        graph.add_node(names[index], _stage_node(index, stage, runner))

    graph.set_entry_point(names[0])

    retrieve_at = _index_of(stages, StageName.RETRIEVE)
    rank_at = _index_of(stages, StageName.RANK)
    loops = (
        retrieve_at is not None
        and rank_at is not None
        and retrieve_at < rank_at
        and max_refine_rounds > 0
    )

    synthesize_at = _index_of(stages, StageName.SYNTHESIZE)
    validate_at = _index_of(stages, StageName.VALIDATE)
    revises = (
        synthesize_at is not None
        and validate_at is not None
        and synthesize_at < validate_at
        and max_revise_rounds > 0
    )

    for index in range(len(stages)):
        following = names[index + 1] if index + 1 < len(stages) else END
        if loops and index == rank_at:
            graph.add_node(_REFINE, _refine_node)
            graph.add_edge(_REFINE, names[retrieve_at])  # type: ignore[index]
            graph.add_conditional_edges(
                names[index],
                _router(max_refine_rounds),
                {_REFINE: _REFINE, _CONTINUE: following},
            )
        elif revises and index == validate_at:
            graph.add_node(_REVISE, _revise_node)
            graph.add_edge(_REVISE, names[synthesize_at])  # type: ignore[index]
            graph.add_conditional_edges(
                names[index],
                _revise_router(max_revise_rounds),
                {_REVISE: _REVISE, _CONTINUE: following},
            )
        else:
            graph.add_edge(names[index], following)

    return graph.compile()


def recursion_limit(
    stages: list[Stage], max_refine_rounds: int, max_revise_rounds: int = MAX_REVISE_ROUNDS
) -> int:
    """Enough supersteps for every stage plus each loop's passes, and no more.

    A revision re-runs everything from SYNTHESIZE to VALIDATE, plus its own node.
    """
    synthesize_at = _index_of(stages, StageName.SYNTHESIZE)
    validate_at = _index_of(stages, StageName.VALIDATE)
    per_revision = (
        validate_at - synthesize_at + 2
        if synthesize_at is not None and validate_at is not None
        else 0
    )
    return (
        len(stages)
        + max_refine_rounds * _STEPS_PER_REFINEMENT
        + max_revise_rounds * per_revision
        + 2
    )


_REFINE = "refine"
_REVISE = "revise"
_CONTINUE = "continue"


def _node_name(index: int) -> str:
    return f"stage_{index}"


def _index_of(stages: list[Stage], name: StageName) -> int | None:
    return next(
        (index for index, stage in enumerate(stages) if stage.name is name), None
    )


def _stage_node(
    index: int, stage: Stage, runner: StageRunner
) -> Any:
    async def node(state: GraphState) -> dict[str, PipelineContext]:
        return {"ctx": await runner(index, stage, state["ctx"], state["attempt"])}

    return node


async def _refine_node(state: GraphState) -> dict[str, PipelineContext]:
    """Bump the round counter. No stage row, no commit, no model call.

    A node rather than something the router does, because a LangGraph router
    returns an edge and cannot write state — and because a counter incremented
    invisibly is how a bounded loop becomes an unbounded one.
    """
    ctx = state["ctx"]
    return {"ctx": ctx.model_copy(update={"refine_round": ctx.refine_round + 1})}


def _router(max_refine_rounds: int) -> Callable[[GraphState], str]:
    def route(state: GraphState) -> str:
        ctx = state["ctx"]
        if ctx.thin_claims and ctx.refine_round < max_refine_rounds:
            return _REFINE
        return _CONTINUE

    return route


async def _revise_node(state: GraphState) -> dict[str, PipelineContext]:
    """Bump the revision counter; SYNTHESIZE reads the failed report off ``ctx``.

    Like ``_refine_node``: no stage row, no commit, no model call.
    """
    ctx = state["ctx"]
    return {"ctx": ctx.model_copy(update={"revise_round": ctx.revise_round + 1})}


def _revise_router(max_revise_rounds: int) -> Callable[[GraphState], str]:
    def route(state: GraphState) -> str:
        ctx = state["ctx"]
        report = ctx.validation
        if (
            report is not None
            and not report.passed
            and ctx.revise_round < max_revise_rounds
            and report.failures
            and all(failure.code in REVISABLE_FAILURES for failure in report.failures)
        ):
            return _REVISE
        return _CONTINUE

    return route
