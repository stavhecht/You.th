"""What a model call costs, kept separate from what it consumed.

The database stores tokens; this module turns tokens into dollars at read time.
That split is deliberate. Tokens are a fact about a call that happened and never
change; a price is an external number that does, and baking a cost column into
``pipeline_stage_runs`` would freeze whichever price was current the day the run
executed — with no record of which one it was. Recomputing means a price
correction fixes history instead of leaving it quietly wrong.

The trade is the mirror image: repricing is retroactive, so past runs re-quote at
today's rates. That is the right default while these numbers inform a build
decision rather than a customer invoice. If a run's cost ever has to be *owed*
rather than *estimated*, this becomes a table with effective dates and the
choice above inverts.

**Model ids are provider-namespaced** (``anthropic/claude-sonnet-5``,
``ollama/llama3.1:8b``), matching the convention ``sources.embedding_model``
already uses. Bare model names collide across providers and, worse, make a local
model indistinguishable from a hosted one — which is the difference between free
and not.

Prices are USD per million tokens, **as_of 2026-08-21**, transcribed from
Anthropic's published pricing. Nothing verifies them against a real invoice: no
Anthropic call has been made from this codebase yet (see README, "Verification
status"). Treat a figure here as a documented assumption, and confirm it against
the pricing page before anyone reports a number from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.llm.base import TokenUsage

#: Costs are quantised to this many decimal places. A single run lands in the
#: low cents, so cent precision would round most of the signal away — including
#: the entire cache saving, which is the thing this exists to make visible.
_PRECISION = Decimal("0.000001")

_PER_MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens, by token class.

    The four classes are priced separately because they differ by an order of
    magnitude in both directions: a cache read is a tenth of a fresh input
    token, a cache write is a quarter *more* than one. Collapsing them into a
    single input rate makes prompt caching look free when it is not, and makes
    the first (write) call look cheap when it is the expensive one.
    """

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal

    @classmethod
    def free(cls) -> Price:
        """A model that costs nothing to call — a local one.

        Distinct from an *absent* entry, which means unknown. Zero and unknown
        are both falsy and mean opposite things; see ``cost_usd``.
        """
        return cls(
            input=Decimal(0),
            output=Decimal(0),
            cache_read=Decimal(0),
            cache_write=Decimal(0),
        )


def _usd(
    *, input: str, output: str, cache_read: str, cache_write: str
) -> Price:
    """Build a Price from string literals.

    Strings, not floats: ``Decimal(0.30)`` is 0.29999999999999998889776975...,
    and the whole point of using Decimal here is not to do that.
    """
    return Price(
        input=Decimal(input),
        output=Decimal(output),
        cache_read=Decimal(cache_read),
        cache_write=Decimal(cache_write),
    )


#: Keyed by namespaced model id. Add a row when you point a setting at a new
#: model — an unpriced model reports ``None`` rather than guessing, which is
#: visible in the console as "unknown" instead of a wrong number.
PRICES: dict[str, Price] = {
    # Anthropic. Cache write is the 5-minute TTL rate; the 1-hour TTL costs
    # more, and this table does not model TTL because nothing here sets it.
    "anthropic/claude-opus-5": _usd(
        input="15.00", output="75.00", cache_read="1.50", cache_write="18.75"
    ),
    "anthropic/claude-sonnet-5": _usd(
        input="3.00", output="15.00", cache_read="0.30", cache_write="3.75"
    ),
    "anthropic/claude-haiku-4-5": _usd(
        input="1.00", output="5.00", cache_read="0.10", cache_write="1.25"
    ),
    # Local. Free in dollars, not in time — Ollama runs are the slow ones, and
    # a zero here should not be read as "no reason to care how many tokens".
    "ollama/llama3.1:8b": Price.free(),
    "ollama/llama3.1:70b": Price.free(),
    "ollama/mistral:7b": Price.free(),
    "ollama/qwen2.5:14b": Price.free(),
    "llamacpp/ternary-bonsai-2-27b": Price.free(),
}


def cost_usd(model: str, usage: TokenUsage) -> Decimal | None:
    """Price one model call, or ``None`` if the model is not in the table.

    ``None`` rather than ``Decimal(0)`` on an unknown model, and the distinction
    is load-bearing. A missing price is the normal state right after someone
    points ``SYNTHESIS_MODEL`` at something new, and that is exactly when a
    silent zero would be believed: the console would show a run that cost
    nothing, which is indistinguishable from a local run and wrong in the one
    direction that matters. Callers must decide what to do with the gap; they
    may not sum it.
    """
    price = PRICES.get(model)
    if price is None:
        return None

    total = (
        price.input * usage.input_tokens
        + price.output * usage.output_tokens
        + price.cache_read * usage.cache_read_tokens
        + price.cache_write * usage.cache_write_tokens
    ) / _PER_MILLION
    return total.quantize(_PRECISION)


def total_cost_usd(calls: list[tuple[str, TokenUsage]]) -> Decimal | None:
    """Sum several calls, or ``None`` if *any* model in the set is unpriced.

    All-or-nothing on purpose. A run whose extraction is priced and whose
    synthesis is not would otherwise report the extraction cost alone — a
    number that is both plausible and roughly 1% of the truth, which is worse
    than admitting ignorance. The per-stage figures stay visible either way, so
    nothing is hidden by refusing to add them up.
    """
    total = Decimal(0)
    priced_any = False
    for model, usage in calls:
        # A call that consumed nothing needs no price: the no-evidence branch
        # skips synthesis entirely, and demanding a price for a call that never
        # happened would blank the cost of an otherwise fully-priced run.
        if usage == TokenUsage():
            continue
        cost = cost_usd(model, usage)
        if cost is None:
            return None
        total += cost
        priced_any = True
    return total.quantize(_PRECISION) if priced_any else Decimal(0)
