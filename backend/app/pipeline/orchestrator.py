"""Runs the stages in order and records what happened.

The recording is the reason this exists rather than a bare `for` loop. Without
``pipeline_runs``, a draft that failed citation validation is indistinguishable
from a pipeline that never ran — invariant #2 would be enforced but invisible,
which in practice means unmaintained.

Locally this drives function calls. In production the same sequence becomes a
Step Functions state machine; because stages exchange a serialisable context,
that swap does not touch this file's logic, only how ``stage.run`` is dispatched.
"""

from __future__ import annotations

import logging
import traceback
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.enums import RunStatus
from app.domain.models import PipelineRun, PipelineStageRun
from app.imagery.factory import build_image_client
from app.llm.base import TokenUsage
from app.llm.embeddings.factory import build_embedding_provider
from app.llm.factory import build_appraisal_client, build_generative_clients
from app.pipeline.graph import (
    MAX_REFINE_ROUNDS,
    build_pipeline_graph,
    recursion_limit,
)
from app.pipeline.stages import (
    PipelineContext,
    Stage,
    StageError,
    StageName,
    StageUsage,
)
from app.pipeline.steps.appraise import AppraiseStage
from app.pipeline.steps.extract import ExtractStage
from app.pipeline.steps.full_text import FullTextStage
from app.pipeline.steps.illustrate import IllustrateStage, IllustrationConfig
from app.pipeline.steps.persist import PersistStage, ValidateStage
from app.pipeline.steps.rank import RankStage
from app.pipeline.steps.retrieve import RetrieveStage
from app.pipeline.steps.synthesize import SynthesizeStage
from app.retrieval.cache import SourceCache
from app.retrieval.factory import (
    build_doi_client,
    build_europe_pmc_client,
    build_http_client,
    build_providers,
    build_pubmed_client,
)
from app.retrieval.full_text import EuropePMCFullText
from app.retrieval.identifiers import DoiRegistry, EuropePMCIdentifiers
from app.retrieval.query_builder import TemplateQueryStrategy
from app.retrieval.rerank import RerankConfig, SemanticReranker
from app.retrieval.retractions import PubMedRetractionSource

logger = logging.getLogger(__name__)

#: Wait before a requeued run may be claimed again, indexed by the attempt that
#: just failed. The dominant retryable cause is a provider throttling us, and
#: the poll interval is five seconds — requeueing without a delay just spends
#: the next attempt on the same 429. Ends at the last entry for any further
#: attempts, so raising ``PIPELINE_MAX_ATTEMPTS`` cannot walk off the end.
RETRY_DELAYS = (timedelta(seconds=60), timedelta(minutes=5))


def retry_delay(attempt: int) -> timedelta:
    return RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)) - 1]


class _RunAborted(Exception):
    """Carries the failed run's context out through the graph.

    The failure has already been recorded and rolled back by the time this is
    raised; it exists only to stop the graph and return the context, which
    ``run`` hands back to the worker exactly as the old loop's early ``return``
    did. It carries the context rather than letting the caller re-read it
    because the stage mutated ``usage_by_stage`` on its way out, and that object
    is the only record of what a failed model call cost.
    """

    def __init__(self, ctx: PipelineContext) -> None:
        super().__init__("pipeline run aborted")
        self.ctx = ctx


