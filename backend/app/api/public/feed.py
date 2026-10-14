"""Public feed endpoints. Read-only, unauthenticated.

The ``status = 'published'`` filter is not applied here — it lives in
``services/feed.py::_published_only`` so there is one place to audit and no way
for a new endpoint to forget it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text

from app.api.deps import OptionalReaderDep, SessionDep
from app.api.public.schemas import ArticleOut, FeedFacetsOut, FeedPageOut
from app.domain.enums import Subject, Verdict
from app.domain.models import Reader
from app.services.feed import ArticleNotFound, FeedService

router = APIRouter(tags=["public"])


@router.get("/feed", response_model=FeedPageOut)
async def get_feed(
    session: SessionDep,
    reader: OptionalReaderDep,
    cursor: Annotated[str | None, Query(description="Opaque cursor from next_cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 24,
    verdict: Annotated[Verdict | None, Query(description="Filter by verdict")] = None,
    subject: Annotated[Subject | None, Query(description="Filter by subject")] = None,
    personalise: Annotated[
        bool,
        Query(
            alias="personalise",
            description="Lift the signed-in reader's interests to the top",
        ),
    ] = True,
    q: Annotated[
        str | None,
        Query(max_length=200, description="Search published articles"),
    ] = None,
) -> FeedPageOut:
    """Cursor-paginated feed of published article cards.

    ``limit`` defaults to 24 so the masonry layout can fill several columns on
    a wide viewport without a second round trip on first paint.

    Works signed out, and that is the normal case — the token is optional and a
    stale one degrades to the anonymous feed rather than to a 401 (see
    ``optional_reader``). A signed-in reader's interests reorder the page; they
    never narrow it, so the two feeds hold the same articles in a different
    order. ``personalise=false`` is how the reader asks for the plain
    chronological feed without signing out.

    ``q`` turns the page into a search: ranked by relevance rather than date,
    narrowed by ``verdict`` and ``subject`` as the feed is, and never
    personalised (``FeedService.search`` says why). A blank ``q`` is the feed.
    """
    query = (q or "").strip()
    if query:
        try:
            found, next_cursor, match = await FeedService(session).search(
                query, cursor, limit, verdict, subject
            )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed cursor"
            ) from None
        return FeedPageOut(items=found, next_cursor=next_cursor, match=match)

    interests: list[Subject] = []
    if reader is not None and personalise:
        row = await session.get(Reader, reader.id)
        if row is not None:
            interests = list(row.interests)

    try:
        items, next_cursor = await FeedService(session).page(
            cursor, limit, verdict, subject, interests
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed cursor"
        ) from None
    return FeedPageOut(items=items, next_cursor=next_cursor)


@router.get("/feed/facets", response_model=FeedFacetsOut)
async def get_feed_facets(session: SessionDep) -> FeedFacetsOut:
    """Published counts per subject and per verdict, for the browse drawer.

    Declared **above** ``/articles/{slug}`` for no routing reason — it is a
    different prefix — but kept next to the feed it describes.
    """
    return FeedFacetsOut(**await FeedService(session).facets())


@router.get("/articles/{slug}", response_model=ArticleOut)
async def get_article(slug: str, session: SessionDep) -> ArticleOut:
    """Full article with citations and resolved sources.

    404 for any slug that is not published, including one that exists in
    ``pending_review`` — the response must not distinguish "no such article"
    from "not published yet".
    """
    try:
        article = await FeedService(session).article(slug)
    except ArticleNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Article not found"
        ) from None
    return ArticleOut(**article)


@router.get("/healthz")
async def healthz(session: SessionDep) -> dict[str, str]:
    """Liveness plus database reachability.

    A health check that only proves the process is up will report green while
    every request 500s on a dead connection pool.
    """
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
        ) from None
    return {"status": "ok"}
