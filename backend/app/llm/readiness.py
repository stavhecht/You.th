"""Can the model servers a run would use answer right now?

The worker asks before it claims, because ``attempts`` is counted at claim: a
stopped llama-server failed each queued run three times in six minutes and left
them dead for good (2026-11-05), when all they needed was to wait.

llama-server gets a real completion because a dead one looks healthy: ``/health``
says ok while every completion fails (see ``llama-server/watchdog.sh``, whose
request this copies). Ollama is only asked if it is up, since a completion would
load a language model, which cannot share the GPU with llama-server. For the same
reason Ollama's language models are unloaded before that completion, as before any
other, but only if the server is up: it may be stopped so the eval's judge can use
Ollama, and this runs every 30 s while work waits.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

from app.config import Settings

#: The first completion after a long idle took 23 s while macOS paged the weights in.
PROBE_TIMEOUT_SECONDS = 60.0

_SAY_OK = {
    "messages": [{"role": "user", "content": "Say OK."}],
    "max_tokens": 4,
    "reasoning_effort": "none",
    "chat_template_kwargs": {"reasoning_effort": "none"},
}


async def model_down(
    settings: Settings,
    http: httpx.AsyncClient | None = None,
    *,
    make_room: Callable[[], Awaitable[None]] | None = None,
) -> str | None:
    """Why a run could not reach its models, or None when it could. Never raises for
    a server that is down. Hosted providers are not probed: a call to one costs money."""
    if http is None:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as own:
            return await model_down(settings, own, make_room=make_room)
    llm = settings.llm_provider.lower()
    if llm == "llamacpp":
        reason = await _llama_server_down(settings.llamacpp_base_url, http, make_room)
        if reason:
            return reason
    if "ollama" in (llm, settings.embedding_provider.lower()):
        return await _ollama_down(settings.ollama_base_url, http)
    return None


async def _llama_server_down(
    base_url: str,
    http: httpx.AsyncClient,
    make_room: Callable[[], Awaitable[None]] | None,
) -> str | None:
    try:
        (await http.get(f"{base_url}/health")).raise_for_status()
        if make_room is not None:
            await make_room()
        response = await http.post(f"{base_url}/v1/chat/completions", json=_SAY_OK)
        response.raise_for_status()
        if not response.json()["choices"][0]["message"]["content"]:
            return "llama-server gave an empty answer"
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        return f"llama-server did not complete a request: {exc!r}"
    return None


async def _ollama_down(base_url: str, http: httpx.AsyncClient) -> str | None:
    try:
        (await http.get(f"{base_url}/api/version")).raise_for_status()
    except httpx.HTTPError as exc:
        return f"Ollama did not answer: {exc!r}"
    return None
