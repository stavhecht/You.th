"""The Ollama client's one structured turn: validation repair and failure classes.

Ollama is the default generative provider. Its ``format`` is a decoding
grammar, not a validator, so the contract's own limits (12-word headline,
sentence caps) are checked here and a breach gets exactly one repair turn.
And every failure must be classified right, because the orchestrator acts on
it: an outage is retried with a backoff, a broken contract or a missing model
fails for good — retrying would send the identical request.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from ollama import ResponseError

from app.domain.contracts import ExtractionInput, ExtractionOutput
from app.llm.base import LLMError
from app.llm.ollama_client import OllamaExtractionClient, _chat

VALID = {"product": "Creatine", "target_claims": ["improves strength"], "ingredients": []}
INVALID = {"product": "Creatine", "target_claims": [], "ingredients": []}  # min_length=1


class _Ollama:
    """Answers ``chat`` from a script: dicts become responses, exceptions raise."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(step)),
            prompt_eval_count=100,
            eval_count=20,
        )


async def _turn(client: _Ollama) -> Any:
    return await _chat(
        client,  # type: ignore[arg-type]
        call="extraction",
        model="llama3.1:8b",
        system="system prompt",
        user="user prompt",
        output_format=ExtractionOutput,
        num_ctx=4096,
        max_tokens=256,
        temperature=0.0,
    )


# --- the happy path and the repair turn ----------------------------------------


async def test_a_valid_answer_is_returned_with_its_usage_and_namespaced_model() -> None:
    client = _Ollama(VALID)
    result = await _turn(client)
    assert result.output.target_claims == ["improves strength"]
    assert (result.usage.input_tokens, result.usage.output_tokens) == (100, 20)
    assert result.model == "ollama/llama3.1:8b"  # the price table's key
    assert len(client.calls) == 1
    assert (
        client.calls[0]["options"]["num_ctx"] == 4096
    )  # never the 4K server default by accident


async def test_a_broken_answer_gets_one_repair_turn_naming_the_field() -> None:
    client = _Ollama(INVALID, VALID)
    result = await _turn(client)

    assert result.output.target_claims == ["improves strength"]
    repair = client.calls[1]["messages"]
    assert [m["role"] for m in repair] == ["system", "user", "assistant", "user"]
    assert repair[2]["content"] == json.dumps(INVALID)  # shown its own answer
    assert "target_claims" in repair[3]["content"]  # told exactly which field
    assert result.usage.input_tokens == 200  # both attempts were paid for


async def test_still_broken_after_the_repair_is_a_permanent_failure_that_keeps_its_cost() -> (
    None
):
    client = _Ollama(INVALID, INVALID)
    with pytest.raises(LLMError) as raised:
        await _turn(client)
    assert raised.value.retryable is False
    assert raised.value.usage is not None and raised.value.usage.input_tokens == 200
    assert raised.value.model == "ollama/llama3.1:8b"
    assert len(client.calls) == 2  # never a third


async def test_non_json_output_is_repaired_like_any_other_breach() -> None:
    client = _Ollama("not an object", VALID)  # valid JSON, wrong shape
    result = await _turn(client)
    assert result.output.product == "Creatine"


# --- failure classes ---------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (ResponseError("model 'x' not found", 404), False),  # missing model: pull it
        (ResponseError("bad request", 400), False),
        (ResponseError("overloaded", 429), True),
        (ResponseError("internal error", 500), True),
        (ResponseError("unavailable", 503), True),
        (ConnectionError("connection refused"), True),
        (httpx.ReadTimeout("timed out"), True),
    ],
)
async def test_each_failure_is_classified_for_the_orchestrator(
    error: BaseException, retryable: bool
) -> None:
    with pytest.raises(LLMError) as raised:
        await _turn(_Ollama(error))
    assert raised.value.retryable is retryable


async def test_a_missing_model_says_how_to_fix_it() -> None:
    with pytest.raises(LLMError, match=r"ollama pull llama3\.1:8b"):
        await _turn(_Ollama(ResponseError("not found", 404)))


async def test_the_extraction_client_sends_the_topic_and_parses_the_answer() -> None:
    client = _Ollama(VALID)
    result = await OllamaExtractionClient(client).extract(  # type: ignore[arg-type]
        ExtractionInput(topic="Creatine for strength")
    )
    assert result.output.product == "Creatine"
    sent = "\n".join(m["content"] for m in client.calls[0]["messages"])
    assert "Creatine for strength" in sent
