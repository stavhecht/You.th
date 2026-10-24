"""Transaction boundaries and retry, against a real database.

A fake session cannot fail any of this. The whole subject is what survives a
rollback and what a *different* transaction can see, and a fake has one of
neither. So these run against Postgres and skip when it is unreachable, the
same way ``test_db_invariants.py`` does — **a skipped run verifies none of it.**

    docker compose up -d db
    python -m scripts.migrate
    pytest tests/unitTest/test_orchestrator_transactions.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.domain.contracts import ValidationFailure, ValidationReport
from app.domain.enums import RunStatus, SourceApi, StudyType
from app.domain.models import PipelineRun, Source
from app.llm.base import TokenUsage
from app.pipeline.orchestrator import PipelineOrchestrator, retry_delay
from app.pipeline.stages import PipelineContext, StageError, StageName

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url

_PMID_PREFIX = "9100"


@pytest.fixture
async def factory() -> Any:
    """A session factory over a live database, with per-test cleanup.

    Unlike the other DB-backed suites this one cannot roll back at the end:
    the code under test commits, deliberately, and a test that rolled those
    commits away would be asserting against a transaction the production path
    never has. So the rows are deleted by hand afterwards instead.
    """
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no database at {DATABASE_URL}")

    made = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield made
    finally:
        async with made() as cleanup:
            await cleanup.execute(
                sa.text("DELETE FROM sources WHERE pmid LIKE :p"),
                {"p": f"{_PMID_PREFIX}%"},
            )
            await cleanup.execute(
                sa.text("DELETE FROM pipeline_runs WHERE topic LIKE 'txntest:%'")
            )
            await cleanup.commit()
        await engine.dispose()


async def _new_run(factory: async_sessionmaker[AsyncSession], topic: str) -> str:
    async with factory() as session:
        run = PipelineRun(topic=topic, status=RunStatus.QUEUED, attempts=0)
        session.add(run)
        await session.commit()
        return run.id


async def _run_row(factory: async_sessionmaker[AsyncSession], run_id: str) -> Any:
    """Read the run in a *fresh* transaction — the point is cross-visibility."""
    async with factory() as session:
        return (
            await session.execute(
                sa.text(
                    "SELECT status, attempts, next_attempt_at, error "
                    "FROM pipeline_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
        ).one()


async def _source_visible(
    factory: async_sessionmaker[AsyncSession], pmid: str
) -> bool:
    async with factory() as session:
        return bool(
            await session.scalar(
                sa.text("SELECT count(*) FROM sources WHERE pmid = :p"), {"p": pmid}
            )
        )


# ---------------------------------------------------------------------------
# Test stages.
# ---------------------------------------------------------------------------


class _WritingStage:
    """Stands in for RetrieveStage: writes an expensive row into `sources`."""

    name = StageName.RETRIEVE

    def __init__(self, session: AsyncSession, pmid: str) -> None:
        self._session = session
        self._pmid = pmid

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        self._session.add(
            Source(
                pmid=self._pmid,
                title="A cached paper",
                abstract="The abstract we already paid to embed.",
                study_type=StudyType.RCT,
                source_api=SourceApi.PUBMED,
                created_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
            )
        )
        await self._session.flush()
        return ctx


class _FailingStage:
    """Stands in for SynthesizeStage blowing up after retrieval succeeded."""

    name = StageName.SYNTHESIZE

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        raise self._exc


class _WriteThenFailStage:
    """Writes, then raises. The partial write must not survive."""

    name = StageName.SYNTHESIZE

    def __init__(self, session: AsyncSession, pmid: str) -> None:
        self._session = session
        self._pmid = pmid

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        self._session.add(
            Source(
                pmid=self._pmid,
                title="Half-written",
                abstract="Written by a stage that then failed.",
                source_api=SourceApi.PUBMED,
                created_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
            )
        )
        await self._session.flush()
        raise StageError(StageName.SYNTHESIZE, "died after writing")


class _NoopStage:
    name = StageName.PERSIST

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        return ctx


class _ModelCallingStage:
    """Stands in for a stage that spends tokens, optionally then failing.

    Records usage *before* raising, the way ExtractStage and SynthesizeStage
    do: a provider that answered and then returned something unusable charges
    for it either way.
    """

    name = StageName.SYNTHESIZE

    def __init__(
        self, model: str, usage: TokenUsage, exc: BaseException | None = None
    ) -> None:
        self._model = model
        self._usage = usage
        self._exc = exc

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        ctx.record_usage(self.name, self._model, self._usage)
        if self._exc is not None:
            raise self._exc
        return ctx


# ---------------------------------------------------------------------------
# What survives a downstream failure.
# ---------------------------------------------------------------------------


async def test_an_earlier_stages_writes_survive_a_later_stages_failure(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reason this fix exists.

    Under one transaction per run, a synthesis failure discarded the `sources`
    rows and the embeddings RetrieveStage had just paid for — so the retry
    re-fetched and re-embedded everything, defeating the cache at exactly the
    moment it would have helped.

    Asserted from a *separate* session, because "still in our own transaction"
    and "committed" look identical from inside the one that wrote it.
    """
    run_id = await _new_run(factory, "txntest:survives")
    pmid = f"{_PMID_PREFIX}0001"

    async with factory() as session:
        orchestrator = PipelineOrchestrator(
            session,
            [_WritingStage(session, pmid), _FailingStage(RuntimeError("model down"))],
            factory,
        )
        await orchestrator.run(run_id, "txntest:survives")

    assert await _source_visible(factory, pmid) is True


