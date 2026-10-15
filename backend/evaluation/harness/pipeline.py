"""Run one case through the production pipeline and record everything it did.

**What runs is production code.** The stages are the real ``ExtractStage`` …
``ValidateStage``; the providers come from ``retrieval/factory.py``; the order,
the RANK -> RETRIEVE refinement edge and the VALIDATE -> SYNTHESIZE revision
edge come from ``pipeline/graph.py::build_pipeline_graph`` — the same compiled
graph the orchestrator drives. The eval passes its own ``StageRunner`` into that
graph, exactly as the orchestrator passes ``_run_stage``, which is the seam the
graph module was built around.

**What is substituted, and why:**

* HTTP, models and embeddings go through recording wrappers
  (``harness/http.py``, ``harness/llm.py``, ``harness/embeddings.py``) that
  implement the same Protocols, so the stages cannot tell.
* ``SourceCache`` / ``SemanticReranker`` / the validation session are the
  in-memory versions in ``harness/store.py``: nothing is written anywhere the
  product reads.
* ``ILLUSTRATE`` and ``PERSIST`` are not run. PERSIST writes the article into
  the review queue; ILLUSTRATE is decorative, never fails a run, and is switched
  off in the root ``.env`` during the testing phase. The status PERSIST *would*
  write (``pending_review`` / ``validation_failed``) is derived from the
  validation report, by the same rule PERSIST uses.
* The orchestrator's database bookkeeping and the worker's requeue are not run.
  A retryable failure is recorded as such (``FAILED_RETRYABLE``) instead of
  being retried after a 60-second backoff; the bound on attempts is
  ``PIPELINE_MAX_ATTEMPTS`` and is reported, not exercised.

``check_stage_parity`` compares this stage list against what
``build_default_pipeline`` builds, so a stage added to production and not here
is reported at startup rather than silently unevaluated.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from langgraph.errors import GraphRecursionError

from app.config import Settings, get_settings
from app.domain.contracts import CandidatePaper
from app.domain.enums import SourceApi, Verdict
from app.evidence.grading import classify_study_type
from app.llm.base import AppraisalClient, ExtractionClient, SynthesisClient
from app.llm.embeddings.base import EmbeddingProvider
from app.llm.embeddings.factory import build_embedding_provider
from app.llm.factory import build_appraisal_client, build_generative_clients
from app.pipeline.graph import (
    MAX_REFINE_ROUNDS,
    MAX_REVISE_ROUNDS,
    REVISABLE_FAILURES,
    build_pipeline_graph,
    recursion_limit,
)
from app.pipeline.stages import PipelineContext, Stage, StageError, StageName
from app.pipeline.steps.appraise import AppraiseStage
from app.pipeline.steps.extract import ExtractStage
from app.pipeline.steps.full_text import FullTextStage
from app.pipeline.steps.persist import ValidateStage
from app.pipeline.steps.rank import RankStage
from app.pipeline.steps.retrieve import RetrieveStage
from app.pipeline.steps.synthesize import SynthesizeStage
from app.retrieval.base import ScholarlyProvider, SearchQuery
from app.retrieval.factory import (
    build_europe_pmc_client,
    build_http_client,
    build_providers,
    build_pubmed_client,
)
from app.retrieval.full_text import EuropePMCFullText
from app.retrieval.identifiers import EuropePMCIdentifiers
from app.retrieval.query_builder import TemplateQueryStrategy, UnanchoredQuery
from app.retrieval.rerank import RerankConfig
from app.retrieval.retractions import PubMedRetractionSource
from evaluation.config import EvalConfig
from evaluation.harness.cassette import Cassette
from evaluation.harness.embeddings import CachingEmbedder
from evaluation.harness.faults import FaultInjector
from evaluation.harness.http import EvalHttp
from evaluation.harness.llm import (
    EvalAppraisalClient,
    EvalExtractionClient,
    EvalSynthesisClient,
    HallucinatingSynthesisClient,
    ScriptedSynthesisClient,
)
from evaluation.harness.store import (
    MemoryReranker,
    MemorySession,
    MemorySourceCache,
    MemoryStore,
)
from evaluation.harness.trace import (
    CURRENT_STAGE,
    PaperRecord,
    RankedEntry,
    RetrievalRound,
    Trace,
    TraceRecorder,
)
from evaluation.schema import EvalCase, InjectedDocument, Outcome

logger = logging.getLogger(__name__)

#: Production stages the eval deliberately does not run (see module docstring).
NOT_EVALUATED = (StageName.ILLUSTRATE, StageName.PERSIST)


@dataclass
class EvalEnvironment:
    """Everything built once per evaluation run and shared by its cases."""

    settings: Settings
    config: EvalConfig
    mode: str
    cassette: Cassette
    http: httpx.AsyncClient
    extraction: ExtractionClient | None
    synthesis: SynthesisClient | None
    embedder: EmbeddingProvider | None
    #: None when ``APPRAISAL_ENABLED`` is off — APPRAISE then records
    #: ``cause: disabled``, exactly as it does in production.
    appraisal: AppraisalClient | None = None
    #: Component -> why it is unavailable. A case needing one is SKIPPED.
    unavailable: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @classmethod
    async def create(cls, config: EvalConfig, mode: str) -> EvalEnvironment:
        settings = get_settings()
        cassette = Cassette(config.path(config.run.cache_path))
        http = build_http_client(settings)
        unavailable: dict[str, str] = {}

        extraction: ExtractionClient | None = None
        synthesis: SynthesisClient | None = None
        appraisal: AppraisalClient | None = None
        try:
            extraction, synthesis = build_generative_clients(settings)
            if settings.appraisal_enabled:
                appraisal = build_appraisal_client(settings)
        except Exception as exc:
            unavailable["llm"] = f"generative clients could not be built: {exc}"

        embedder: EmbeddingProvider | None = None
        try:
            embedder = build_embedding_provider(settings)
        except Exception as exc:
            unavailable["embeddings"] = f"embedding provider could not be built: {exc}"

        env = cls(
            settings=settings,
            config=config,
            mode=mode,
            cassette=cassette,
            http=http,
            extraction=extraction,
            synthesis=synthesis,
            embedder=embedder,
            appraisal=appraisal,
            unavailable=unavailable,
        )
        if mode != "replay":
            await env._preflight()
        env.notes.extend(check_stage_parity(settings))
        return env

    async def _preflight(self) -> None:
        """Fail a dependency up front, so an outage is a skip and not a score.

        Without this, Ollama being down would make every case "fail at
        EXTRACT", and the report would measure the laptop rather than the system.
        """
        provider = self.settings.llm_provider.lower()
        if provider == "anthropic":
            return
        if provider == "llamacpp":
            await self._preflight_llamacpp()
        try:
            response = await self.http.get(
                f"{self.settings.ollama_base_url.rstrip('/')}/api/tags", timeout=5
            )
            names = {model["name"] for model in response.json().get("models", [])}
        except Exception as exc:
            reason = f"ollama is not reachable at {self.settings.ollama_base_url}: {exc}"
            if provider == "ollama":
                self.unavailable.setdefault("llm", reason)
            self.unavailable.setdefault("embeddings", reason)
            return

        def absent(models: list[str]) -> list[str]:
            return [m for m in models if m not in names and f"{m}:latest" not in names]

        if provider == "ollama":
            wanted = [
                self.settings.ollama_extraction_model,
                self.settings.ollama_synthesis_model,
            ]
            if self.settings.appraisal_enabled:
                wanted.append(self._ollama_appraisal_model())
            missing_llm = absent(wanted)
            if missing_llm:
                self.unavailable.setdefault(
                    "llm", f"ollama is missing model(s): {', '.join(missing_llm)}"
                )
        if self.settings.embedding_provider.lower() == "ollama":
            missing_emb = absent([self.settings.ollama_embedding_model])
            if missing_emb:
                self.unavailable.setdefault(
                    "embeddings", f"ollama is missing model(s): {', '.join(missing_emb)}"
                )

    async def _preflight_llamacpp(self) -> None:
        """A real two-token completion, not ``/health``.

        Measured 2026-10-15: after running out of GPU memory, llama-server
        answers ``/health`` with ok and every completion with ``Compute error``
        until restarted. A health check would score a run of failed cases.
        """
        url = f"{self.settings.llamacpp_base_url.rstrip('/')}/v1/chat/completions"
        try:
            response = await self.http.post(
                url,
                json={
                    "messages": [{"role": "user", "content": "Say OK."}],
                    "max_tokens": 4,
                    "reasoning_effort": "none",
                    "chat_template_kwargs": {"reasoning_effort": "none"},
                },
                timeout=120,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"].get("content") or ""
            if not content.strip():
                raise RuntimeError("empty answer (out of GPU memory? restart it)")
        except Exception as exc:
            self.unavailable.setdefault(
                "llm", f"llama-server at {url} cannot answer: {exc}"
            )

    def describe(self) -> dict[str, Any]:
        """The configuration a result was produced under, recorded on every trace."""
        s = self.settings
        provider = s.llm_provider.lower()
        # The effort is part of the name so that recordings made thinking and
        # not thinking never replay as each other.
        llamacpp = f"{s.llamacpp_model}@reasoning-{s.llamacpp_reasoning_effort}"
        model = {
            "ollama": (s.ollama_extraction_model, s.ollama_synthesis_model),
            "llamacpp": (llamacpp, llamacpp),
        }.get(provider, (s.extraction_model, s.synthesis_model))
        appraisal = {
            "ollama": self._ollama_appraisal_model(),
            "llamacpp": llamacpp,
        }.get(provider, s.appraisal_model or s.synthesis_model)
        return {
            "llm_provider": provider,
            "extraction_model": model[0],
            "synthesis_model": model[1],
            # Empty settings mean the synthesis model, as in llm/factory.py.
            "appraisal_model": appraisal if s.appraisal_enabled else None,
            "embedding_model": self.embedder.model_id if self.embedder else None,
            "enabled_providers": list(s.enabled_providers),
            "retrieval_top_k": s.retrieval_top_k,
            "retrieval_min_year": s.retrieval_min_year,
            "full_text_max_papers": s.full_text_max_papers,
            "appraisal_mode": s.appraisal_mode if s.appraisal_enabled else None,
            "mode": self.mode,
        }

    def _ollama_appraisal_model(self) -> str:
        return self.settings.ollama_appraisal_model or self.settings.ollama_synthesis_model

    def model_id(self, call: str) -> str:
        info = self.describe()
        return f"{info['llm_provider']}/{info[f'{call}_model']}"

    async def aclose(self) -> None:
        await self.http.aclose()
        self.cassette.close()


def check_stage_parity(settings: Settings) -> list[str]:
    """Compare the eval's stage order with ``build_default_pipeline``'s.

    Builds the production list without touching the database (construction
    makes no queries) and reports any difference beyond the two stages the
    eval skips on purpose.
    """
    from app.pipeline.orchestrator import build_default_pipeline

    try:
        production = [stage.name for stage in build_default_pipeline(None, settings)]  # type: ignore[arg-type]
    except Exception as exc:
        return [f"could not build the production stage list to compare: {exc}"]
    expected = [name for name in production if name not in NOT_EVALUATED]
    evaluated = [
        StageName.EXTRACT,
        StageName.RETRIEVE,
        StageName.RANK,
        StageName.FULL_TEXT,
        StageName.APPRAISE,
        StageName.SYNTHESIZE,
        StageName.VALIDATE,
    ]
    if expected != evaluated:
        return [
            "STAGE DRIFT: production runs "
            f"{[str(name) for name in production]} but the eval runs "
            f"{[str(name) for name in evaluated]} "
            f"(+ {[str(n) for n in NOT_EVALUATED]} skipped). "
            "Update evaluation/harness/pipeline.py."
        ]
    return []


class InjectingProvider:
    """Appends synthetic records to one provider's results — for injection cases."""

    def __init__(self, inner: ScholarlyProvider, documents: list[InjectedDocument]) -> None:
        self._inner = inner
        self._documents = documents

    @property
    def source_api(self) -> SourceApi:
        return self._inner.source_api

    async def search(self, query: SearchQuery) -> list[CandidatePaper]:
        papers = await self._inner.search(query)
        return [*papers, *(_injected_paper(doc, self.source_api) for doc in self._documents)]


