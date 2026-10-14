"""FastAPI application factory.

Two surfaces mounted on one app, separated by prefix and by auth posture:

    /api/*                 public, unauthenticated, read-only
    /api/console/auth/*    unauthenticated (login must be reachable)
    /api/console/*         JWT required, enforced router-level

The separation is structural rather than conventional — public handlers live in
``api/public`` and cannot acquire an authenticated reviewer, console handlers
live behind a router-level dependency and cannot lose one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import automation
from app.api.console import research as console_research
from app.api.console import routes as console_routes
from app.api.public import contact as public_contact
from app.api.public import feed as public_feed
from app.api.public import media as public_media
from app.api.public import readers as public_readers
from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.discovery.schedule import ScanScheduler
from app.logging_setup import configure_logging
from app.security.auth import AuthError
from app.services.reader import ReaderError
from app.services.review import ReviewError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()

    # Fail fast on an embedding-width mismatch rather than at the first
    # pgvector write. Skipped when a hosted provider has no key configured, so
    # the API can serve the public feed without embedding credentials. Ollama
    # needs no key, and building its provider makes no network call — only the
    # width is asserted here, so a stopped `ollama serve` does not block the API.
    is_local = settings.embedding_provider.lower() == "ollama"
    if is_local or settings.voyage_api_key or settings.openai_api_key:
        from app.llm.embeddings.factory import build_embedding_provider

        build_embedding_provider(settings)
    else:
        logger.warning(
            "no embedding provider key configured; the pipeline will not run "
            "(the public feed and console are unaffected)"
        )

    # Fills the trend desk on a timer so nobody has to press the button. Started
    # here rather than in the worker because `manual_scan` — the single-flight
    # runner it shares with the console's button — lives in this process, and
    # two runners would be two guards. See `discovery/schedule.py`.
    scheduler = ScanScheduler(settings, get_session_factory())
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(logging.DEBUG if settings.debug else logging.INFO)

    app = FastAPI(title="You.th API", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Three headers every API response can carry without breaking anything.

        ``nosniff`` matters most here: ``/api/media`` serves stored bytes, and
        the store decides their type from magic bytes so the browser must not
        second-guess it. Nothing under ``/api`` is meant to be framed, and no
        URL here should leak to a third party as a referrer.

        A Content-Security-Policy is deliberately not set: these responses are
        JSON and images, and the policy that matters is the SPA's, which is
        served by whatever hosts ``frontend/dist`` in production — not this app.
        """
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    app.include_router(public_feed.router, prefix="/api")                      # app client
    app.include_router(public_readers.router, prefix="/api")                  # accounts
    app.include_router(public_contact.router, prefix="/api")                  # let us know
    # Stored images, read from `media_objects`. A router rather than the
    # StaticFiles mount this replaced, because the bytes are in the database —
    # see services/media.py. Unauthenticated by design: these images are
    # embedded in published articles, so every reader fetches them. Uploading
    # is the console-only half and lives on the authenticated router below.
    #
    # Under /api rather than a /media of its own because the dev proxy and any
    # deployment already route that prefix — media needs no new rule in either
    # place, and article documents hold these paths verbatim.
    app.include_router(public_media.router, prefix="/api")                     # images
    app.include_router(console_routes.auth_router, prefix="/api/console")     # login
    app.include_router(console_routes.router, prefix="/api/console")           # reviewer
    app.include_router(console_research.router, prefix="/api/console")        # research
    # n8n. Shared-secret auth, and 404 throughout while no token is configured.
    app.include_router(automation.router, prefix="/api")                       # n8n

    @app.exception_handler(ReviewError)
    async def _review_error(_: Request, exc: ReviewError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)}
        )

    @app.exception_handler(ReaderError)
    async def _reader_error(_: Request, exc: ReaderError) -> JSONResponse:
        # Same treatment as ReviewError: an illegal operation on your own data
        # is a conflict, and the message is safe to show because it can only
        # describe the caller's own account.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)}
        )

    @app.exception_handler(AuthError)
    async def _auth_error(_request: Request, _exc: AuthError) -> JSONResponse:
        # Deliberately generic: never tell the client which half of the
        # credential was wrong.
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Not authenticated"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    return app


app = create_app()
