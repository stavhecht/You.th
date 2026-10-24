"""The llama-server synthesis client: request shape, repair, and failures.

No server is needed; ``httpx.MockTransport`` answers in its place.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.domain.contracts import SynthesisInput
from app.llm.base import LLMError
from app.llm.llamacpp_client import (
    REASONING_ALLOWANCE,
    LlamaCppChat,
    LlamaCppSynthesisClient,
    ollama_unloader,
)
from app.llm.ollama_client import SYNTHESIS_MAX_TOKENS, SYNTHESIS_TEMPERATURE
from tests.conftest import make_draft


def _completion(content: str, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "choices": [
            {"message": {"content": content}, "finish_reason": finish_reason}
        ],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
    }


def _client(
    handler: Callable[[httpx.Request], httpx.Response], effort: str = "none"
) -> LlamaCppSynthesisClient:
    transport = httpx.AsyncClient(
        base_url="http://llama", transport=httpx.MockTransport(handler)
    )
    return LlamaCppSynthesisClient(LlamaCppChat(transport, "ternary-bonsai-2-27b", effort))


async def test_sends_the_grammar_and_the_pipelines_sampling(
    payload: SynthesisInput,
) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_completion(make_draft().model_dump_json()))

    result = await _client(handler).synthesize(payload)

    body = sent[0]
    assert body["response_format"]["type"] == "json_schema"
    assert "$defs" not in json.dumps(body["response_format"]["json_schema"]["schema"])
    assert body["temperature"] == SYNTHESIS_TEMPERATURE
    assert body["max_tokens"] == SYNTHESIS_MAX_TOKENS
    assert body["reasoning_effort"] == "none"
    assert result.model == "llamacpp/ternary-bonsai-2-27b"
    assert result.usage.input_tokens == 1200


async def test_thinking_widens_the_output_cap(payload: SynthesisInput) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_completion(make_draft().model_dump_json()))

    await _client(handler, effort="medium").synthesize(payload)

    assert sent[0]["max_tokens"] == SYNTHESIS_MAX_TOKENS + REASONING_ALLOWANCE


async def test_a_contract_break_gets_one_repair_turn(payload: SynthesisInput) -> None:
    broken = make_draft().model_dump(mode="json")
    broken["headline"] = " ".join(["word"] * 30)
    answers = [json.dumps(broken), make_draft().model_dump_json()]
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_completion(answers[len(sent) - 1]))

    result = await _client(handler).synthesize(payload)

    assert len(sent) == 2
    assert "broke these constraints" in sent[1]["messages"][-1]["content"]
    # Both attempts' tokens are counted.
    assert result.usage.output_tokens == 600


async def test_a_truncated_answer_is_named_as_one(payload: SynthesisInput) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion('{"headline": "Cut', "length"))

    with pytest.raises(LLMError, match="max_tokens") as raised:
        await _client(handler).synthesize(payload)
    assert raised.value.usage.output_tokens == 300
    assert not raised.value.retryable


async def test_a_server_error_is_retryable(payload: SynthesisInput) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="loading model")

    with pytest.raises(LLMError) as raised:
        await _client(handler).synthesize(payload)
    assert raised.value.retryable


async def test_room_is_made_before_every_call(payload: SynthesisInput) -> None:
    order: list[str] = []

    async def make_room() -> None:
        order.append("make_room")

    def handler(request: httpx.Request) -> httpx.Response:
        order.append("synthesis")
        return httpx.Response(200, json=_completion(make_draft().model_dump_json()))

    transport = httpx.AsyncClient(
        base_url="http://llama", transport=httpx.MockTransport(handler)
    )
    client = LlamaCppSynthesisClient(LlamaCppChat(transport, "m", make_room=make_room))
    await client.synthesize(payload)
    await client.synthesize(payload, feedback="again")

    assert order == ["make_room", "synthesis", "make_room", "synthesis"]


class _FakeOllama:
    def __init__(self, fail: bool = False) -> None:
        self.unloaded: list[tuple[str, Any]] = []
        self._fail = fail

    async def ps(self) -> Any:
        if self._fail:
            raise ConnectionError("ollama is down")

        class _Model:
            def __init__(self, name: str) -> None:
                self.model = name
                self.name = name

        class _Running:
            def __init__(self) -> None:
                self.models = [_Model("llama3.1:8b"), _Model("mxbai-embed-large:latest")]

        return _Running()

    async def generate(self, *, model: str, prompt: str, keep_alive: Any) -> None:
        self.unloaded.append((model, keep_alive))


async def test_the_unloader_keeps_the_embedding_model() -> None:
    ollama = _FakeOllama()
    await ollama_unloader(ollama, keep=frozenset({"mxbai-embed-large"}))()  # type: ignore[arg-type]
    assert ollama.unloaded == [("llama3.1:8b", 0)]


async def test_a_failed_unload_never_fails_synthesis() -> None:
    await ollama_unloader(_FakeOllama(fail=True))()  # type: ignore[arg-type]


async def test_an_empty_answer_names_gpu_memory_and_is_retryable(
    payload: SynthesisInput,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(""))

    with pytest.raises(LLMError, match="GPU memory") as raised:
        await _client(handler).synthesize(payload)
    assert raised.value.retryable


async def test_a_compute_error_says_the_server_needs_a_restart(
    payload: SynthesisInput,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, json={"error": {"code": 500, "message": "Compute error."}}
        )

    with pytest.raises(LLMError, match="restarted") as raised:
        await _client(handler).synthesize(payload)
    assert raised.value.retryable