def _injected_paper(doc: InjectedDocument, source_api: SourceApi) -> CandidatePaper:
    return CandidatePaper(
        doi=doc.doi,
        title=doc.title,
        abstract=doc.abstract,
        journal=doc.journal,
        year=doc.year,
        study_type=classify_study_type(doc.publication_types, doc.title, doc.abstract),
        raw_study_type="; ".join(doc.publication_types) or None,
        url=f"https://doi.org/{doc.doi}",
        source_api=source_api,
    )


class _Aborted(Exception):
    def __init__(self, ctx: PipelineContext, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.ctx = ctx
        self.cause = cause


class _EvalRunner:
    """The ``StageRunner`` the graph calls per node: span, run, snapshot.

    The analogue of ``PipelineOrchestrator._run_stage``, minus the database:
    the ordinal is offset by refine and revise rounds exactly as production
    offsets it, so a trace's stage list reads the way ``pipeline_stage_runs``
    would.
    """

    def __init__(self, recorder: TraceRecorder, stages: list[Stage]) -> None:
        self._recorder = recorder
        self._stages = stages

    async def __call__(
        self, ordinal: int, stage: Stage, ctx: PipelineContext, attempt: int
    ) -> PipelineContext:
        name = str(stage.name)
        span = self._recorder.begin_stage(
            name,
            ordinal + (ctx.refine_round + ctx.revise_round) * len(self._stages),
            ctx.refine_round,
            ctx.revise_round,
        )
        token = CURRENT_STAGE.set(name)
        try:
            new_ctx = await stage.run(ctx)
        except StageError as exc:
            self._recorder.end_stage(
                span,
                error={
                    "type": "StageError",
                    "message": str(exc),
                    "retryable": exc.retryable,
                    "cause": type(exc.__cause__).__name__ if exc.__cause__ else None,
                },
                metrics=ctx.metrics.get(name),
            )
            raise _Aborted(ctx, exc) from exc
        except Exception as exc:
            self._recorder.end_stage(
                span,
                error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "unexpected": True,
                    "traceback": traceback.format_exc(limit=6),
                },
                metrics=ctx.metrics.get(name),
            )
            raise _Aborted(ctx, exc) from exc
        finally:
            CURRENT_STAGE.reset(token)

        self._recorder.end_stage(span, metrics=new_ctx.metrics.get(name))
        _snapshot(self._recorder.trace, stage.name, new_ctx)
        return new_ctx


