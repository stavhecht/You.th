"""A database outage does not end the worker's poll loop.

The research claim was guarded and the article claim, the stale sweep and the
failure write were not, so one Postgres restart raised out of ``run_forever``
and the process exited with runs still queued. No database needed: every call
that would reach one is replaced.
"""

from __future__ import annotations

import asyncio

from app.pipeline.runner import PipelineWorker


async def test_the_loop_keeps_polling_through_a_database_outage() -> None:
    worker = PipelineWorker(poll_interval=0.01, sweep_interval=0.0)
    sweeps = claims = 0

    async def sweep() -> None:
        nonlocal sweeps
        sweeps += 1
        raise ConnectionRefusedError("database is restarting")

    async def no_research() -> None:
        return None

    async def claim() -> None:
        nonlocal claims
        claims += 1
        if claims >= 3:
            worker.request_shutdown()
        raise ConnectionRefusedError("database is restarting")

    worker._recover_stale_runs = sweep  # type: ignore[method-assign]
    worker._claim_research_run = no_research  # type: ignore[method-assign]
    worker._claim_next_run = claim  # type: ignore[method-assign]

    await asyncio.wait_for(worker.run_forever(), timeout=5)

    assert claims >= 3  # kept polling after the first failure
    assert sweeps >= 3  # including the boot sweep, now inside the loop
