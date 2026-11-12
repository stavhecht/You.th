"""Weekly product discovery: find product names in what is already flowing, then confirm.

1. **Texts.** The publisher-feed items the trend search already cached
   (``research_feed_state.items`` — read only, no request), plus one news
   search per product category (``product_max_news_queries``). Mentions found
   in social posts are recorded by the weekly social run, from the same reading.
2. **Read** with the shared reader (``intel/reader.py``): cached, batched,
   quote-checked. A word filter drops texts with no wellness or product words
   before the model sees them.
3. **Record** each mention once per source, products deduplicated by key.
4. **Confirm** the unconfirmed products checked longest ago, whichever run found
   them, at most ``product_max_confirmations``: an official page is a web result
   on the brand's own domain; an FDA record is a 510(k) whose applicant is the
   brand. Neither is ever inferred from the mentions themselves.

A mention is never a launch by itself (``products/resolve.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain.models import Product, ResearchFeedState
from app.intel.reader import ReadCall, read_texts, worth_reading
from app.products.contracts import CATEGORY_INFO, FDA_CATEGORIES, ProductCategory
from app.products.fda import lookup_510k
from app.products.resolve import is_official_url, is_product_page
from app.products.store import Mention, record_mentions, refresh_statuses
from app.research.providers.base import NewsProvider, ProviderUnavailable, WebSearchProvider
from app.research.providers.web_search import ddgs_region
from app.research.scoring import mentions
from app.retrieval.throttle import HttpClient
from app.social.contracts import CollectionError, CollectionStatus, SourceLog
from app.social.store import RunReport

NEWS_LOOKBACK_DAYS = 7


def provider_error(exc: ProviderUnavailable) -> CollectionError:
    """The research providers' one exception, in the collection vocabulary."""
    text = str(exc)
    lowered = text.lower()
    # "ratelimit"/"rate limit", never a bare "rate": that is in "generate".
    if "429" in text or "ratelimit" in lowered.replace(" ", "") or "quota" in lowered:
        return CollectionError(CollectionStatus.RATE_LIMITED, text)
    if exc.permanent:
        return CollectionError(CollectionStatus.UNAVAILABLE, text)
    return CollectionError(CollectionStatus.ERROR, text)


async def discover_products(
    session: AsyncSession,
    report: RunReport,
    *,
    read_call: ReadCall | None,
    news: NewsProvider,
    web: WebSearchProvider,
    fda_http: HttpClient,
    settings: Settings,
    now: datetime,
) -> None:
    feeds, news_log = SourceLog(), SourceLog()
    report.sources = {"feeds": feeds, "news": news_log}
    texts: dict[str, str] = {}
    origins: dict[str, tuple[str, str | None, datetime | None]] = {}

    feed_rows = (
        await session.execute(select(ResearchFeedState.feed_url, ResearchFeedState.items))
    ).all()
    feed_items = 0
    for feed_url, items in feed_rows:
        for item in items or []:
            url, title = item.get("link"), item.get("title")
            if not url or not title:
                continue
            texts[url] = f"{title}\n{item.get('summary') or ''}".strip()
            publisher = item.get("publisher") or urlsplit(feed_url).hostname
            origins[url] = ("rss", publisher, _parse(item.get("published")))
            feed_items += 1
    if feed_rows:
        feeds.ok(feed_items)
    else:
        feeds.detail = "no feed items cached yet (the trend search fills them)"

    today = now.date()
    categories = [c for c in ProductCategory if c in CATEGORY_INFO]
    for category in categories[: settings.product_max_news_queries]:
        if news_log.stopped:
            break
        phrase = CATEGORY_INFO[category][0]
        try:
            found = await news.search(
                [phrase],
                since=today - timedelta(days=NEWS_LOOKBACK_DAYS),
                until=today,
                language=settings.research_language,
            )
        except ProviderUnavailable as exc:
            news_log.failed(provider_error(exc), f"news {phrase!r}")
            continue
        news_log.ok(len(found.articles))
        for article in found.articles:
            texts.setdefault(article.url, article.title)
            origins.setdefault(article.url, ("news", article.source, article.published_at))

    wanted = {key: text for key, text in texts.items() if worth_reading(text)}
    # News first: we searched for it this run, and the feed backlog alone fills the budget.
    # Dev, 2026-11-07: 301 feed items still unread and no news text ever reached the reader.
    wanted = dict(sorted(wanted.items(), key=lambda item: origins[item[0]][0] != "news"))
    result = await read_texts(
        session, read_call, wanted, max_batches=settings.social_max_reading_batches
    )
    report.sources["reader"] = result.log(configured=read_call is not None)
    mentions = [
        Mention(
            **product,
            source_kind=origins[key][0],
            source_url=key,
            publisher=origins[key][1],
            published_at=origins[key][2],
            text=texts[key],
        )
        for key, reading in result.readings.items()
        for product in reading.get("products") or []
    ]
    touched = await record_mentions(session, mentions, now=now)
    await refresh_statuses(session, touched, now=now)
    await session.commit()

    checked = await _confirm(
        session, report, web=web, fda_http=fda_http, settings=settings, now=now
    )
    await refresh_statuses(session, checked, now=now)
    await session.commit()
    report.stats.update(
        texts=len(texts),
        read=len(result.readings),
        reading_backlog=result.backlog,
        mentions=len(mentions),
        products_touched=len(touched),
        products_checked=len(checked),
    )
    if result.backlog:
        report.notes.append(f"{result.backlog} texts wait for the next run's reading")