def _paper_record(source_id: str, paper: CandidatePaper) -> PaperRecord:
    return PaperRecord(
        source_id=source_id,
        pmid=paper.pmid,
        doi=paper.doi,
        title=paper.title,
        abstract=paper.abstract,
        journal=paper.journal,
        year=paper.year,
        study_type=str(paper.study_type),
        raw_study_type=paper.raw_study_type,
        url=paper.url,
        source_api=str(paper.source_api),
        injected=bool(paper.doi and paper.doi.startswith("10.0000/eval-")),
    )


def _snapshot(trace: Trace, stage: StageName, ctx: PipelineContext) -> None:
    """Copy what each stage produced into the trace, and the decision that follows."""
    if stage is StageName.EXTRACT and ctx.extraction is not None:
        trace.extraction = ctx.extraction.model_dump(mode="json")
    elif stage is StageName.RETRIEVE:
        for candidates in ctx.candidates.values():
            for entry in candidates:
                trace.papers.setdefault(
                    entry.source_id, _paper_record(entry.source_id, entry.paper)
                )
    elif stage is StageName.RANK:
        trace.rounds.append(
            RetrievalRound(
                round=ctx.refine_round,
                candidates={
                    claim: [entry.source_id for entry in entries]
                    for claim, entries in ctx.candidates.items()
                },
                ranked={
                    claim: [
                        RankedEntry(
                            source_id=entry.source_id,
                            handle=entry.citation_handle,
                            cosine=round(entry.cosine_similarity, 4),
                            score=round(entry.final_score, 4),
                        )
                        for entry in entries
                    ]
                    for claim, entries in ctx.ranked.items()
                },
                thin_claims=list(ctx.thin_claims),
            )
        )
        if ctx.thin_claims:
            refine = ctx.refine_round < MAX_REFINE_ROUNDS
            trace.decisions.append(
                {
                    "after": "rank",
                    "round": ctx.refine_round,
                    "decision": "refine" if refine else "continue (refinement budget spent)",
                    "reason": (
                        f"{len(ctx.thin_claims)} claim(s) below the supported-tier quorum"
                    ),
                    "thin_claims": list(ctx.thin_claims),
                }
            )
    elif stage is StageName.FULL_TEXT:
        trace.excerpts = {
            source_id: [excerpt.model_dump() for excerpt in excerpts]
            for source_id, excerpts in ctx.excerpts.items()
        }
    elif stage is StageName.APPRAISE:
        trace.stances = {
            claim: {source_id: str(stance) for source_id, stance in labels.items()}
            for claim, labels in ctx.stances.items()
        }
    elif stage is StageName.SYNTHESIZE:
        if ctx.synthesis_input is not None:
            trace.synthesis_input = ctx.synthesis_input.model_dump(mode="json")
        metrics = ctx.metrics.get(str(StageName.SYNTHESIZE), {})
        if metrics.get("model_called") is False:
            trace.decisions.append(
                {
                    "after": "synthesize",
                    "decision": "deterministic no-evidence article (no model call)",
                    "reason": metrics.get("no_evidence_cause"),
                }
            )
        elif metrics.get("coverage_retry"):
            trace.decisions.append(
                {
                    "after": "synthesize",
                    "decision": "coverage re-prompt",
                    "reason": (
                        f"first draft covered {metrics.get('first_draft_handles_covered')} of "
                        f"{metrics.get('sources_in_prompt')} sources"
                    ),
                }
            )
    elif stage is StageName.VALIDATE and ctx.validation is not None:
        report = ctx.validation
        trace.validations.append(report.model_dump(mode="json"))
        if report.passed:
            decision = "accept (would be stored pending_review)"
        elif (
            ctx.revise_round < MAX_REVISE_ROUNDS
            and report.failures
            and all(f.code in REVISABLE_FAILURES for f in report.failures)
        ):
            decision = "revise (sent back to synthesize with the failures)"
        else:
            decision = "reject (would be stored validation_failed)"
        trace.decisions.append(
            {
                "after": "validate",
                "round": ctx.revise_round,
                "decision": decision,
                "failures": sorted({f.code for f in report.failures}),
            }
        )


