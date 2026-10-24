"""The stage graph: ordering, the refinement cycle, and its bounds.

These exercise `pipeline/graph.py` directly with fake stages, so they need no
database and no model. The transaction guarantees the graph runs *inside* are
covered by `test_orchestrator_transactions.py` against real Postgres — a fake
session cannot fail any of those, and this file deliberately does not pretend to.
"""

from __future__ import annotations

from app.domain.contracts import ExtractionOutput, ValidationFailure, ValidationReport
from app.domain.enums import StudyType
from app.pipeline.graph import (
    MAX_REFINE_ROUNDS,
    MAX_REVISE_ROUNDS,
    build_pipeline_graph,
    recursion_limit,
)
from app.pipeline.stages import PipelineContext, Stage, StageName


class _Recorder:
    """A stage that records that it ran and otherwise leaves the context alone."""

    def __init__(self, name: StageName, log: list[StageName]) -> None:
        self.name = name
        self._log = log

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        self._log.append(self.name)
        return ctx


class _RankWithThinClaims:
    """A RANK that reports thin claims until it has been re-run enough times."""

    name = StageName.RANK

    def __init__(self, log: list[StageName], thin_while_below: int) -> None:
        self._log = log
        self._thin_while_below = thin_while_below

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        self._log.append(self.name)
        thin = ["a claim"] if ctx.refine_round < self._thin_while_below else []
        return ctx.model_copy(update={"thin_claims": thin})


def _ctx() -> PipelineContext:
    return PipelineContext(
        run_id="run-1",
        topic="Ashwagandha",
        extraction=ExtractionOutput(
            product="Ashwagandha", target_claims=["a claim"], ingredients=[]
        ),
    )


async def _drive(stages: list[Stage]) -> PipelineContext:
    async def runner(
        ordinal: int, stage: Stage, ctx: PipelineContext, attempt: int
    ) -> PipelineContext:
        return await stage.run(ctx)

    graph = build_pipeline_graph(stages, runner)
    final = await graph.ainvoke(
        {"ctx": _ctx(), "attempt": 1},
        config={"recursion_limit": recursion_limit(stages, MAX_REFINE_ROUNDS)},
    )
    return final["ctx"]


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


async def test_stages_run_in_list_order() -> None:
    log: list[StageName] = []
    order = [
        StageName.EXTRACT,
        StageName.RETRIEVE,
        StageName.RANK,
        StageName.SYNTHESIZE,
        StageName.ILLUSTRATE,
        StageName.VALIDATE,
        StageName.PERSIST,
    ]
    stages = [_Recorder(name, log) for name in order]

    await _drive(stages)  # type: ignore[arg-type]

    assert log == order


async def test_persist_runs_last() -> None:
    """Not a restatement of the above: PERSIST being last is what makes its
    write atomic with the run's completion row, and the graph is now what
    decides ordering."""
    log: list[StageName] = []
    stages = [
        _Recorder(name, log)
        for name in (StageName.EXTRACT, StageName.VALIDATE, StageName.PERSIST)
    ]

    await _drive(stages)  # type: ignore[arg-type]

    assert log[-1] is StageName.PERSIST


async def test_a_single_stage_list_still_compiles() -> None:
    """Tests build orchestrators over one fake stage. Keying nodes on StageName
    or assuming RETRIEVE/RANK exist would make those undefined."""
    log: list[StageName] = []

    await _drive([_Recorder(StageName.EXTRACT, log)])  # type: ignore[arg-type]

    assert log == [StageName.EXTRACT]


# ---------------------------------------------------------------------------
# The refinement cycle
# ---------------------------------------------------------------------------


