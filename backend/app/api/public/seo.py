"""What crawlers and link previews read. Public, read-only.

The site is a client-rendered app: a crawler that runs no scripts gets the same
empty shell for every address. Three small answers to that, none of which
changes what a reader sees:

* ``/sitemap.xml`` lists every published article.
* ``/robots.txt`` points at it and keeps crawlers off the review desk.
* ``/share/{slug}`` is a real HTML page for one article, with the title,
  summary and picture a link preview shows. The proxy in front of the site
  sends link-preview bots here for ``/a/{slug}`` (see ``ops/Caddyfile``);
  a person who lands on it is sent on to the article.

All three go through ``FeedService``, so the ``published`` filter is the same
one the feed uses.
"""

from __future__ import annotations

import json
from html import escape
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, HTTPException, Response, status

from app.api.deps import SessionDep, SettingsDep
from app.services.feed import ArticleNotFound, FeedService

router = APIRouter(tags=["public"])


@router.get("/sitemap.xml", response_class=Response)
async def sitemap(session: SessionDep, settings: SettingsDep) -> Response:
    site = settings.public_site_url.rstrip("/")
    urls = "".join(
        f"<url><loc>{xml_escape(f'{site}/a/{slug}')}</loc>"
        f"<lastmod>{changed.date().isoformat()}</lastmod></url>"
        for slug, changed in await FeedService(session).sitemap()
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{xml_escape(site)}/</loc></url>{urls}</urlset>"
    )
    return Response(body, media_type="application/xml")


@router.get("/robots.txt", response_class=Response)
async def robots(settings: SettingsDep) -> Response:
    site = settings.public_site_url.rstrip("/")
    body = (
        "User-agent: *\nDisallow: /review\nDisallow: /api/console\n\n"
        f"Sitemap: {site}/sitemap.xml\n"
    )
    return Response(body, media_type="text/plain")


@router.get("/share/{slug}", response_class=Response)
async def share(slug: str, session: SessionDep, settings: SettingsDep) -> Response:
    """One article as plain HTML with link-preview tags. 404 unless published."""
    try:
        article = await FeedService(session).article(slug)
    except ArticleNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Article not found") from None

    site = settings.public_site_url.rstrip("/")
    url = f"{site}/a/{article['slug']}"
    title, summary = escape(article["headline"]), escape(article["summary"])
    image = await FeedService(session).card_image(slug)
    image_tags = (
        f'<meta property="og:image" content="{escape(site + image)}">'
        '<meta name="twitter:card" content="summary_large_image">'
        if image
        else '<meta name="twitter:card" content="summary">'
    )
    # schema.org, for search results. `<` is written as \u003c so nothing in a
    # headline can close the script element early.
    linked_data = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": article["headline"],
            "description": article["summary"],
            "datePublished": article["published_at"].isoformat(),
            "mainEntityOfPage": url,
            "publisher": {"@type": "Organization", "name": "You.th"},
        }
    ).replace("<", "\\u003c")
    body = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{title} · You.th</title>"
        f'<meta name="description" content="{summary}">'
        f'<link rel="canonical" href="{escape(url)}">'
        '<meta property="og:type" content="article">'
        '<meta property="og:site_name" content="You.th">'
        f'<meta property="og:title" content="{title}">'
        f'<meta property="og:description" content="{summary}">'
        f'<meta property="og:url" content="{escape(url)}">'
        f"{image_tags}"
        f'<script type="application/ld+json">{linked_data}</script>'
        f'<meta http-equiv="refresh" content="0; url={escape(url)}">'
        f"</head><body><h1>{title}</h1><p>{summary}</p>"
        f'<p><a href="{escape(url)}">Read the article on You.th</a></p></body></html>'
    )
    return Response(body, media_type="text/html")