async def test_a_failing_stages_own_writes_do_not_survive(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Committing per stage must not become committing a half-finished stage.

    Before this change the caller's blanket ``session.commit()`` ran even after
    a stage raised, so a stage that wrote rows and then failed had them
    committed anyway — PersistStage adding an article, flushing, and dying
    afterwards would leave the article behind with no run record pointing at it.
    """
    run_id = await _new_run(factory, "txntest:partial")
    kept, discarded = f"{_PMID_PREFIX}0002", f"{_PMID_PREFIX}0003"

    async with factory() as session:
        orchestrator = PipelineOrchestrator(
            session,
            [_WritingStage(session, kept), _WriteThenFailStage(session, discarded)],
            factory,
        )
        await orchestrator.run(run_id, "txntest:partial")

    assert await _source_visible(factory, kept) is True
    assert await _source_visible(factory, discarded) is False


async def test_no_transaction_is_held_open_across_a_stage_boundary(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A run used to sit ``idle in transaction`` for the length of every model
    call — measured directly, ``xact_age`` grew in lockstep with the call — and
    that transaction held the row locks RetrieveStage takes on matched sources.
    A second worker on an overlapping topic blocks behind them for minutes.

    Asserted on the session rather than on ``pg_stat_activity``, because
    reading the backend's state requires issuing a query, and issuing a query
    is what opens the transaction being tested for.
    """
    open_at_stage_start: list[bool] = []

    class _ObservingStage:
        name = StageName.RANK

        def __init__(self, session: AsyncSession) -> None:
            self._session = session

        async def run(self, ctx: PipelineContext) -> PipelineContext:
            open_at_stage_start.append(self._session.in_transaction())
            return ctx

    run_id = await _new_run(factory, "txntest:idle")
    async with factory() as session:
        orchestrator = PipelineOrchestrator(
            session,
            [
                _WritingStage(session, f"{_PMID_PREFIX}0004"),
                _ObservingStage(session),
                _NoopStage(),
            ],
            factory,
        )
        await orchestrator.run(run_id, "txntest:idle")

    # Entering the stage after one that wrote and committed: nothing held.
    assert open_at_stage_start == [False]


# ---------------------------------------------------------------------------
# Retry.
# ---------------------------------------------------------------------------


async def test_a_retryable_failure_requeues_instead_of_failing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """``StageError.retryable`` was declared from the start and read by nothing.

    The cases that set it — a throttled provider, a claim that went unsearched
    — are exactly the ones a second attempt fixes.
    """
    run_id = await _new_run(factory, "txntest:requeue")

    async with factory() as session:
        orchestrator = PipelineOrchestrator(
            session,
            [_FailingStage(StageError(StageName.RETRIEVE, "429", retryable=True))],
            factory,
            max_attempts=3,
        )
        await orchestrator.run(run_id, "txntest:requeue", attempt=1)

    status, _, next_attempt_at, error = await _run_row(factory, run_id)
    assert status == RunStatus.QUEUED
    assert error["retrying"] is True
    # Held back, so the next five-second poll does not spend attempt 2 on the
    # same 429 it just received.
    assert next_attempt_at > datetime.now(UTC)
    assert next_attempt_at <= datetime.now(UTC) + retry_delay(1)


async def test_the_last_attempt_fails_for_good(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _new_run(factory, "txntest:exhausted")

    async with factory() as session:
        orchestrator = PipelineOrchestrator(
            session,
            [_FailingStage(StageError(StageName.RETRIEVE, "429", retryable=True))],
            factory,
            max_attempts=3,
        )
        await orchestrator.run(run_id, "txntest:exhausted", attempt=3)

    status, _, next_attempt_at, error = await _run_row(factory, run_id)
    assert status == RunStatus.FAILED
    # Distinguishes "gave up after retrying" from "never worth retrying".
    assert error["attempts_exhausted"] is True
    assert next_attempt_at is None


async def test_a_non_retryable_failure_is_never_requeued(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A malformed draft does not get better by being generated again at the
    same temperature. Only ``retryable`` failures spend the budget."""
    run_id = await _new_run(factory, "txntest:permanent")

    async with factory() as session:
        orchestrator = PipelineOrchestrator(
            session,
            [_FailingStage(StageError(StageName.SYNTHESIZE, "bad draft"))],
            factory,
            max_attempts=3,
        )
        await orchestrator.run(run_id, "txntest:permanent", attempt=1)

    status, _, _, error = await _run_row(factory, run_id)
    assert status == RunStatus.FAILED
    assert error["attempts_exhausted"] is False


async def test_an_unexpected_exception_is_never_requeued(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only a stage that declares itself retryable gets a retry. An escaped
    TypeError is a bug, and retrying a bug is just three of it."""
    run_id = await _new_run(factory, "txntest:crash")

    async with factory() as session:
        orchestrator = PipelineOrchestrator(
            session, [_FailingStage(TypeError("boom"))], factory, max_attempts=3
        )
        await orchestrator.run(run_id, "txntest:crash", attempt=1)

    status, _, _, _ = await _run_row(factory, run_id)
    assert status == RunStatus.FAILED


async def test_stage_rows_are_kept_per_attempt_not_overwritten(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Diagnosing a run that succeeded on its third try means seeing the first
    two. The old UNIQUE (run_id, ordinal) made a retry an integrity error."""
    run_id = await _new_run(factory, "txntest:stagerows")

    for attempt in (1, 2):
        async with factory() as session:
            orchestrator = PipelineOrchestrator(
                session,
                [_FailingStage(StageError(StageName.RETRIEVE, "429", retryable=True))],
                factory,
                max_attempts=5,
            )
            await orchestrator.run(run_id, "txntest:stagerows", attempt=attempt)

    async with factory() as session:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT attempt, stage, status FROM pipeline_stage_runs "
                    "WHERE run_id = :id ORDER BY attempt"
                ),
                {"id": run_id},
            )
        ).all()

    assert [(row.attempt, row.stage) for row in rows] == [
        (1, "synthesize"),
        (2, "synthesize"),
    ]
    assert {row.status for row in rows} == {RunStatus.FAILED}


# ---------------------------------------------------------------------------
# The token ledger.
#
# A fake session cannot check any of this either: the point is that the ledger
# survives the rollback that discards a failed stage's writes, and that the run
# rollup accumulates across separate transactions.
# ---------------------------------------------------------------------------


async def _stage_ledger(
    factory: async_sessionmaker[AsyncSession], run_id: str
) -> Any:
    async with factory() as session:
        return (
            await session.execute(
                sa.text(
                    "SELECT stage, attempt, model, input_tokens, output_tokens, "
                    "cache_read_tokens, cache_write_tokens FROM pipeline_stage_runs "
                    "WHERE run_id = :id ORDER BY attempt, ordinal"
                ),
                {"id": run_id},
            )
        ).all()


async def _run_tokens(factory: async_sessionmaker[AsyncSession], run_id: str) -> Any:
    async with factory() as session:
        return (
            await session.execute(
                sa.text(
                    "SELECT input_tokens, output_tokens, cache_read_tokens, "
                    "cache_write_tokens FROM pipeline_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
        ).one()


async def test_a_successful_stage_records_its_model_and_tokens(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _new_run(factory, "txntest:ledger")
    usage = TokenUsage(
        input_tokens=10_336, output_tokens=423, cache_read_tokens=1_285
    )

    async with factory() as session:
        await PipelineOrchestrator(
            session,
            [_ModelCallingStage("anthropic/claude-sonnet-5", usage), _NoopStage()],
            factory,
        ).run(run_id, "txntest:ledger")

    row = (await _stage_ledger(factory, run_id))[0]
    assert row.model == "anthropic/claude-sonnet-5"
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (
        10_336,
        423,
        1_285,
    )


async def test_a_stage_that_calls_no_model_leaves_the_model_null(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """NULL, not "". The column is a join key into the price table, and an
    empty string is a lookup miss that reads as an unpriced model — the one
    thing the table refuses to report as free."""
    run_id = await _new_run(factory, "txntest:nomodel")

    async with factory() as session:
        await PipelineOrchestrator(session, [_NoopStage()], factory).run(
            run_id, "txntest:nomodel"
        )

    row = (await _stage_ledger(factory, run_id))[0]
    assert row.model is None
    assert row.input_tokens == 0


async def test_tokens_burned_by_a_failing_stage_survive_the_rollback(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The charge is the provider's, not ours, so the rollback must not undo it.

    Truncation is the case that matters: it spends the whole 8K synthesis
    budget and produces nothing. A run recorded at zero cost here would make
    the pipeline look cheapest at the moment it is burning the most.
    """
    run_id = await _new_run(factory, "txntest:burned")

    async with factory() as session:
        await PipelineOrchestrator(
            session,
            [
                _ModelCallingStage(
                    "anthropic/claude-sonnet-5",
                    TokenUsage(input_tokens=10_336, output_tokens=8_000),
                    StageError(StageName.SYNTHESIZE, "hit max_tokens"),
                )
            ],
            factory,
        ).run(run_id, "txntest:burned")

    row = (await _stage_ledger(factory, run_id))[0]
    assert row.output_tokens == 8_000
    assert row.model == "anthropic/claude-sonnet-5"

    spent = await _run_tokens(factory, run_id)
    assert spent.output_tokens == 8_000


async def test_the_run_rollup_accumulates_across_attempts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A retried run spent both attempts.

    The context is rebuilt per attempt, so assigning the rollup rather than
    incrementing it would overwrite the first attempt's tokens with the
    second's — reporting the cheaper attempt as the run's whole cost.
    """
    run_id = await _new_run(factory, "txntest:rollup")
    usage = TokenUsage(input_tokens=1_000, output_tokens=100)

    for attempt in (1, 2):
        async with factory() as session:
            await PipelineOrchestrator(
                session,
                [
                    _ModelCallingStage(
                        "anthropic/claude-sonnet-5",
                        usage,
                        StageError(StageName.SYNTHESIZE, "429", retryable=True),
                    )
                ],
                factory,
                max_attempts=5,
            ).run(run_id, "txntest:rollup", attempt=attempt)

    spent = await _run_tokens(factory, run_id)
    assert (spent.input_tokens, spent.output_tokens) == (2_000, 200)

    # Per-attempt rows stay separable, which is what makes the total explicable.
    assert [(r.attempt, r.output_tokens) for r in await _stage_ledger(factory, run_id)] == [
        (1, 100),
        (2, 100),
    ]


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [
        (1, timedelta(seconds=60)),
        (2, timedelta(minutes=5)),
        # Clamped, so raising PIPELINE_MAX_ATTEMPTS cannot walk off the end.
        (3, timedelta(minutes=5)),
        (99, timedelta(minutes=5)),
    ],
)
def test_retry_delay_grows_then_holds(attempt: int, expected: timedelta) -> None:
    assert retry_delay(attempt) == expected


# ---------------------------------------------------------------------------
# The refinement loop re-runs a stage within one attempt.
# ---------------------------------------------------------------------------


class _RetrieveOnce:
    """A RETRIEVE that records how many times it ran."""

    name = StageName.RETRIEVE

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        self.runs += 1
        return ctx


class _RankAskingForOneMorePass:
    name = StageName.RANK

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        thin = ["a claim"] if ctx.refine_round < 1 else []
        return ctx.model_copy(update={"thin_claims": thin})


async def test_a_refinement_pass_does_not_collide_with_its_own_stage_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """``pipeline_stage_runs`` is UNIQUE on (run_id, attempt, ordinal).

    A second pass through RETRIEVE on one attempt therefore cannot reuse
    RETRIEVE's list index. When it did, the IntegrityError escaped the
    orchestrator entirely and crashed the worker's poll loop, outside any
    stage's failure handling — so the run neither failed nor completed.
    """
    run_id = await _new_run(factory, "txntest:refine")
    retrieve = _RetrieveOnce()

    async with factory() as session:
        await PipelineOrchestrator(
            session,
            [retrieve, _RankAskingForOneMorePass(), _NoopStage()],  # type: ignore[list-item]
            factory,
        ).run(run_id, "txntest:refine")

    assert retrieve.runs == 2, "the loop actually took a second pass"

    row = await _run_row(factory, run_id)
    assert row.status == RunStatus.SUCCEEDED

    rows = await _stage_ledger(factory, run_id)
    stages = [r.stage for r in rows]
    assert stages.count("retrieve") == 2, "both passes are recorded, not overwritten"


async def test_an_ordinary_run_still_records_its_list_position(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The offset is by refinement round, so a run that never loops writes
    exactly the ordinals it always did."""
    run_id = await _new_run(factory, "txntest:ordinals")

    async with factory() as session:
        await PipelineOrchestrator(
            session,
            [_RetrieveOnce(), _NoopStage()],  # type: ignore[list-item]
            factory,
        ).run(run_id, "txntest:ordinals")

    async with factory() as session:
        ordinals = (
            await session.execute(
                sa.text(
                    "SELECT ordinal FROM pipeline_stage_runs "
                    "WHERE run_id = :id ORDER BY ordinal"
                ),
                {"id": run_id},
            )
        ).scalars().all()

    assert list(ordinals) == [0, 1]


class _SynthesizeCounting:
    name = StageName.SYNTHESIZE

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        self.runs += 1
        return ctx


class _ValidateFailingOnce:
    """Fails the first draft with a fixable failure, passes the rewrite."""

    name = StageName.VALIDATE

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        passed = ctx.revise_round > 0
        report = ValidationReport(
            passed=passed,
            citations_total=1,
            citations_resolved=1,
            best_evidence_grade=StudyType.RCT,
            failures=[]
            if passed
            else [ValidationFailure(code="uncited_beat", message="beat 2 cites nothing")],
        )
        return ctx.model_copy(update={"validation": report})


async def test_a_revision_pass_does_not_collide_with_its_own_stage_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The VALIDATE -> SYNTHESIZE loop re-runs stages on one attempt, exactly as
    refinement does, so it needs the same ordinal offset — or the second
    SYNTHESIZE row is the IntegrityError that crashes the poll loop."""
    run_id = await _new_run(factory, "txntest:revise")
    synthesize = _SynthesizeCounting()

    async with factory() as session:
        await PipelineOrchestrator(
            session,
            [synthesize, _ValidateFailingOnce(), _NoopStage()],  # type: ignore[list-item]
            factory,
        ).run(run_id, "txntest:revise")

    assert synthesize.runs == 2, "the draft was rewritten once"
    row = await _run_row(factory, run_id)
    assert row.status == RunStatus.SUCCEEDED

    async with factory() as session:
        ordinals = (
            await session.execute(
                sa.text(
                    "SELECT stage, ordinal FROM pipeline_stage_runs "
                    "WHERE run_id = :id ORDER BY ordinal"
                ),
                {"id": run_id},
            )
        ).all()
    assert [(stage, ordinal) for stage, ordinal in ordinals] == [
        ("synthesize", 0),
        ("validate", 1),
        ("synthesize", 3),
        ("validate", 4),
        ("persist", 5),
    ]