async def test_thin_claims_send_the_run_back_through_retrieve() -> None:
    log: list[StageName] = []
    stages = [
        _Recorder(StageName.EXTRACT, log),
        _Recorder(StageName.RETRIEVE, log),
        _RankWithThinClaims(log, thin_while_below=1),
        _Recorder(StageName.SYNTHESIZE, log),
    ]

    ctx = await _drive(stages)  # type: ignore[arg-type]

    assert log == [
        StageName.EXTRACT,
        StageName.RETRIEVE,
        StageName.RANK,
        StageName.RETRIEVE,  # the second pass
        StageName.RANK,
        StageName.SYNTHESIZE,
    ]
    assert ctx.refine_round == 1


async def test_no_thin_claims_means_no_second_pass() -> None:
    log: list[StageName] = []
    stages = [
        _Recorder(StageName.RETRIEVE, log),
        _RankWithThinClaims(log, thin_while_below=0),
        _Recorder(StageName.SYNTHESIZE, log),
    ]

    ctx = await _drive(stages)  # type: ignore[arg-type]

    assert log == [StageName.RETRIEVE, StageName.RANK, StageName.SYNTHESIZE]
    assert ctx.refine_round == 0


async def test_the_loop_is_bounded_even_when_the_claim_stays_thin() -> None:
    """A claim still thin after broadening is a fact about the literature, and
    every extra pass spends provider budget against a per-IP ceiling the worker
    shares. The cautious verdict the quorum produces is the honest answer."""
    log: list[StageName] = []
    stages = [
        _Recorder(StageName.RETRIEVE, log),
        _RankWithThinClaims(log, thin_while_below=99),
        _Recorder(StageName.SYNTHESIZE, log),
    ]

    ctx = await _drive(stages)  # type: ignore[arg-type]

    assert log.count(StageName.RETRIEVE) == 1 + MAX_REFINE_ROUNDS
    assert ctx.refine_round == MAX_REFINE_ROUNDS
    assert log[-1] is StageName.SYNTHESIZE, "the run still finishes"


async def test_the_cycle_is_absent_when_rank_precedes_retrieve() -> None:
    """Only added when the two stages are present in the order that makes a
    cycle meaningful. A list that is not the real pipeline compiles as a chain."""
    log: list[StageName] = []
    stages = [
        _RankWithThinClaims(log, thin_while_below=99),
        _Recorder(StageName.RETRIEVE, log),
    ]

    await _drive(stages)  # type: ignore[arg-type]

    assert log == [StageName.RANK, StageName.RETRIEVE]


def test_the_recursion_limit_covers_every_refinement_pass() -> None:
    """Derived from the graph's shape, so adding a stage cannot walk into a
    limit whose error says nothing about the actual problem."""
    log: list[StageName] = []
    seven = [_Recorder(StageName.EXTRACT, log) for _ in range(7)]

    # Seven nodes, plus refine + retrieve + rank for each refinement round.
    assert recursion_limit(seven, 1) >= 7 + 3  # type: ignore[arg-type]
    assert recursion_limit(seven, 2) >= 7 + 6  # type: ignore[arg-type]


async def test_thin_claims_are_carried_to_retrieve_on_the_second_pass() -> None:
    """RETRIEVE re-searches the claims RANK named, not all of them. Passing the
    whole list would re-query claims that are already well served, against a
    provider ceiling the worker shares."""
    seen: list[list[str]] = []

    class _RecordingRetrieve:
        name = StageName.RETRIEVE

        async def run(self, ctx: PipelineContext) -> PipelineContext:
            seen.append(list(ctx.thin_claims))
            return ctx

    log: list[StageName] = []
    stages = [_RecordingRetrieve(), _RankWithThinClaims(log, thin_while_below=1)]

    await _drive(stages)  # type: ignore[arg-type]

    assert seen == [[], ["a claim"]]


# ---------------------------------------------------------------------------
# The revision cycle (VALIDATE -> SYNTHESIZE)
# ---------------------------------------------------------------------------


