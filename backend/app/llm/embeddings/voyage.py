"""Voyage AI embedding provider — the MVP default.

Chosen for scientific-text performance per dollar, which matters because the
``sources`` cache is designed to grow into thousands of embedded abstracts.

Swapping to OpenAI is a config change (``settings.embedding_provider``), but
note it is not free at runtime: a different model means a different vector
space, so an existing cache must be re-embedded. See ``openai.py``.
"""

from __future__ import annotations

import logging

from voyageai.client_async import AsyncClient

from app.llm.embeddings.base import EmbeddingError

logger = logging.getLogger(__name__)

VOYAGE_MODEL = "voyage-4"
VOYAGE_DIMENSION = 1024

# Voyage caps documents per request; abstracts are short enough that the batch
# limit binds before the token limit.
MAX_BATCH_SIZE = 128


class VoyageEmbeddingProvider:
    def __init__(self, client: AsyncClient, model: str = VOYAGE_MODEL) -> None:
        self._client = client
        self._model = model

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return VOYAGE_DIMENSION

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed abstracts for storage.

        Order preservation is not incidental — callers zip the result against
        the candidate list, so a reordered response would attach every
        embedding to the wrong paper. Batches are concatenated in input order
        and the total is asserted before returning.
        """
        if not texts:
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_BATCH_SIZE):
            batch = texts[start : start + MAX_BATCH_SIZE]
            try:
                result = await self._client.embed(
                    batch, model=self._model, input_type="document"
                )
            except Exception as exc:  # voyageai raises bare exceptions
                raise EmbeddingError(f"voyage embed_documents failed: {exc}") from exc
            # The SDK types embeddings as list[float] | list[int]; coerce so the
            # vector written to pgvector has one consistent element type.
            vectors.extend([float(value) for value in vector] for vector in result.embeddings)

        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"voyage returned {len(vectors)} vectors for {len(texts)} inputs; "
                "refusing to return a misaligned batch"
            )
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """Embed a claim for search.

        ``input_type="query"`` rather than ``"document"``: Voyage embeds the
        two asymmetrically and it measurably improves retrieval. Do not
        collapse this into ``embed_documents``.
        """
        try:
            result = await self._client.embed([text], model=self._model, input_type="query")
        except Exception as exc:
            raise EmbeddingError(f"voyage embed_query failed: {exc}") from exc
        return [float(value) for value in result.embeddings[0]]


def build_voyage_provider(api_key: str) -> VoyageEmbeddingProvider:
    return VoyageEmbeddingProvider(AsyncClient(api_key=api_key))