def _settings_for(case: EvalCase, env: EvalEnvironment) -> Settings:
    if not case.faults:
        return env.settings
    fast = env.config.failure_suite
    return env.settings.model_copy(
        update={
            "provider_max_retries": fast.provider_max_retries,
            "provider_max_retry_wait_seconds": fast.provider_max_retry_wait_seconds,
        }
    )


def build_stages(
    case: EvalCase,
    env: EvalEnvironment,
    recorder: TraceRecorder,
    store: MemoryStore,
    *,
    stop_after: StageName | None = None,
) -> list[Stage]:
    """The production stage list, wired to the eval's seams.

    Mirrors ``build_default_pipeline`` argument for argument; see
    ``check_stage_parity`` for the guard against the two drifting apart.
    """
    settings = _settings_for(case, env)
    faults = FaultInjector(case.faults)
    http = EvalHttp(env.http, env.cassette, env.mode, recorder, faults)

    providers = build_providers(settings, http)  # type: ignore[arg-type]
    if case.inject_documents:
        by_provider: dict[str, list[InjectedDocument]] = {}
        for doc in case.inject_documents:
            by_provider.setdefault(doc.provider, []).append(doc)
        providers = [
            InjectingProvider(provider, by_provider[str(provider.source_api)])
            if str(provider.source_api) in by_provider
            else provider
            for provider in providers
        ]
    europe_pmc = build_europe_pmc_client(settings, http)  # type: ignore[arg-type]

    embedder = CachingEmbedder(
        env.embedder, env.cassette, env.mode, recorder, faults.component("embeddings")
    )
    extraction = EvalExtractionClient(
        env.extraction,
        env.model_id("extraction"),
        env.cassette,
        env.mode,
        recorder,
        faults.component("llm_extraction"),
    )
    synthesis_fault = faults.component("llm_synthesis")
    synthesis: Any
    if synthesis_fault is not None and synthesis_fault.kind == "hallucinate":
        synthesis = HallucinatingSynthesisClient(recorder)
    elif case.llm == "scripted":
        synthesis = ScriptedSynthesisClient(recorder)
    else:
        synthesis = EvalSynthesisClient(
            env.synthesis,
            env.model_id("synthesis"),
            env.cassette,
            env.mode,
            recorder,
            synthesis_fault,
        )

    appraisal = (
        EvalAppraisalClient(
            env.appraisal,
            env.model_id("appraisal"),
            env.cassette,
            env.mode,
            recorder,
            faults.component("llm_appraisal"),
        )
        if env.settings.appraisal_enabled
        else None
    )

    stages: list[Stage] = [
        ExtractStage(extraction),
        RetrieveStage(
            providers,
            TemplateQueryStrategy(min_year=settings.retrieval_min_year),
            MemorySourceCache(store, embedder),  # type: ignore[arg-type]
            settings.retrieval_max_candidates_per_claim,
            resolver=EuropePMCIdentifiers(europe_pmc),
            retraction_screen=PubMedRetractionSource(
                build_pubmed_client(settings, http),  # type: ignore[arg-type]
                settings.pubmed_api_key or None,
            ),
        ),
        RankStage(
            MemoryReranker(store, embedder, faults.component("vector_store")),  # type: ignore[arg-type]
            RerankConfig(top_k=settings.retrieval_top_k, min_year=settings.retrieval_min_year),
        ),
        FullTextStage(EuropePMCFullText(europe_pmc), embedder, settings.full_text_max_papers),
        AppraiseStage(appraisal, two_call=settings.appraisal_mode == "two_call"),
        SynthesizeStage(synthesis, relevance_filter=settings.synthesis_relevance_filter),
        ValidateStage(MemorySession(store)),  # type: ignore[arg-type]
    ]
    if stop_after is not None:
        names = [stage.name for stage in stages]
        stages = stages[: names.index(stop_after) + 1]
    return stages