class _Validate:
    """A VALIDATE that fails with ``code`` until the draft has been revised enough."""

    name = StageName.VALIDATE

    def __init__(self, log: list[StageName], *, fail_while_below: int, code: str) -> None:
        self._log = log
        self._fail_while_below = fail_while_below
        self._code = code

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        self._log.append(self.name)
        passed = ctx.revise_round >= self._fail_while_below
        report = ValidationReport(
            passed=passed,
            citations_total=1,
            citations_resolved=1,
            best_evidence_grade=StudyType.RCT,
            failures=[]
            if passed
            else [ValidationFailure(code=self._code, message="beat 2 cites nothing")],
        )
        return ctx.model_copy(update={"validation": report})


def _revision_stages(
    log: list[StageName], *, fail_while_below: int, code: str = "uncited_beat"
) -> list[Stage]:
    return [
        _Recorder(StageName.EXTRACT, log),
        _Recorder(StageName.SYNTHESIZE, log),
        _Recorder(StageName.ILLUSTRATE, log),
        _Validate(log, fail_while_below=fail_while_below, code=code),
        _Recorder(StageName.PERSIST, log),
    ]  # type: ignore[list-item]


async def test_a_failed_draft_is_rewritten_once_then_persisted() -> None:
    log: list[StageName] = []

    ctx = await _drive(_revision_stages(log, fail_while_below=1))

    assert log == [
        StageName.EXTRACT,
        StageName.SYNTHESIZE,
        StageName.ILLUSTRATE,
        StageName.VALIDATE,
        StageName.SYNTHESIZE,  # the rewrite
        StageName.ILLUSTRATE,
        StageName.VALIDATE,
        StageName.PERSIST,
    ]
    assert ctx.revise_round == 1
    assert ctx.validation is not None and ctx.validation.passed


async def test_the_rewrite_is_bounded_and_a_second_failure_is_persisted_as_is() -> None:
    """The invariant is that a failing draft never reaches the queue — PERSIST
    stores it as validation_failed — not that every topic yields an article."""
    log: list[StageName] = []

    ctx = await _drive(_revision_stages(log, fail_while_below=99))

    assert log.count(StageName.SYNTHESIZE) == 1 + MAX_REVISE_ROUNDS
    assert log[-1] is StageName.PERSIST
    assert ctx.validation is not None and not ctx.validation.passed


async def test_a_failure_a_rewrite_cannot_fix_goes_straight_to_persist() -> None:
    log: list[StageName] = []

    await _drive(_revision_stages(log, fail_while_below=1, code="unresolvable_source"))

    assert log.count(StageName.SYNTHESIZE) == 1


async def test_a_passing_draft_is_not_rewritten() -> None:
    log: list[StageName] = []

    ctx = await _drive(_revision_stages(log, fail_while_below=0))

    assert log.count(StageName.SYNTHESIZE) == 1
    assert ctx.revise_round == 0


async def test_appraise_runs_once_after_refinement_and_is_skipped_by_a_rewrite() -> None:
    """APPRAISE labels the final ranked set, so it must follow the RANK ->
    RETRIEVE loop rather than sit inside it; and a rewrite goes back to
    SYNTHESIZE, reusing the labels instead of paying for them twice."""
    log: list[StageName] = []
    stages = [
        _Recorder(StageName.RETRIEVE, log),
        _RankWithThinClaims(log, thin_while_below=1),
        _Recorder(StageName.APPRAISE, log),
        _Recorder(StageName.SYNTHESIZE, log),
        _Validate(log, fail_while_below=1, code="uncited_beat"),
        _Recorder(StageName.PERSIST, log),
    ]

    await _drive(stages)  # type: ignore[arg-type]

    assert log == [
        StageName.RETRIEVE,
        StageName.RANK,
        StageName.RETRIEVE,
        StageName.RANK,
        StageName.APPRAISE,
        StageName.SYNTHESIZE,
        StageName.VALIDATE,
        StageName.SYNTHESIZE,
        StageName.VALIDATE,
        StageName.PERSIST,
    ]
