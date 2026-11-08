"""Executes one claimed collection run. Called from the pipeline worker's loop.

In the worker, not the API, for the reason research runs are: the reader's
model calls must never overlap a pipeline run's (llama-server cannot share the
GPU), and the weekly run's PubMed counts share NCBI's per-IP budget with it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.intel.reader import build_reader_call
from app.llm.base import LLMError
from app.llm.factory import build_generative_clients
from app.products.discover import discover_products
from app.research.factory import (
    YOUTUBE_RPS,
    build_news_provider,
    build_web_provider,
    source_client,
)
from app.research.providers.scientific import EuropePMCEvidenceProvider, PubMedEvidenceProvider
from app.research.providers.web_search import ddgs_region
from app.retrieval.factory import (
    build_europe_pmc_provider,
    build_http_client,
    build_pubmed_provider,
)
from app.social import service
from app.social.collect import collect_daily
from app.social.contracts import CollectionKind
from app.social.questions import build_question_call
from app.social.store import RunReport
from app.social.weekly import LiteratureCheck, analyse_week
from app.social.youtube import YouTubeClient, detect_youtube_quota

logger = logging.getLogger(__name__)

#: openFDA allows 240 requests a minute without a key.
OPENFDA_RPS = 2.0


def _literature_check(settings: Settings, http: httpx.AsyncClient) -> LiteratureCheck | None:
    try:
        extraction, _ = build_generative_clients(settings)
    except LLMError as exc:
        logger.warning("weekly run: no extraction client, literature is not counted: %s", exc)
        return None
    return LiteratureCheck(
        extraction,
        (
            PubMedEvidenceProvider(build_pubmed_provider(settings, http)),
            EuropePMCEvidenceProvider(build_europe_pmc_provider(settings, http)),
        ),
    )


async def execute_collection_run(
    run_id: str,
    label: str,
    kind: str,
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run it and record how it ended. Never raises."""
    report = RunReport()
    error: str | None = None
    now = datetime.now(UTC)
    http = build_http_client(settings)
    try:
        async with factory() as session:
            if kind == CollectionKind.SOCIAL_DAILY:
                client = (
                    YouTubeClient(
                        source_client(
                            settings,
                            http,
                            "youtube_social",
                            YOUTUBE_RPS,
                            detect=detect_youtube_quota,
                        ),
                        settings.youtube_api_key,
                    )
                    if settings.youtube_api_key
                    else None
                )
                await collect_daily(
                    session, report, client, run_id=run_id, settings=settings, now=now
                )
            elif kind == CollectionKind.SOCIAL_WEEKLY:
                await analyse_week(
                    session,
                    report,
                    read_call=build_reader_call(settings),
                    question_call=build_question_call(settings),
                    evidence=_literature_check(settings, http),
                    run_id=run_id,
                    settings=settings,
                    now=now,
                )
            elif kind == CollectionKind.PRODUCT_DISCOVERY:
                region = ddgs_region(settings.research_geo, settings.research_language)
                await discover_products(
                    session,
                    report,
                    read_call=build_reader_call(settings),
                    news=build_news_provider(settings, http, region),
                    web=build_web_provider(settings, http),
                    fda_http=source_client(settings, http, "openfda", OPENFDA_RPS),
                    settings=settings,
                    now=now,
                )
            else:
                error = f"unknown collection kind {kind!r}"
    except Exception as exc:
        logger.exception("collection run %s crashed", label)
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await http.aclose()
    try:
        await service.finish(
            factory,
            run_id,
            collection_status=report.collection_status(),
            stats=report.stats,
            notes=report.notes,
            error=error,
        )
    except Exception:
        logger.exception("could not record the end of collection run %s", label)
        return
    logger.info(
        "collection run %s %s: %s",
        label,
        "failed" if error else "completed",
        {name: log.status.value for name, log in report.sources.items()},
    )
