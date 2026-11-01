"""What the pipeline does when things go wrong, end to end through the worker.

Each case drives the real worker and orchestrator against the test database and
checks three things: what the run record says, what was (and was not) written,
and that nothing unreviewed or invalid ever reaches the public site.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from app.config import get_settings
from app.llm.base import LLMError, TokenUsage
from tests.e2e.support import FakeExtraction, FakeProvider, ScriptedWriter, paper


def _queue(client: Any, console: dict[str, str], topic: str = "Magnesium for sleep") -> str:
    response = client.post("/api/console/pipeline/runs", json={"topic": topic}, headers=console)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def _run(client: Any, console: dict[str, str], run_id: str) -> dict[str, Any]:
    response = client.get(f"/api/console/pipeline/runs/{run_id}", headers=console)
    assert response.status_code == 200, response.text
    return response.json()


def _nothing_public(client: Any) -> None:
    assert client.get("/api/feed").json()["items"] == []
    assert client.get("/api/feed/facets").json()["total"] == 0


async def _article_count(factory: Any) -> int:
    async with factory() as db:
        return int(await db.scalar(sa.text("SELECT count(*) FROM articles")))


async def _make_due(factory: Any, run_id: str) -> None:
    """Skip the retry backoff, as the clock would."""
    async with factory() as db:
        await db.execute(
            sa.text("UPDATE pipeline_runs SET next_attempt_at = NULL WHERE id = :id"),
            {"id": run_id},
        )
        await db.commit()


# --- the draft is wrong ------------------------------------------------------


async def test_a_hallucinated_citation_is_stored_as_failed_and_never_reaches_review(
    client: Any, console: dict[str, str], run_worker: Any
) -> None:
    """Invariant #2: a draft citing a source it was never shown is revised once,
    and when the revision cites it again it is stored ``validation_failed`` —
    out of the review queue, unpublishable, invisible."""
    writer = ScriptedWriter(invent_handle=True)
    run_id = _queue(client, console)
    await run_worker(writer=writer)

    run = _run(client, console, run_id)
    assert run["status"] == "succeeded"  # the run worked; the draft did not
    assert writer.calls == 2  # first draft + exactly one revision

    pending = client.get(
        "/api/console/articles", params={"status": "pending_review"}, headers=console
    ).json()["items"]
    assert pending == []
    failed = client.get(
        "/api/console/articles", params={"status": "validation_failed"}, headers=console
    ).json()["items"]
    assert [a["id"] for a in failed] == [run["articleId"]]

    detail = client.get(f"/api/console/articles/{run['articleId']}", headers=console).json()
    codes = {f["code"] for f in detail["validationReport"]["failures"]}
    assert "hallucinated_handle" in codes

    approve = client.post(f"/api/console/articles/{run['articleId']}/approve", headers=console)
    assert approve.status_code == 409
    assert client.get(f"/api/articles/{detail['slug']}").status_code == 404
    _nothing_public(client)


async def test_unusable_papers_yield_a_no_evidence_draft_without_calling_the_model(
    client: Any, console: dict[str, str], run_worker: Any
) -> None:
    """Every candidate filtered out (abstracts under RANK's floor) reaches
    SYNTHESIZE as zero sources: the draft is assembled deterministically and the
    model is never handed a product name with nothing to ground it."""
    short = [
        p.model_copy(update={"abstract": "Too short to rank."})
        for p in (paper(n) for n in range(1, 4))
    ]
    writer = ScriptedWriter()
    run_id = _queue(client, console)
    await run_worker(provider=FakeProvider(short), writer=writer)

    run = _run(client, console, run_id)
    assert run["status"] == "succeeded"
    assert writer.calls == 0
    detail = client.get(f"/api/console/articles/{run['articleId']}", headers=console).json()
    assert detail["verdict"] == "no_evidence"
    assert detail["sources"] == []
    assert detail["status"] == "pending_review"  # still a human's call to publish


# --- the outside world fails -------------------------------------------------


async def test_a_search_outage_requeues_the_run_and_writes_nothing(
    client: Any, console: dict[str, str], run_worker: Any, factory: Any
) -> None:
    """A search that could not run must never look like one that found nothing:
    no ``no_evidence`` article, the run requeued with a backoff instead."""
    provider = FakeProvider([paper(1)], down=True)
    run_id = _queue(client, console)
    await run_worker(provider=provider)

    run = _run(client, console, run_id)
    assert run["status"] == "queued"
    assert run["nextAttemptAt"] is not None
    assert run["attempts"] == 1
    assert run["error"]["retrying"] is True
    assert await _article_count(factory) == 0

    # The backoff holds: an immediate second pass does not re-claim it.
    await run_worker(provider=provider)
    assert _run(client, console, run_id)["attempts"] == 1


async def test_a_run_that_keeps_failing_stops_at_the_attempt_budget(
    client: Any, console: dict[str, str], run_worker: Any, factory: Any
) -> None:
    provider = FakeProvider([paper(1)], down=True)
    budget = get_settings().pipeline_max_attempts
    run_id = _queue(client, console)
    for _ in range(budget + 1):  # one pass more than the budget allows
        await run_worker(provider=provider)
        await _make_due(factory, run_id)

    run = _run(client, console, run_id)
    assert run["status"] == "failed"
    assert run["attempts"] == budget
    assert await _article_count(factory) == 0


async def test_a_model_outage_is_retried_and_its_tokens_are_still_counted(
    client: Any, console: dict[str, str], run_worker: Any
) -> None:
    """The provider charged for the failed call; a zero here would make the
    pipeline look cheapest exactly when it burns the most."""
    outage = LLMError(
        "llama-server timed out",
        usage=TokenUsage(input_tokens=5000),
        model="test/synth",
        retryable=True,
    )
    run_id = _queue(client, console)
    await run_worker(writer=ScriptedWriter(fail=outage))

    run = _run(client, console, run_id)
    assert run["status"] == "queued"
    assert run["inputTokens"] >= 5000


async def test_a_model_that_breaks_the_contract_twice_fails_for_good(
    client: Any, console: dict[str, str], run_worker: Any
) -> None:
    """Not retryable: the retry would send the identical request."""
    broken = LLMError("output broke the schema twice", model="test/synth", retryable=False)
    writer = ScriptedWriter(fail=broken)
    run_id = _queue(client, console)
    await run_worker(writer=writer)

    run = _run(client, console, run_id)
    assert run["status"] == "failed"
    assert writer.calls == 2  # the one fresh draft, then stop
    assert run["articleId"] is None


async def test_a_topic_naming_no_substance_fails_before_any_search(
    client: Any, console: dict[str, str], run_worker: Any
) -> None:
    """``UnanchoredQuery``: searching the outcome alone found 50 papers about
    something else and published a confident verdict on them. It must fail
    permanently, before a single provider request."""
    provider = FakeProvider([paper(1)])
    extraction = FakeExtraction(product="unspecified", ingredients=())
    run_id = _queue(client, console, topic="Is it good for you?")
    await run_worker(provider=provider, extraction=extraction)

    run = _run(client, console, run_id)
    assert run["status"] == "failed"
    assert run["attempts"] == 1
    assert provider.queries == []
    _nothing_public(client)
