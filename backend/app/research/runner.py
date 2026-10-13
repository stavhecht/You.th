"""Executes one claimed research run. Called from the pipeline worker's loop.

In the worker, not the API, for the same reason a generation run is: it takes
minutes (Trends and GDELT pacing dominate). In the *same* worker as
pipeline runs so the two never overlap — the worker's PubMed throttle is per
process and NCBI's ceiling is per IP, so a research run and a pipeline run
counting papers at the same time would each think it had the whole budget.
The cost is that a queued draft waits for a research run to finish.
"""

from __future__ import annotations

import functools
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.research import service
from app.research.contracts import ResearchStage
from app.research.factory import build_research_deps
from app.research.graph import ResearchAgent
from app.retrieval.factory import build_http_client

logger = logging.getLogger(__name__)


async def execute_research_run(
    run_id: str, settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Run the graph for one claimed run and record how it ended. Never raises."""
    try:
        state = await service.load_state(factory, run_id, today=datetime.now(UTC).date())
    except Exception as exc:
        # A stored config that no longer validates (a run queued before a
        # deploy changed ``ResearchConfig``) used to raise out of here and out
        # of the worker's loop, leaving the run ``running`` until the stale
        # sweep. Fail it now, with the reason.
        logger.exception("research run %s could not be loaded", run_id)
        await service.fail_unloadable(factory, run_id, f"{type(exc).__name__}: {exc}")
        return
    logger.info("research run %s started", state.label)
    http = build_http_client(settings)
    try:
        trend, evidence = build_research_deps(
            settings,
            http,
            state.config,
            history=functools.partial(service.recent_history, factory),
            topics=service.PostgresTopicStore(factory),
            feeds=service.PostgresFeedStore(factory),
        )
        agent = ResearchAgent(
            trend, evidence=evidence, checkpoint=functools.partial(service.checkpoint, factory)
        )
        state = await agent.run(state)
    except Exception as exc:
        # The graph records node failures itself; this is for anything outside
        # it (a provider that failed to build, a checkpoint write that raised).
        logger.exception("research run %s crashed", state.label)
        state.failed = f"{type(exc).__name__}: {exc}"
        state.stage = ResearchStage.FAILED
    finally:
        await http.aclose()

    try:
        await service.finish(factory, state)
    except Exception:
        logger.exception("could not record the end of research run %s", state.label)
        return
    try:
        pruned = await service.prune_undecided(
            factory, timedelta(days=settings.research_retention_days)
        )
        if pruned:
            logger.info("pruned %d undecided candidates from old research runs", pruned)
    except Exception:
        logger.exception("could not prune old research candidates")
    try:
        observations, topics = await service.prune_history(
            factory, timedelta(days=settings.research_history_retention_days)
        )
        if observations or topics:
            logger.info(
                "pruned %d trend observations and %d unreferenced topics", observations, topics
            )
    except Exception:
        logger.exception("could not prune old trend history")
    selected = sum(1 for c in state.candidates if c.status.value == "selected")
    logger.info(
        "research run %s %s: %d candidates, %d selected%s",
        state.label,
        "failed" if state.failed else "completed",
        len(state.candidates),
        selected,
        f" ({state.failed})" if state.failed else "",
    )
