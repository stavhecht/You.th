"""Builds the enabled scholarly providers from config.

``settings.enabled_providers`` defaults to PubMed alone. That is Phase 1's
design, not an oversight: one API learned properly beats four half-integrated,
and PubMed is the one carrying study-type metadata.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.retrieval.base import ScholarlyProvider
from app.retrieval.providers import (
    EuropePMCProvider,
    OpenAlexProvider,
    SemanticScholarProvider,
)
from app.retrieval.pubmed import PubMedProvider, detect_throttle
from app.retrieval.throttle import (
    HttpClient,
    RateLimiter,
    ThrottledClient,
    ThrottleDetector,
)

logger = logging.getLogger(__name__)

USER_AGENT = "EvidWell/0.1 (evidence-checked wellness content)"

#: Requests per second, per provider, per *process*.
#:
#: Every published limit below is enforced per IP, while a limiter can only see
#: its own process — two workers on one host share the ceiling and neither
#: knows the other exists. These sit under the documented numbers to leave that
#: headroom. NCBI in particular answers sustained overage by blocking the
#: address, which is not a failure any retry recovers from.
PUBMED_ANONYMOUS_RPS = 2.0  # documented 3/s
PUBMED_KEYED_RPS = 7.0  # documented 10/s with an API key
EUROPE_PMC_RPS = 5.0
DOI_ORG_RPS = 5.0  # undocumented; the handle API is a public resolver
#: Semantic Scholar's keyed search endpoint is 1 req/s. It is the slowest of
#: the four by an order of magnitude, which is a reason to keep its result cap
#: low rather than a reason to exceed it.
SEMANTIC_SCHOLAR_RPS = 1.0
OPENALEX_RPS = 5.0  # documented 10/s in the polite pool


def build_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


def build_providers(
    settings: Settings, http: httpx.AsyncClient
) -> list[ScholarlyProvider]:
    """Instantiate each enabled provider behind its own throttle.

    Every provider gets a ``ThrottledClient`` wrapping the shared httpx client:
    connection pooling stays process-wide, pacing is per API. The limiter has
    to be constructed here rather than inside each provider because it is
    per-provider *shared* state — one instance paced by two limiters is two
    ceilings, and the sum is the one that reaches the API.

    Raises:
        ValueError: an unknown provider name — surfaced loudly rather than
            silently skipped, because a typo in config would otherwise look
            like "that source has no literature".
    """
    providers: list[ScholarlyProvider] = []
    for name in settings.enabled_providers:
        match name.strip().lower():
            case "pubmed":
                providers.append(build_pubmed_provider(settings, http))
            case "europe_pmc":
                providers.append(build_europe_pmc_provider(settings, http))
            case "semantic_scholar":
                api_key = settings.semantic_scholar_api_key or None
                if not api_key:
                    # Skipped here rather than returning [] from search(). An
                    # empty result from a provider that never made a request
                    # is indistinguishable from a successful search, and
                    # RetrieveStage's coverage check counts it as one — so a
                    # keyless Semantic Scholar would mask a claim whose only
                    # real provider was rate limited.
                    logger.warning(
                        "semantic_scholar is enabled but SEMANTIC_SCHOLAR_API_KEY is "
                        "unset; skipping it. Unauthenticated search 429s under any "
                        "real fan-out."
                    )
                    continue
                providers.append(
                    SemanticScholarProvider(
                        throttled_client(
                            settings, http, "semantic_scholar", SEMANTIC_SCHOLAR_RPS
                        ),
                        api_key,
                    )
                )
            case "openalex":
                if not settings.openalex_api_key:
                    logger.warning(
                        "openalex has no OPENALEX_API_KEY: keyless requests share a small "
                        "daily budget per IP (about 100 searches), after which every call "
                        "is refused until midnight UTC. Keys are free."
                    )
                providers.append(
                    OpenAlexProvider(
                        throttled_client(settings, http, "openalex", OPENALEX_RPS),
                        settings.openalex_mailto or None,
                        settings.openalex_api_key or None,
                    )
                )
            case unknown:
                raise ValueError(
                    f"unknown provider {unknown!r} in enabled_providers; expected "
                    "pubmed, europe_pmc, semantic_scholar or openalex"
                )

    if not providers:
        raise ValueError(
            f"no usable providers from enabled_providers={settings.enabled_providers!r}; "
            "retrieval would find nothing"
        )

    logger.info("retrieval providers: %s", ", ".join(p.source_api for p in providers))
    return providers


def build_pubmed_provider(settings: Settings, http: httpx.AsyncClient) -> PubMedProvider:
    """PubMed behind its own throttle, keyed or anonymous.

    Also used by the research agent, which counts papers per topic. Built per
    caller, so each gets its own limiter — safe only because the worker runs a
    research run and a pipeline run one after the other, never together.
    """
    return PubMedProvider(build_pubmed_client(settings, http), settings.pubmed_api_key or None)


def build_pubmed_client(settings: Settings, http: httpx.AsyncClient) -> HttpClient:
    """A throttled E-utilities client, keyed or anonymous.

    RETRIEVE's retraction screen uses its own: it runs after the stage's
    searches have returned, so the two limiters never pace requests at once.
    """
    api_key = settings.pubmed_api_key or None
    return throttled_client(
        settings,
        http,
        "pubmed",
        PUBMED_KEYED_RPS if api_key else PUBMED_ANONYMOUS_RPS,
        detect_throttle=detect_throttle,
    )


def build_europe_pmc_provider(
    settings: Settings, http: httpx.AsyncClient
) -> EuropePMCProvider:
    return EuropePMCProvider(throttled_client(settings, http, "europe_pmc", EUROPE_PMC_RPS))


def build_europe_pmc_client(settings: Settings, http: httpx.AsyncClient) -> HttpClient:
    """The throttled client behind the PMID lookup and the full-text client.

    One limiter for both, and separate from the Europe PMC *search* provider's.
    Safe because none of the three overlap: RETRIEVE fills in PMIDs after its
    searches have returned, FULL_TEXT runs after RETRIEVE has finished, and a
    worker runs one pipeline at a time.
    """
    return throttled_client(settings, http, "europe_pmc", EUROPE_PMC_RPS)


def build_doi_client(settings: Settings, http: httpx.AsyncClient) -> HttpClient:
    """The throttled client behind RETRIEVE's DOI registration check."""
    return throttled_client(settings, http, "doi_org", DOI_ORG_RPS)


def throttled_client(
    settings: Settings,
    http: httpx.AsyncClient,
    provider: str,
    per_second: float,
    detect_throttle: ThrottleDetector | None = None,
) -> HttpClient:
    return ThrottledClient(
        http,
        RateLimiter(per_second),
        provider=provider,
        max_retries=settings.provider_max_retries,
        max_wait_seconds=settings.provider_max_retry_wait_seconds,
        detect_throttle=detect_throttle,
    )