async def _confirm(
    session: AsyncSession,
    report: RunReport,
    *,
    web: WebSearchProvider,
    fda_http: HttpClient,
    settings: Settings,
    now: datetime,
) -> set[str]:
    """Look up the products checked longest ago and return those a source answered for.

    Not only this run's: most products come from the weekly social run, which never
    looks them up. A lookup that could not run does not use up the product's turn.
    """
    not_asked = "not asked: no product this run needed it"
    web_log = SourceLog(CollectionStatus.EMPTY, not_asked)
    fda_log = SourceLog(CollectionStatus.EMPTY, not_asked)
    report.sources["web"], report.sources["openfda"] = web_log, fda_log
    pending = (
        (
            await session.execute(
                select(Product)
                .where(Product.status != "confirmed")
                .order_by(
                    Product.confirmation_checked_at.asc().nulls_first(),
                    Product.last_seen_at.desc(),
                )
                .limit(settings.product_max_confirmations)
            )
        )
        .scalars()
        .all()
    )
    region = ddgs_region(settings.research_geo, settings.research_language)
    checked: set[str] = set()
    for product in pending:
        answered = False
        if product.official_url is None and not web_log.stopped:
            try:
                results = await web.search(
                    f"{product.brand} {product.name}", region=region, max_results=5
                )
            except ProviderUnavailable as exc:
                web_log.failed(provider_error(exc), f"official page {product.name!r}")
            else:
                web_log.ok(len(results))
                answered = True
                # The page must name the product: a rumoured "Oura Ring 9" also finds the
                # Oura homepage, and "confirmed" never falls.
                product.official_url = next(
                    (
                        r.url
                        for r in results
                        if is_official_url(r.url, product.brand)
                        and is_product_page(r.url, product.brand, product.name)
                        and mentions(f"{r.title} {r.snippet}", product.name)
                    ),
                    None,
                )
        if (
            product.category in FDA_CATEGORIES
            and product.fda_reference is None
            and not fda_log.stopped
        ):
            name = product.product_key.split("|", 1)[1]
            try:
                reference = await lookup_510k(fda_http, brand=product.brand, name=name)
            except CollectionError as exc:
                fda_log.failed(exc, f"510(k) {product.name!r}")
            else:
                fda_log.ok(1 if reference else 0)
                answered = True
                product.fda_reference = reference
        if answered:
            product.confirmation_checked_at = now
            checked.add(product.id)
    return checked


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
