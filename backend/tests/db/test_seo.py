"""What crawlers and link previews are served (needs a database).

The site is a client-rendered app, so a crawler that runs no scripts sees one
empty page for every address. The sitemap lists what is published, and
``/api/share/{slug}`` is a small real HTML page for an article that the proxy
hands to link-preview bots. Neither may show a draft. Rolled back after each test.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_session
from app.config import get_settings
from app.main import create_app
from tests.db.test_feed_search import _draft, _publish, _slug

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url


@pytest.fixture
async def session() -> Any:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no database at {DATABASE_URL}")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        try:
            yield db
        finally:
            await db.rollback()
    await engine.dispose()


@pytest.fixture
async def http(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> Any:
        yield session

    app.dependency_overrides[get_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_the_sitemap_lists_published_articles_and_no_drafts(
    http: httpx.AsyncClient, session: AsyncSession
) -> None:
    live = await _slug(session, await _publish(session))
    draft = await _slug(session, await _draft(session))

    response = await http.get("/api/sitemap.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    site = get_settings().public_site_url.rstrip("/")
    assert f"<loc>{site}/a/{live}</loc>" in response.text
    assert draft not in response.text


async def test_robots_points_at_the_sitemap_and_keeps_crawlers_off_the_desk(
    http: httpx.AsyncClient,
) -> None:
    text = (await http.get("/api/robots.txt")).text
    assert "Disallow: /review" in text
    assert f"Sitemap: {get_settings().public_site_url.rstrip('/')}/sitemap.xml" in text


async def test_the_share_page_carries_the_article_and_escapes_it(
    http: httpx.AsyncClient, session: AsyncSession
) -> None:
    slug = await _slug(
        session, await _publish(session, headline='Zinc & colds: "does it <work>"?')
    )

    response = await http.get(f"/api/share/{slug}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    escaped = "Zinc &amp; colds: &quot;does it &lt;work&gt;&quot;?"
    assert f'<meta property="og:title" content="{escaped}">' in response.text
    assert "<work>" not in response.text  # nothing from the article lands unescaped
    assert f"/a/{slug}" in response.text


async def test_a_draft_has_no_share_page(
    http: httpx.AsyncClient, session: AsyncSession
) -> None:
    slug = await _slug(session, await _draft(session))
    assert (await http.get(f"/api/share/{slug}")).status_code == 404


async def test_anonymous_reads_are_cacheable_for_a_minute(
    http: httpx.AsyncClient, session: AsyncSession
) -> None:
    slug = await _slug(session, await _publish(session))
    for path in ("/api/feed", "/api/feed/facets", f"/api/articles/{slug}"):
        headers = (await http.get(path)).headers
        assert headers["cache-control"] == "public, max-age=60", path
        assert "Authorization" in headers["vary"], path  # CORS adds Origin
    # A 404 is not: a just-published article must not be hidden by a cached miss.
    assert "cache-control" not in (await http.get("/api/articles/no-such-slug")).headers