def classify_outcome(
    ctx: PipelineContext | None, error: BaseException | None, *, retrieval_only: bool
) -> Outcome:
    if error is not None:
        if isinstance(error, StageError):
            if isinstance(error.__cause__, UnanchoredQuery):
                return Outcome.REFUSED_UNANCHORED
            return Outcome.FAILED_RETRYABLE if error.retryable else Outcome.FAILED_PERMANENT
        return Outcome.FAILED_UNEXPECTED
    if retrieval_only:
        return Outcome.RETRIEVAL_ONLY
    if ctx is None or ctx.validation is None or ctx.draft is None:
        return Outcome.CRASHED
    if not ctx.validation.passed:
        return Outcome.ANSWERED_UNVALIDATED
    return Outcome.NO_EVIDENCE if ctx.draft.verdict is Verdict.NO_EVIDENCE else Outcome.ANSWERED


def needs(case: EvalCase) -> list[str]:
    """Components a case cannot run without. EXTRACT runs in every suite,
    including retrieval-only, so the generative client is always needed —
    unless the case faults it on purpose."""
    required = ["embeddings"]
    if not any(fault.target == "llm_extraction" for fault in case.faults):
        required.append("llm")
    return required


async def execute_case(
    case: EvalCase, env: EvalEnvironment, *, stop_after: StageName | None = None
) -> Trace:
    recorder = TraceRecorder.start(case.id, case.executed_query, env.mode, env.describe())
    missing = [c for c in needs(case) if c in env.unavailable]
    if missing:
        recorder.trace.skip_reason = "; ".join(env.unavailable[c] for c in missing)
        return recorder.finish(Outcome.SKIPPED)

    store = MemoryStore()
    stages = build_stages(case, env, recorder, store, stop_after=stop_after)
    graph = build_pipeline_graph(stages, _EvalRunner(recorder, stages))
    ctx = PipelineContext(run_id=f"eval-{case.id}", topic=case.executed_query, blurb=case.blurb)

    error: BaseException | None = None
    final: PipelineContext | None = None
    outcome: Outcome | None = None
    try:
        state = await asyncio.wait_for(
            graph.ainvoke(
                {"ctx": ctx, "attempt": 1},
                config={"recursion_limit": recursion_limit(stages, MAX_REFINE_ROUNDS)},
            ),
            timeout=env.config.run.case_timeout_seconds,
        )
        final = state["ctx"]
    except _Aborted as aborted:
        final, error = aborted.ctx, aborted.cause
    except GraphRecursionError as exc:
        outcome = Outcome.LOOP_LIMIT
        recorder.trace.error = {"type": "GraphRecursionError", "message": str(exc)}
    except TimeoutError:
        outcome = Outcome.TIMEOUT
        recorder.trace.error = {
            "type": "TimeoutError",
            "message": f"case exceeded {env.config.run.case_timeout_seconds:.0f}s",
        }
    except Exception as exc:  # anything here escaped the graph: a harness bug
        logger.exception("case %s crashed outside the graph", case.id)
        outcome = Outcome.CRASHED
        recorder.trace.error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(limit=8),
        }

    if outcome is None:
        outcome = classify_outcome(final, error, retrieval_only=stop_after is not None)
    if error is not None:
        recorder.trace.error = {
            "type": type(error).__name__,
            "message": str(error),
            "retryable": getattr(error, "retryable", None),
            "cause": type(error.__cause__).__name__ if error.__cause__ else None,
        }
    if final is not None:
        if final.extraction is not None and recorder.trace.extraction is None:
            recorder.trace.extraction = final.extraction.model_dump(mode="json")
        if final.draft is not None:
            recorder.trace.final_draft = final.draft.model_dump(mode="json")
        if final.validation is not None:
            recorder.trace.final_validation = final.validation.model_dump(mode="json")
        recorder.trace.stage_metrics = dict(final.metrics)
        recorder.trace.usage_by_stage = {
            name: {"model": entry.model, **asdict(entry.usage)}
            for name, entry in final.usage_by_stage.items()
        }
    if recorder.trace.cassette_misses:
        recorder.trace.skip_reason = (
            f"{recorder.trace.cassette_misses} call(s) were not in the recording (replay mode)"
        )
        return recorder.finish(Outcome.SKIPPED)
    return recorder.finish(outcome)
