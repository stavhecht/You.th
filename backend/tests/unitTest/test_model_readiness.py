"""``model_down`` asks the servers a run would use, and never raises."""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.llm.readiness import model_down


def _settings(**values: str) -> Settings:
    return Settings(**{"llm_provider": "llamacpp", "embedding_provider": "ollama", **values})


def _answers(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _completion(text: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


async def test_both_servers_answering_is_not_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _completion("OK") if "completions" in request.url.path else httpx.Response(200)

    async with _answers(handler) as http:
        assert await model_down(_settings(), http) is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"error": {"message": "Loading model"}}),
        # "A dead server looks healthy": /health says ok while completions fail.
        httpx.Response(500, json={"error": {"message": "Compute error"}}),
        _completion(""),
    ],
)
async def test_llama_server_counts_as_down_unless_it_really_completes(
    response: httpx.Response,
) -> None:
    async with _answers(lambda request: response) as http:
        reason = await model_down(_settings(), http)
    assert reason is not None and "llama-server" in reason


async def test_a_refused_connection_is_down_and_does_not_raise() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed")

    async with _answers(refuse) as http:
        assert "llama-server" in (await model_down(_settings(), http) or "")


async def test_a_dead_embedder_counts_too() -> None:
    """RETRIEVE turns an embedding failure into a retryable stage failure as well."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            raise httpx.ConnectError("refused")
        return _completion("OK")

    async with _answers(handler) as http:
        assert "Ollama" in (await model_down(_settings(), http) or "")


async def test_ollama_is_unloaded_before_the_probe_as_before_any_completion() -> None:
    """Both resident, llama-server fails every request until it is restarted."""
    order: list[str] = []

    async def make_room() -> None:
        order.append("unload")

    def handler(request: httpx.Request) -> httpx.Response:
        order.append(request.url.path)
        return _completion("OK") if "completions" in request.url.path else httpx.Response(200)

    async with _answers(handler) as http:
        assert await model_down(_settings(), http, make_room=make_room) is None
    assert order.index("unload") < order.index("/v1/chat/completions")


async def test_a_stopped_server_does_not_unload_ollama() -> None:
    """It may be stopped so the eval's judge can use Ollama; a hold probes every 30 s."""

    async def make_room() -> None:
        raise AssertionError("unloaded Ollama for a server that is not running")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with _answers(refuse) as http:
        reason = await model_down(_settings(), http, make_room=make_room)
    assert "llama-server" in (reason or "")


async def test_a_hosted_setup_is_never_probed() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"probed {request.url}")

    settings = _settings(llm_provider="anthropic", embedding_provider="voyage")
    async with _answers(fail) as http:
        assert await model_down(settings, http) is None
