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

    async def no_collection(*, model_free_only: bool = False) -> None:
        return None

    async def claim() -> None:
        nonlocal claims
        claims += 1
        if claims >= 3:
            worker.request_shutdown()
        raise ConnectionRefusedError("database is restarting")

    worker._recover_stale_runs = sweep  # type: ignore[method-assign]
    worker._claim_research_run = no_research  # type: ignore[method-assign]
    worker._claim_collection_run = no_collection  # type: ignore[method-assign]
    worker._claim_next_run = claim  # type: ignore[method-assign]

    await asyncio.wait_for(worker.run_forever(), timeout=5)

    assert claims >= 3  # kept polling after the first failure
    assert sweeps >= 3  # including the boot sweep, now inside the loop


def _worker_with_queue(
    *, down: list[str | None], work: bool = True
) -> tuple[PipelineWorker, dict]:
    """A worker whose database is stubbed: `down` scripts each model probe."""
    seen: dict = {"probes": 0, "claims": 0, "research": 0, "collection": []}

    async def model_down() -> str | None:
        seen["probes"] += 1
        return down.pop(0) if down else None

    worker = PipelineWorker(poll_interval=0.01, model_wait=0.01, model_down=model_down)

    async def sweep() -> None:
        return None

    async def has_work() -> bool:
        return work

    async def claim_research() -> None:
        seen["research"] += 1

    async def claim_collection(*, model_free_only: bool = False) -> None:
        seen["collection"].append(model_free_only)

    async def claim() -> None:
        seen["claims"] += 1
        worker.request_shutdown()

    worker._recover_stale_runs = sweep  # type: ignore[method-assign]
    worker._has_work_needing_a_model = has_work  # type: ignore[method-assign]
    worker._claim_research_run = claim_research  # type: ignore[method-assign]
    worker._claim_collection_run = claim_collection  # type: ignore[method-assign]
    worker._claim_next_run = claim  # type: ignore[method-assign]
    return worker, seen


async def test_a_run_is_not_claimed_while_the_model_server_is_down() -> None:
    """Attempts are counted at claim, so not claiming is what spends none. On
    2026-11-05 a stopped llama-server burned three attempts each in six minutes."""
    worker, seen = _worker_with_queue(down=["refused", "refused", None])

    await asyncio.wait_for(worker.run_forever(), timeout=5)

    assert seen["probes"] == 3
    assert seen["claims"] == 1, "claimed only once the server answered"
    assert seen["research"] == 1, "research also waited: its triage needs the model"


async def test_work_needing_no_model_still_runs_during_an_outage() -> None:
    """The daily social run makes no model calls, and a lost day is lost coverage."""
    worker, seen = _worker_with_queue(down=["llama-server refused", None])

    await asyncio.wait_for(worker.run_forever(), timeout=5)

    assert seen["collection"] == [True, False]


async def test_an_idle_worker_never_calls_the_model() -> None:
    """A probe is a real completion; polling one all day would keep the weights paged in."""
    worker, seen = _worker_with_queue(down=[], work=False)

    await asyncio.wait_for(worker.run_forever(), timeout=5)

    assert seen["probes"] == 0


async def test_a_probe_that_breaks_does_not_stop_the_worker() -> None:
    worker, seen = _worker_with_queue(down=[])

    async def broken() -> str | None:
        raise RuntimeError("probe bug")

    worker._model_down = broken

    await asyncio.wait_for(worker.run_forever(), timeout=5)

    assert seen["claims"] == 1


async def test_a_research_or_collection_run_that_raises_does_not_end_the_worker(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """Both executors promise never to raise, and both can: recording a failure
    needs the database that just went away. The worker has no restart policy of
    its own, so an escape here stopped the queue until someone noticed."""
    from app.pipeline import runner

    worker = PipelineWorker(poll_interval=0.01, sweep_interval=1e9)
    ran: list[str] = []

    async def boom(*args: object, **kwargs: object) -> None:
        ran.append("ran")
        raise ConnectionRefusedError("database went away mid-run")

    async def sweep() -> None:
        return None

    research = [("run-1", "research_1")]
    collection = [("run-2", "social_daily_1", "social_daily")]

    async def claim_research() -> tuple[str, str] | None:
        return research.pop() if research else None

    async def claim_collection(*, model_free_only: bool = False) -> tuple[str, str, str] | None:
        return collection.pop() if collection else None

    async def claim() -> None:
        worker.request_shutdown()

    monkeypatch.setattr(runner, "execute_research_run", boom)
    monkeypatch.setattr(runner, "execute_collection_run", boom)
    worker._recover_stale_runs = sweep  # type: ignore[method-assign]
    worker._claim_research_run = claim_research  # type: ignore[method-assign]
    worker._claim_collection_run = claim_collection  # type: ignore[method-assign]
    worker._claim_next_run = claim  # type: ignore[method-assign]

    await asyncio.wait_for(worker.run_forever(), timeout=5)

    assert ran == ["ran", "ran"]  # both ran, both raised, and the loop went on