class PipelineOrchestrator:
    """Drives the stages and writes the run record.

    **The orchestrator owns every transaction boundary.** A stage never commits
    and the caller never commits; each successful stage is committed here, and
    a failing one is rolled back here. Two session factories are in play and
    the split is deliberate:

    * ``session`` — the pipeline session, threaded into the stages that touch
      the database. Committed per stage (see ``run``).
    * ``bookkeeping_factory`` — a *separate* factory, so stage records commit
      independently of the pipeline transaction. If the article write rolls
      back, the record of what happened must survive, or the failures you most
      need to debug are exactly the ones that erase their own evidence.
    """

    def __init__(
        self,
        session: AsyncSession,
        stages: list[Stage],
        bookkeeping_factory: async_sessionmaker[AsyncSession],
        max_attempts: int = 3,
    ) -> None:
        self._session = session
        self._stages = stages
        self._bookkeeping = bookkeeping_factory
        self._max_attempts = max_attempts
        # Compiled once per orchestrator, not per run: the shape depends only on
        # the stage list, and building it per attempt would rebuild an identical
        # graph on every retry.
        self._graph = build_pipeline_graph(stages, self._run_stage)
        self._recursion_limit = recursion_limit(stages, MAX_REFINE_ROUNDS)

    async def run(
        self,
        run_id: str,
        topic: str,
        blurb: str | None = None,
        attempt: int = 1,
    ) -> PipelineContext:
        """Execute every stage in order, recording and committing each.

        Returns the final context whether the run succeeded or failed; callers
        inspect ``ctx.article_id`` and ``ctx.validation``.

        On failure, later stages stay ``queued`` so the console shows exactly
        where it stopped.

        **Why the commit is per stage.** A single transaction around the whole
        run means a synthesis failure discards the ``sources`` rows and the
        embeddings that RetrieveStage just paid for — defeating the cache at
        precisely the moment it would help, since the retry re-fetches and
        re-embeds everything. It also leaves the connection ``idle in
        transaction`` for the length of every model call, holding the row locks
        RetrieveStage took on matched sources; a second worker touching an
        overlapping topic blocks behind them for minutes. Committing per stage
        is also what the Step Functions target does anyway — one Lambda, one
        connection, one transaction per state — so this narrows the gap between
        local and deployed rather than widening it.

        Safe because ``sources`` is an idempotent cache with no invariant
        attached, and the only stage that writes article data is PersistStage,
        which is one stage and therefore still atomic in itself.

        **The last stage is the exception.** Its write commits together with
        the run's completion row in ``_finish_run``. Committing them separately
        opens a window where an article exists but its run still reads
        ``running``; a worker killed there gets its run requeued as stale and
        writes a *second* article for the same topic. Persist is last, so there
        is no long call after it that a held transaction would obstruct — the
        atomicity is free.
        """
        ctx = PipelineContext(run_id=run_id, topic=topic, blurb=blurb)
        await self._set_run_status(run_id, RunStatus.RUNNING, started=True)

        # The per-stage bookkeeping lives in ``_run_stage``, which the graph
        # calls for every node. Failure unwinds through ``_RunAborted`` rather
        # than through a return value, because a raising node produces no state
        # for LangGraph to hand back and the context it mutated on the way out
        # is the one carrying the failed call's tokens.
        try:
            final = await self._graph.ainvoke(
                {"ctx": ctx, "attempt": attempt},
                config={"recursion_limit": self._recursion_limit},
            )
        except _RunAborted as aborted:
            return aborted.ctx

        ctx = final["ctx"]
        await self._finish_run(run_id, ctx)
        return ctx

    async def _run_stage(
        self, ordinal: int, stage: Stage, ctx: PipelineContext, attempt: int
    ) -> PipelineContext:
        """One stage, with the row, the commit and the failure handling around it.

        This is verbatim the body of the loop this class used to be, and keeping
        it verbatim is the point: the commit boundary, the rollback-before-record
        order in ``_abandon``, and the last stage's commit being deferred into
        ``_finish_run`` are all guarantees with tests attached, and none of them
        is expressible as a LangGraph feature. The graph decides what runs next
        and nothing else.

        **``ordinal`` is the execution slot, not the list position, and the two
        stopped being the same thing when the refinement loop arrived.**
        ``pipeline_stage_runs`` carries a UNIQUE constraint on
        ``(run_id, attempt, ordinal)``, so a second pass through RETRIEVE on one
        attempt cannot reuse RETRIEVE's list index: it is an
        ``IntegrityError`` that escapes the orchestrator entirely and crashes the
        worker's poll loop, outside any stage's failure handling.

        Offsetting by the round keeps every row unique without a migration, and
        keeps the numbers monotonic in execution order — round 0 runs 0..8, and a
        run that refines continues 10, 11, then 12..17 — which is what the console
        sorts on. On ``refine_round`` 0 this is exactly the list position, so an
        ordinary run records precisely the ordinals it always did.

        The VALIDATE -> SYNTHESIZE revision loop adds its round to the same
        offset. Refinement always happens before revision (RANK precedes
        VALIDATE), so the sum still only grows in execution order: a run that
        does both writes its second SYNTHESIZE at 5 + 2 * len(stages).

        The *commit* boundary still keys on the list position, because "is this
        the last stage" is a question about the pipeline's shape and not about
        how many times it has looped.
        """
        run_id = ctx.run_id
        stage_run_id = await self._record_stage_start(
            run_id,
            stage.name,
            ordinal + (ctx.refine_round + ctx.revise_round) * len(self._stages),
            attempt,
        )
        try:
            new_ctx = await stage.run(ctx)
        except StageError as exc:
            await self._abandon(
                stage_run_id, stage.name, exc, ctx, retryable=exc.retryable
            )
            await self._roll_up_usage(run_id, ctx)
            await self._handle_failure(
                run_id, stage.name, str(exc), attempt, retryable=exc.retryable
            )
            raise _RunAborted(ctx) from exc
        except Exception as exc:
            await self._abandon(stage_run_id, stage.name, exc, ctx)
            await self._roll_up_usage(run_id, ctx)
            await self._handle_failure(
                run_id,
                stage.name,
                f"{type(exc).__name__}: {exc}",
                attempt,
                retryable=False,
            )
            raise _RunAborted(ctx) from exc

        # Durable before it is reported succeeded, not after. The last stage is
        # the exception: its write commits with the run's completion row in
        # ``_finish_run``. Identified by position in the list, exactly as the
        # loop did.
        if ordinal < len(self._stages) - 1:
            await self._session.commit()
        await self._record_stage_end(
            stage_run_id,
            metrics=new_ctx.metrics.get(str(stage.name)),
            usage=new_ctx.usage_by_stage.get(str(stage.name)),
        )
        return new_ctx

    async def _abandon(
        self,
        stage_run_id: str,
        stage: StageName,
        exc: BaseException,
        ctx: PipelineContext,
        *,
        retryable: bool | None = None,
    ) -> None:
        """Discard the failed stage's partial writes, then record the failure.

        The rollback is not incidental. A stage that wrote rows and *then*
        raised used to have those rows committed by the caller's blanket
        ``session.commit()`` — PersistStage adding an article, flushing, and
        failing afterwards would leave the article behind with no run record
        pointing at it. Rolling back here means the failed stage leaves nothing,
        while every stage that already succeeded keeps what it committed.

        The token ledger is the exception to "leaves nothing", and has to be:
        the rollback discards *our* writes, not the provider's charge. A model
        call that answered and then truncated or failed validation is billed in
        full. ``ctx`` is readable here at all because ``record_usage`` mutates
        in place — the stage raised, so it returned no context to copy from.
        """
        await self._session.rollback()

        error: dict[str, object] = {
            "type": type(exc).__name__,
            "stage": str(stage),
            "message": str(exc),
        }
        if retryable is None:
            error["traceback"] = traceback.format_exc(limit=8)
        else:
            error["retryable"] = retryable

        await self._record_stage_end(
            stage_run_id,
            failed=True,
            error=error,
            usage=ctx.usage_by_stage.get(str(stage)),
        )

    async def _record_stage_start(
        self, run_id: str, stage: StageName, ordinal: int, attempt: int
    ) -> str:
        async with self._bookkeeping() as session:
            row = PipelineStageRun(
                run_id=run_id,
                stage=str(stage),
                ordinal=ordinal,
                attempt=attempt,
                status=RunStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
            session.add(row)
            await session.commit()
            return row.id

    async def _record_stage_end(
        self,
        stage_run_id: str,
        *,
        failed: bool = False,
        error: dict[str, object] | None = None,
        metrics: dict[str, object] | None = None,
        usage: StageUsage | None = None,
    ) -> None:
        spent = usage.usage if usage else TokenUsage()
        async with self._bookkeeping() as session:
            await session.execute(
                update(PipelineStageRun)
                .where(PipelineStageRun.id == stage_run_id)
                .values(
                    status=RunStatus.FAILED if failed else RunStatus.SUCCEEDED,
                    error=error,
                    metrics=metrics,
                    # NULL, not "", for a stage that called no model: the column
                    # is a join key into the price table and an empty string is
                    # a lookup miss that reads as an unpriced model.
                    model=(usage.model or None) if usage else None,
                    input_tokens=spent.input_tokens,
                    output_tokens=spent.output_tokens,
                    cache_read_tokens=spent.cache_read_tokens,
                    cache_write_tokens=spent.cache_write_tokens,
                    finished_at=datetime.now(UTC),
                )
            )
            await session.commit()

    async def _set_run_status(
        self, run_id: str, status: RunStatus, *, started: bool = False
    ) -> None:
        async with self._bookkeeping() as session:
            values: dict[str, object] = {"status": status}
            if started:
                values["started_at"] = datetime.now(UTC)
            await session.execute(
                update(PipelineRun).where(PipelineRun.id == run_id).values(**values)
            )
            await session.commit()

    @staticmethod
    def _usage_increments(ctx: PipelineContext) -> dict[str, object]:
        """The run rollup as SQL increments, not assignments.

        Additive because ``attempts`` is a real thing that happens: a run that
        burned a synthesis budget, requeued, and succeeded on its second try
        spent both. ``ctx`` is rebuilt per attempt, so assigning would silently
        overwrite the earlier attempt's tokens with the later one's — reporting
        the cheapest attempt as the run's cost, which is backwards. The
        per-stage rows keep the attempts separable; this is the lifetime total.
        """
        spent = ctx.usage
        return {
            "input_tokens": PipelineRun.input_tokens + spent.input_tokens,
            "output_tokens": PipelineRun.output_tokens + spent.output_tokens,
            "cache_read_tokens": PipelineRun.cache_read_tokens + spent.cache_read_tokens,
            "cache_write_tokens": (
                PipelineRun.cache_write_tokens + spent.cache_write_tokens
            ),
        }

    async def _roll_up_usage(self, run_id: str, ctx: PipelineContext) -> None:
        """Add this attempt's tokens to the run before it is failed or requeued.

        The success path folds the same increments into ``_finish_run``'s single
        atomic write instead of doing this. A failing run needs its own write:
        it never reaches that one, and it is the run most worth having a cost
        for — a model call that answered and then failed is billed in full.
        """
        async with self._bookkeeping() as session:
            await session.execute(
                update(PipelineRun)
                .where(PipelineRun.id == run_id)
                .values(**self._usage_increments(ctx))
            )
            await session.commit()

    async def _handle_failure(
        self,
        run_id: str,
        stage: StageName,
        message: str,
        attempt: int,
        *,
        retryable: bool,
    ) -> None:
        """Requeue a retryable failure with budget left; otherwise fail it.

        ``StageError.retryable`` has been declared since the beginning and read
        by nothing. The cases that set it are the transient ones — a throttled
        provider, a claim that went unsearched (see ``retrieval/throttle.py``)
        — and they are precisely the failures a second attempt fixes. This is
        also what turns the per-stage commit from a nicety into a saving: the
        retry finds the ``sources`` rows and their embeddings already cached
        and skips straight past the expensive half of the run.
        """
        if retryable and attempt < self._max_attempts:
            await self._requeue_run(run_id, stage, message, attempt)
        else:
            await self._fail_run(run_id, stage, message, attempt, retryable=retryable)

    async def _requeue_run(
        self, run_id: str, stage: StageName, message: str, attempt: int
    ) -> None:
        delay = retry_delay(attempt)
        logger.warning(
            "run %s failed retryably at stage %s (attempt %d/%d); requeued for %.0fs "
            "from now: %s",
            run_id,
            stage,
            attempt,
            self._max_attempts,
            delay.total_seconds(),
            message,
        )
        async with self._bookkeeping() as session:
            await session.execute(
                update(PipelineRun)
                .where(PipelineRun.id == run_id)
                .values(
                    status=RunStatus.QUEUED,
                    # Kept, so the console can show why the last attempt failed
                    # while the run sits waiting rather than looking merely slow.
                    error={
                        "stage": str(stage),
                        "message": message,
                        "attempt": attempt,
                        "retrying": True,
                    },
                    started_at=None,
                    next_attempt_at=datetime.now(UTC) + delay,
                )
            )
            await session.commit()

    async def _fail_run(
        self,
        run_id: str,
        stage: StageName,
        message: str,
        attempt: int,
        *,
        retryable: bool = False,
    ) -> None:
        exhausted = retryable and attempt >= self._max_attempts
        logger.error(
            "run %s failed at stage %s%s: %s",
            run_id,
            stage,
            f" after {attempt} attempts" if exhausted else "",
            message,
        )
        async with self._bookkeeping() as session:
            await session.execute(
                update(PipelineRun)
                .where(PipelineRun.id == run_id)
                .values(
                    status=RunStatus.FAILED,
                    error={
                        "stage": str(stage),
                        "message": message,
                        "attempt": attempt,
                        # Distinguishes "gave up after retrying" from "never
                        # worth retrying", which need different responses.
                        "attempts_exhausted": exhausted,
                    },
                    finished_at=datetime.now(UTC),
                )
            )
            await session.commit()

    async def _finish_run(self, run_id: str, ctx: PipelineContext) -> None:
        # This one write does NOT use the bookkeeping session, unlike every
        # other write in this class, and it carries the run's final commit.
        #
        # ``article_id`` is a foreign key into ``articles``, and the article was
        # written by the last stage into the *pipeline* session, still
        # uncommitted (see ``run``). A bookkeeping write commits immediately, so
        # it would point at a row no other transaction can see yet — a
        # ForeignKeyViolationError that aborts the run and takes the
        # uncommitted article down with it.
        #
        # Writing it here instead makes "succeeded, article=X" atomic with X,
        # which is both the honest semantics — if the article fails to commit,
        # the run did not succeed — and what keeps a killed worker from leaving
        # an article whose run still says ``running``, to be requeued as stale
        # and written a second time. The failure paths stay on the bookkeeping
        # session: they carry no such FK, and surviving a rolled-back pipeline
        # transaction is exactly their purpose (§3.3).
        await self._session.execute(
            update(PipelineRun)
            .where(PipelineRun.id == run_id)
            .values(
                status=RunStatus.SUCCEEDED,
                article_id=ctx.article_id,
                **self._usage_increments(ctx),
                finished_at=datetime.now(UTC),
            )
        )
        await self._session.commit()

        validation = ctx.validation
        logger.info(
            "run %s finished: article=%s validation=%s",
            run_id,
            ctx.article_id,
            "passed" if validation and validation.passed else "FAILED",
        )


def build_default_pipeline(session: AsyncSession, settings: Settings) -> list[Stage]:
    """Assemble the nine stages with their dependencies.

    The single place where concrete clients (Ollama or Claude, an embedding
    provider, PubMed) are bound to the Protocols the stages depend on — so
    tests substitute fakes by calling a different builder, not by patching
    imports. Which generative and embedding provider gets bound is config; see
    ``llm/factory.py`` and ``llm/embeddings/factory.py``.
    """
    embedder = build_embedding_provider(settings)
    http = build_http_client(settings)
    providers = build_providers(settings, http)
    europe_pmc = build_europe_pmc_client(settings, http)

    extraction_client, synthesis_client = build_generative_clients(settings)
    cache = SourceCache(session, embedder)
    reranker = SemanticReranker(session, embedder)

    rerank_config = RerankConfig(
        top_k=settings.retrieval_top_k,
        min_year=settings.retrieval_min_year,
    )

    return [
        ExtractStage(extraction_client),
        RetrieveStage(
            providers,
            TemplateQueryStrategy(min_year=settings.retrieval_min_year),
            cache,
            settings.retrieval_max_candidates_per_claim,
            resolver=EuropePMCIdentifiers(europe_pmc),
            retraction_screen=PubMedRetractionSource(
                build_pubmed_client(settings, http), settings.pubmed_api_key or None
            ),
            doi_registry=DoiRegistry(build_doi_client(settings, http)),
        ),
        RankStage(reranker, rerank_config),
        # After RANK so it reads only papers that already made the cut, and
        # after the refinement loop for the same reason: RANK's conditional
        # edge sends a thin run back to RETRIEVE before this ever runs.
        FullTextStage(EuropePMCFullText(europe_pmc), embedder, settings.full_text_max_papers),
        # After the refinement loop, so it labels the final ranked set once, and
        # before SYNTHESIZE, which copies the labels onto the prompt payload for
        # VALIDATE (without rendering them). The revision loop goes back to
        # SYNTHESIZE, not here, and reuses them. Constructed with no client
        # when switched off, for the same reason as IllustrateStage below.
        AppraiseStage(
            build_appraisal_client(settings) if settings.appraisal_enabled else None,
            two_call=settings.appraisal_mode == "two_call",
        ),
        SynthesizeStage(
            synthesis_client,
            relevance_filter=settings.synthesis_relevance_filter,
            reader_edit=settings.synthesis_reader_edit,
        ),
        # After SYNTHESIZE because it illustrates the draft, and before PERSIST
        # because PERSIST has to stay last: its write commits together with the
        # run's completion row (see ``run``), which is what stops a killed
        # worker leaving an article whose run still says ``running``.
        #
        # Constructed even when there is no client. A stage that vanished from
        # the list would renumber every later stage's ordinal, and its absence
        # would be a run with no picture and no row anywhere saying why —
        # ``build_image_client`` returning None becomes a recorded ``cause``
        # rather than a silence.
        IllustrateStage(
            build_image_client(settings),
            IllustrationConfig(
                max_bytes=settings.media_max_bytes,
                lead_size=(settings.image_lead_width, settings.image_lead_height),
                cover_size=(settings.image_cover_width, settings.image_cover_height),
                enabled=settings.image_provider.strip().lower()
                not in ("", "none", "off"),
                cover_from_lead=settings.image_cover_mode == "crop",
            ),
            session,
        ),
        ValidateStage(session),
        PersistStage(session),
    ]
