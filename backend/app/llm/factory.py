"""Selects the generative provider from config.

The mirror of ``llm/embeddings/factory.py``, and the only place that knows which
concrete client backs the two Protocols in ``llm/base.py``. Everything
downstream — stages, orchestrator, tests — depends on the Protocols, so swapping
providers is an env var:

    LLM_PROVIDER=ollama      # local, free; the current default
    LLM_PROVIDER=anthropic   # hosted Claude; what real deployment runs
    LLM_PROVIDER=llamacpp    # local llama-server (Ternary-Bonsai-2-27B)

All three paths stay live and both are exercised by the same call sites. The
Anthropic client is not commented-out code waiting to be revived — it is one
setting away, which is the point.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.llm.anthropic_client import (
    AnthropicAppraisalClient,
    AnthropicExtractionClient,
    AnthropicSynthesisClient,
    build_anthropic_client,
)
from app.llm.base import AppraisalClient, ExtractionClient, LLMError, SynthesisClient
from app.llm.llamacpp_client import (
    LlamaCppAppraisalClient,
    LlamaCppChat,
    LlamaCppExtractionClient,
    LlamaCppSynthesisClient,
    build_llamacpp_client,
    ollama_unloader,
)
from app.llm.ollama_client import (
    OllamaAppraisalClient,
    OllamaExtractionClient,
    OllamaSynthesisClient,
    build_ollama_client,
)

logger = logging.getLogger(__name__)


def build_generative_clients(
    settings: Settings,
) -> tuple[ExtractionClient, SynthesisClient]:
    """Construct the extraction and synthesis clients for the configured provider.

    Both share one transport, as they did before this seam existed: two clients,
    one connection pool.

    Raises:
        LLMError: unknown provider name, or a hosted provider with no API key.
    """
    name = settings.llm_provider.lower()

    if name == "ollama":
        client = build_ollama_client(
            settings.ollama_base_url, settings.ollama_timeout_seconds
        )
        logger.info(
            "generative provider: ollama at %s (extraction=%s, synthesis=%s)",
            settings.ollama_base_url,
            settings.ollama_extraction_model,
            settings.ollama_synthesis_model,
        )
        return (
            OllamaExtractionClient(client, settings.ollama_extraction_model),
            OllamaSynthesisClient(client, settings.ollama_synthesis_model),
        )

    if name == "llamacpp":
        chat = llamacpp_chat(settings)
        logger.info(
            "generative provider: llama-server at %s (%s, reasoning=%s)",
            settings.llamacpp_base_url,
            settings.llamacpp_model,
            settings.llamacpp_reasoning_effort,
        )
        return LlamaCppExtractionClient(chat), LlamaCppSynthesisClient(chat)

    if name == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set (llm_provider='anthropic')")
        anthropic_client = build_anthropic_client(settings.anthropic_api_key)
        logger.info(
            "generative provider: anthropic (extraction=%s, synthesis=%s)",
            settings.extraction_model,
            settings.synthesis_model,
        )
        return (
            AnthropicExtractionClient(anthropic_client, settings.extraction_model),
            AnthropicSynthesisClient(anthropic_client, settings.synthesis_model),
        )

    raise LLMError(
        f"unknown llm_provider {settings.llm_provider!r}; "
        "expected 'ollama', 'anthropic' or 'llamacpp'"
    )


def build_appraisal_client(settings: Settings) -> AppraisalClient:
    """The client for APPRAISE, on the configured provider.

    A function of its own rather than a third element of
    ``build_generative_clients``'s tuple, so callers that only need the two
    original calls are unchanged.
    It opens its own transport; one more connection pool is cheap next to a
    model call. An empty model setting means the synthesis model.

    Raises:
        LLMError: unknown provider name, or a hosted provider with no API key.
    """
    name = settings.llm_provider.lower()

    if name == "ollama":
        model = settings.ollama_appraisal_model or settings.ollama_synthesis_model
        logger.info("appraisal: ollama (%s)", model)
        return OllamaAppraisalClient(
            build_ollama_client(settings.ollama_base_url, settings.ollama_timeout_seconds),
            model,
        )

    if name == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set (llm_provider='anthropic')")
        model = settings.appraisal_model or settings.synthesis_model
        logger.info("appraisal: anthropic (%s)", model)
        return AnthropicAppraisalClient(
            build_anthropic_client(settings.anthropic_api_key), model
        )

    if name == "llamacpp":
        logger.info("appraisal: llama-server (%s)", settings.llamacpp_model)
        return LlamaCppAppraisalClient(llamacpp_chat(settings))

    raise LLMError(
        f"unknown llm_provider {settings.llm_provider!r}; "
        "expected 'ollama', 'anthropic' or 'llamacpp'"
    )


def llamacpp_chat(settings: Settings) -> LlamaCppChat:
    """The llama-server transport, which unloads Ollama's language models first.

    The embedding model is kept: it fits beside Bonsai, and RETRIEVE needs it.
    """
    embedding = (
        frozenset({settings.ollama_embedding_model})
        if settings.embedding_provider.lower() == "ollama"
        else frozenset()
    )
    return LlamaCppChat(
        build_llamacpp_client(settings.llamacpp_base_url),
        settings.llamacpp_model,
        settings.llamacpp_reasoning_effort,
        make_room=ollama_unloader(
            build_ollama_client(settings.ollama_base_url, settings.ollama_timeout_seconds),
            keep=embedding,
        ),
    )
