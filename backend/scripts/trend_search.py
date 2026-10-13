"""Run the Trend Search Agent live and print what it found. Dry by default.

    python -m scripts.trend_search                          # every enabled source
    python -m scripts.trend_search --category sleep_recovery --source rss --source news
    python -m scripts.trend_search --query "mouth taping" --json result.json
    python -m scripts.trend_search --apply                  # queue a real research run

**The dry run makes the network calls and writes nothing.** It runs the trend
half of a research run — collect, normalise, cluster, history, enrich, score,
select — against the live sources, with in-memory feed state and topic
history, and stops before the evidence check: no PubMed, no Europe PMC, no
database writes. Novelty still reads what was already written (read-only)
when the database is reachable. Use it to see what a source returns today,
whether a feed still answers, and how a change to scoring or clustering moves
the ranking — without spending a research run or touching the desk.

``--apply`` does not run anything here. It queues a research run exactly as
the desk's button does (``service.start_research_run``), and the worker picks
it up, evidence check included.

Like the console and n8n, this is a thin wrapper: the agent lives in
``app/research``, and this file is argparse and printing around it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.domain.enums import ResearchCandidateStatus, ResearchRunMode
from app.logging_setup import configure_logging
from app.research import service
from app.research.contracts import (
    SOURCE_NAMES,
    Category,
    PastTopic,
    ResearchConfig,
    ResearchParams,
    ResearchState,
)
from app.research.factory import build_research_deps
from app.research.graph import ResearchAgent
from app.research.trends.history import InMemoryTopicStore
from app.research.trends.sources.rss import InMemoryFeedStore
from app.retrieval.factory import build_http_client


def _params(args: argparse.Namespace) -> ResearchParams:
    return ResearchParams(
        categories=[Category(c) for c in args.category] or None,
        enabled_sources=args.source or None,
        queries=args.query or None,
        trend_window_days=args.window_days,
        target_article_count=args.topics,
        max_requests_per_source=args.max_requests,
    )


async def _history(since: datetime) -> list[PastTopic]:
    """What was already written or decided, read-only; empty without a database."""
    try:
        return await service.recent_history(get_session_factory(), since)
    except Exception as exc:  # no database is a fine way to run a dry run
        print(f"(novelty history unavailable: {exc.__class__.__name__})")
        return []


def _print(state: ResearchState) -> None:
    print(f"\nWindow {state.window_start:%Y-%m-%d %H:%M} -> {state.window_end:%Y-%m-%d %H:%M}")
    print("\nSources")
    for name, status in sorted(state.provider_status.items()):
        detail = f"  {status.detail}" if status.detail else ""
        print(
            f"  {name:14} {status.status:11} calls={status.calls:<3} failures="
            f"{status.failures:<3} retries={status.retries:<3} items={status.items}{detail}"
        )
    print("\nCounts")
    for key, value in state.counts.items():
        print(f"  {key}: {value}")
    if state.notes:
        print("\nNotes")
        for note in state.notes:
            print(f"  - {note}")
    ranked = sorted((c for c in state.candidates if c.rank), key=lambda c: c.rank or 0)
    print(f"\nRanked topics ({len(ranked)})")
    for candidate in ranked:
        scores, m = candidate.scores, candidate.metrics
        assert m is not None
        dims = ", ".join(f"{k} {v:.0f}" for k, v in scores.components.items())
        pool = " *" if candidate.in_evidence_pool else ""
        print(
            f"  {candidate.rank:>2}. {candidate.canonical_topic}{pool}  "
            f"[{scores.overall:.1f}; {dims}; coverage {scores.data_coverage:.0%}; "
            f"{candidate.trend_status.value if candidate.trend_status else '?'}; "
            f"{m.independent_publishers} outlets]"
        )
        for source in candidate.supporting[:3]:
            print(f"        {source.source_name}: {source.title[:80]}  {source.url}")
    discarded = [c for c in state.candidates if c.status is ResearchCandidateStatus.DISCARDED]
    if discarded:
        print(f"\nTurned away ({len(discarded)})")
        for candidate in discarded[:25]:
            print(f"  - {candidate.canonical_topic}: {candidate.discard_reason}")
    if state.failed:
        print(f"\nFAILED: {state.failed}")


async def _run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="queue a real research run")
    parser.add_argument(
        "--category", action="append", default=[], choices=[c.value for c in Category]
    )
    parser.add_argument("--source", action="append", default=[], choices=SOURCE_NAMES)
    parser.add_argument("--query", action="append", default=[], help="a topic to look into")
    parser.add_argument("--window-days", type=int)
    parser.add_argument("--topics", type=int, help="topics to shortlist")
    parser.add_argument("--max-requests", type=int, help="per-source request budget")
    parser.add_argument("--json", type=Path, help="write the trend result here")
    args = parser.parse_args()

    settings = get_settings()
    params = _params(args)
    if args.apply:
        async with get_session_factory()() as session:
            run = await service.start_research_run(
                session,
                mode=ResearchRunMode.MANUAL,
                params=params,
                requested_by=None,
                settings=settings,
            )
            await session.commit()
        print(f"queued research run {run.label} ({run.id}); the worker will run it")
        return 0

    config = ResearchConfig.resolve(settings, params)
    state = ResearchState(
        run_id=str(uuid.uuid4()),
        label="trend_search_dry_run",
        mode=ResearchRunMode.MANUAL,
        config=config,
        today=datetime.now(UTC).date(),
    )
    http = build_http_client(settings)
    try:
        trend, _ = build_research_deps(
            settings,
            http,
            config,
            history=_history,
            topics=InMemoryTopicStore(),
            feeds=InMemoryFeedStore(),
        )

        async def progress(state: ResearchState) -> None:
            print(f"[{datetime.now(UTC):%H:%M:%S}] {state.stage.value}", flush=True)

        agent = ResearchAgent(trend, evidence=None, checkpoint=progress)
        started = datetime.now(UTC)
        state = await agent.run(state)
        print(f"\nfinished in {(datetime.now(UTC) - started).total_seconds():.0f}s")
    finally:
        await http.aclose()
    _print(state)
    if args.json and state.trend_summary is not None:
        args.json.write_text(json.dumps(state.trend_summary, indent=2))
        print(f"\nwrote {args.json}")
    return 1 if state.failed else 0


async def main() -> int:
    """Disposes the engine inside the same event loop (see ``scan_trends``)."""
    configure_logging(logging.WARNING)
    try:
        return await _run()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
