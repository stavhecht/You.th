"""What every research-graph node shares: the state key, the wrapper, the routers.

Both halves of a research run — the trend search (``trends/graph.py``) and the
evidence check (``evidence.py``) — are lists of ``(name, stage, fn)`` steps.
``research/graph.py`` wraps each with ``node`` and wires them into one
LangGraph ``StateGraph``. The wrapper is where the run's guarantees live:

* the stage is written, and the run checkpointed, as a node **starts** and as
  it **ends** — some take minutes, and the desk shows the step a run is on;
* an exception is **recorded, not re-raised**: the run ends FAILED with the
  stage and the error on it, and the candidates found so far are kept;
* every node appends its duration and metrics to ``stage_log``.

Same shape and same reason as ``pipeline/graph.py``: the graph decides what
runs next, and the one state key is replaced wholesale by each node.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END

from app.research.contracts import ProviderStatus, ResearchStage, ResearchState

logger = logging.getLogger(__name__)


class GraphState(TypedDict):
    state: ResearchState


Checkpoint = Callable[[ResearchState], Awaitable[None]]
NodeFn = Callable[[ResearchState], Awaitable[dict[str, Any]]]
Step = tuple[str, ResearchStage, NodeFn]


def node(
    stage: ResearchStage,
    fn: NodeFn,
    *,
    checkpoint: Checkpoint | None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Any:
    """The LangGraph node for one step (``Any``, as ``pipeline/graph.py``'s is:
    LangGraph's node protocols do not accept a plain coroutine factory)."""

    async def run(graph_state: GraphState) -> GraphState:
        state = graph_state["state"]
        state.stage = stage
        if checkpoint is not None:
            await checkpoint(state)
        started = now()
        clock = time.monotonic()
        metrics: dict[str, Any] = {}
        try:
            metrics = await fn(state)
        except Exception as exc:
            logger.exception("research run %s failed at %s", state.label, stage)
            state.failed = f"{stage.value}: {type(exc).__name__}: {exc}"
        state.stage_log.append(
            {
                "stage": stage.value,
                "started_at": started.isoformat(),
                "duration_ms": int((time.monotonic() - clock) * 1000),
                "metrics": metrics,
                **({"failed": state.failed} if state.failed else {}),
            }
        )
        if state.failed is not None:
            state.stage = ResearchStage.FAILED
        if checkpoint is not None:
            await checkpoint(state)
        return {"state": state}

    return run


def unless_failed(following: str) -> Callable[[GraphState], str]:
    def route(graph_state: GraphState) -> str:
        return END if graph_state["state"].failed is not None else following

    return route


def settle(status: ProviderStatus, errors: list[str]) -> None:
    """ok / partial / failed from the call counts, with the last error kept."""
    if status.status in ("unavailable", "skipped") and not status.calls:
        return
    if status.calls and status.failures >= status.calls:
        status.status = "failed"
    elif status.failures:
        status.status = "partial"
    else:
        status.status = "ok"
    if errors:
        status.detail = errors[-1]
