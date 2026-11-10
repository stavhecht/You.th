"""Token accounting and pricing.

Cost is the one number in this system with no natural feedback loop. A wrong
verdict gets caught by a reviewer and a wrong citation gets caught by
validation, but a run reported at a third of its real cost looks exactly like a
cheap run — right up until the invoice. So the failure modes worth testing here
are the *quiet* ones: an unpriced model reading as free, a retried run reporting
only its last attempt, a failed call reporting nothing at all.

No database. The orchestrator's writes are covered in
``test_orchestrator_transactions.py``, which has one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.contracts import ExtractionInput, ExtractionOutput
from app.llm import anthropic_client, ollama_client
from app.llm.base import LLMError, LLMResult, TokenUsage
from app.llm.pricing import PRICES, cost_usd, total_cost_usd
from app.pipeline.stages import PipelineContext, StageError, StageName
from app.pipeline.steps.extract import ExtractStage

SONNET = "anthropic/claude-sonnet-5"
LOCAL = "ollama/llama3.1:8b"


def _ctx() -> PipelineContext:
    return PipelineContext(run_id="r1", topic="ashwagandha for stress")


# ---------------------------------------------------------------------------
# The price table.
# ---------------------------------------------------------------------------


def test_a_priced_model_costs_what_the_table_says() -> None:
    cost = cost_usd(SONNET, TokenUsage(input_tokens=1_000_000))
    assert cost == Decimal("3.000000")


def test_each_token_class_is_priced_separately() -> None:
    """The reason there are four columns rather than two.

    A cache read is a tenth of a fresh input token and a cache write a quarter
    more than one. Folding them into `input_tokens` would make caching look
    free and make the first call look cheap — in opposite directions, so they
    would not even cancel.
    """
    million = 1_000_000
    fresh = cost_usd(SONNET, TokenUsage(input_tokens=million))
    read = cost_usd(SONNET, TokenUsage(cache_read_tokens=million))
    write = cost_usd(SONNET, TokenUsage(cache_write_tokens=million))
    assert fresh is not None and read is not None and write is not None

    assert read < fresh, "a cache read must be cheaper than a fresh input token"
    assert write > fresh, "a cache write must cost more than a fresh input token"


def test_an_unpriced_model_is_unknown_not_free() -> None:
    """The distinction the whole module hangs on.

    Zero and None are both falsy and mean opposite things. A silent zero would
    appear exactly when someone points SYNTHESIS_MODEL at something new — the
    one moment a "this run cost nothing" reading would be believed.
    """
    assert cost_usd("anthropic/claude-not-a-real-model", TokenUsage(input_tokens=999)) is None


def test_a_local_model_is_free_and_that_is_a_number() -> None:
    assert cost_usd(LOCAL, TokenUsage(input_tokens=999, output_tokens=999)) == Decimal(0)


def test_a_bare_model_name_is_not_in_the_table() -> None:
    """Namespacing is load-bearing, not decoration.

    Without the provider prefix a local `llama3.1:8b` and a hosted model nobody
    has priced report the same cost for opposite reasons.
    """
    assert cost_usd("claude-sonnet-5", TokenUsage(input_tokens=1000)) is None
    assert cost_usd("llama3.1:8b", TokenUsage(input_tokens=1000)) is None


def test_every_price_is_namespaced() -> None:
    """Structural guard: a key added without a provider prefix never matches."""
    for model in PRICES:
        assert "/" in model, f"{model!r} is missing its provider namespace"


def test_the_clients_default_models_are_priced() -> None:
    """Catches the most likely way the table goes stale.

    Changing a default model without adding its price yields runs that report
    `null` cost forever, and nothing else complains.
    """
    for model in (anthropic_client.EXTRACTION_MODEL, anthropic_client.SYNTHESIS_MODEL):
        assert anthropic_client.qualified(model) in PRICES
    for model in (ollama_client.EXTRACTION_MODEL, ollama_client.SYNTHESIS_MODEL):
        assert ollama_client.qualified(model) in PRICES


def test_prices_are_decimal_not_float() -> None:
    """`Decimal("0.30")` is 0.30; `Decimal(0.30)` is 0.2999999999999999888977698.

    Cheap to get wrong, and the error compounds over a summed ledger.
    """
    tenth_of_a_million = TokenUsage(cache_read_tokens=100_000)
    assert cost_usd(SONNET, tenth_of_a_million) == Decimal("0.030000")


# ---------------------------------------------------------------------------
# Summing across calls.
# ---------------------------------------------------------------------------


def test_a_run_sums_its_calls() -> None:
    extract = TokenUsage(input_tokens=306, output_tokens=30)
    synth = TokenUsage(input_tokens=10_336, output_tokens=423)
    total = total_cost_usd([(SONNET, extract), (SONNET, synth)])
    assert total == cost_usd(SONNET, extract) + cost_usd(SONNET, synth)  # type: ignore[operator]


def test_one_unpriced_call_makes_the_whole_run_unknown() -> None:
    """All-or-nothing, on purpose.

    Extraction is roughly 1% of a run. Reporting it alone would produce a
    number that is both plausible and wrong by two orders of magnitude —
    strictly worse than admitting we do not know.
    """
    priced = TokenUsage(input_tokens=306, output_tokens=30)
    unpriced = TokenUsage(input_tokens=10_336, output_tokens=423)
    assert total_cost_usd([(SONNET, priced), ("mystery-model", unpriced)]) is None


def test_a_call_that_consumed_nothing_needs_no_price() -> None:
    """The no-evidence branch skips synthesis entirely.

    Its stage row has no model and no tokens. Demanding a price for a call that
    never happened would blank the cost of an otherwise fully-priced run.
    """
    extract = TokenUsage(input_tokens=306, output_tokens=30)
    total = total_cost_usd([(SONNET, extract), ("", TokenUsage())])
    assert total == cost_usd(SONNET, extract)


def test_a_run_with_no_calls_costs_zero_not_unknown() -> None:
    assert total_cost_usd([]) == Decimal(0)


def test_a_cache_hit_is_visibly_cheaper_than_a_cold_call() -> None:
    """What the cache_control breakpoint was supposed to buy, now measurable.

    The synthesis system prompt is ~1285 tokens. Warm, those move from the
    input rate to the cache-read rate; the saving was invisible before this
    because the persistence layer dropped both cache fields.
    """
    cold = TokenUsage(input_tokens=10_336, output_tokens=423, cache_write_tokens=1_285)
    warm = TokenUsage(
        input_tokens=10_336 - 1_285, output_tokens=423, cache_read_tokens=1_285
    )
    cold_cost = cost_usd(SONNET, cold)
    warm_cost = cost_usd(SONNET, warm)
    assert cold_cost is not None and warm_cost is not None
    assert warm_cost < cold_cost


# ---------------------------------------------------------------------------
# The context ledger.
# ---------------------------------------------------------------------------


def test_the_run_total_is_derived_from_the_stage_ledger() -> None:
    ctx = _ctx()
    ctx.record_usage(StageName.EXTRACT, SONNET, TokenUsage(input_tokens=300, output_tokens=30))
    ctx.record_usage(
        StageName.SYNTHESIZE, SONNET, TokenUsage(input_tokens=10_000, output_tokens=400)
    )
    assert ctx.usage.input_tokens == 10_300
    assert ctx.usage.output_tokens == 430


def test_recording_twice_for_one_stage_adds_rather_than_overwrites() -> None:
    """Ollama's validation-repair retry makes two calls inside one stage.

    Both were billed. Overwriting would report the repair attempt as though it
    were the only call.
    """
    ctx = _ctx()
    ctx.record_usage(StageName.SYNTHESIZE, LOCAL, TokenUsage(output_tokens=400))
    ctx.record_usage(StageName.SYNTHESIZE, LOCAL, TokenUsage(output_tokens=380))
    assert ctx.usage_by_stage["synthesize"].usage.output_tokens == 780


def test_a_stage_that_called_no_model_is_absent_from_the_ledger() -> None:
    ctx = _ctx()
    assert ctx.usage_by_stage == {}
    assert ctx.usage == TokenUsage()


def test_the_ledger_survives_model_copy() -> None:
    """Stages return `ctx.model_copy(update=...)`; the ledger must come along."""
    ctx = _ctx()
    ctx.record_usage(StageName.EXTRACT, SONNET, TokenUsage(output_tokens=30))
    assert ctx.model_copy(update={"article_id": "a1"}).usage.output_tokens == 30


# ---------------------------------------------------------------------------
# Tokens spent by a call that failed.
# ---------------------------------------------------------------------------


def test_an_llm_error_carries_no_usage_by_default() -> None:
    """A transport failure consumed nothing, and must not invent a charge."""
    exc = LLMError("connection refused")
    assert exc.usage == TokenUsage()
    assert exc.model == ""


def test_an_llm_error_can_carry_what_a_failed_call_consumed() -> None:
    exc = LLMError("truncated", usage=TokenUsage(output_tokens=8_000), model=SONNET)
    assert exc.usage.output_tokens == 8_000
    assert exc.model == SONNET


class _FailingExtractionClient:
    """A model that answered and then failed — billed in full."""

    def __init__(self, exc: LLMError) -> None:
        self._exc = exc

    async def extract(self, payload: ExtractionInput) -> LLMResult[ExtractionOutput]:
        raise self._exc


class _WorkingExtractionClient:
    async def extract(self, payload: ExtractionInput) -> LLMResult[ExtractionOutput]:
        return LLMResult(
            output=ExtractionOutput(
                product="ashwagandha",
                target_claims=["reduces stress"],
                ingredients=["ashwagandha"],
            ),
            usage=TokenUsage(input_tokens=306, output_tokens=30),
            model=SONNET,
        )


async def test_a_stage_records_the_tokens_a_failed_call_burned() -> None:
    """The most expensive failure must not be the cheapest-looking one.

    Truncation spends the entire output budget and produces nothing. Recorded
    on the context rather than returned, because a raising stage returns no
    context — which is precisely why `record_usage` mutates in place.
    """
    ctx = _ctx()
    stage = ExtractStage(
        _FailingExtractionClient(
            LLMError("hit max_tokens", usage=TokenUsage(output_tokens=4_000), model=SONNET)
        )
    )

    with pytest.raises(StageError):
        await stage.run(ctx)

    assert ctx.usage_by_stage["extract"].usage.output_tokens == 4_000
    assert ctx.usage_by_stage["extract"].model == SONNET


async def test_a_successful_stage_records_its_model_and_tokens() -> None:
    ctx = _ctx()
    out = await ExtractStage(_WorkingExtractionClient()).run(ctx)

    recorded = out.usage_by_stage["extract"]
    assert recorded.model == SONNET
    assert recorded.usage.input_tokens == 306
    assert out.usage.output_tokens == 30
