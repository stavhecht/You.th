"""Database writes for products: record a mention, then recompute status from the record."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Product, ProductObservation
from app.products.resolve import (
    LAUNCH_KINDS,
    checked_category,
    launch_status,
    product_key,
    product_status,
)
from app.social.analytics import windows


@dataclass(frozen=True, slots=True)
class Mention:
    """One product named by one source: the reader's output plus where it came from."""

    brand: str
    product_name: str
    category: str
    mention_kind: str
    quote: str
    source_kind: str
    source_url: str
    publisher: str | None
    published_at: datetime | None
    social_post_id: str | None = None
    #: The whole text it was read from. A quote is often just the product's name.
    text: str = ""


async def record_mentions(
    session: AsyncSession, mentions: list[Mention], *, now: datetime
) -> set[str]:
    """Upsert each product by its key and add the observation once per source.
    Returns the ids of the products touched."""
    touched: set[str] = set()
    for mention in mentions:
        key = product_key(mention.brand, mention.product_name)
        if key is None:
            continue
        product_id: str = (
            await session.execute(
                insert(Product)
                .values(
                    product_key=key,
                    brand=mention.brand,
                    name=mention.product_name,
                    category=checked_category(
                        mention.category,
                        f"{mention.product_name} {mention.quote} {mention.text}",
                    ),
                    first_seen_at=now,
                    last_seen_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["product_key"], set_={"last_seen_at": now}
                )
                .returning(Product.id)
            )
        ).scalar_one()
        await session.execute(
            insert(ProductObservation)
            .values(
                product_id=product_id,
                social_post_id=mention.social_post_id,
                source_kind=mention.source_kind,
                source_url=mention.source_url,
                publisher=mention.publisher,
                published_at=mention.published_at,
                observed_at=now,
                mention_kind=mention.mention_kind,
                quote=mention.quote[:200],
            )
            .on_conflict_do_nothing(constraint="product_observations_once_per_source")
        )
        touched.add(product_id)
    return touched


def source_identity(publisher: str | None, url: str) -> str:
    """Who said it: the publisher or creator, else the site. Independence is
    counted over these, so one outlet's ten articles are one source."""
    if publisher and publisher.strip():
        return publisher.strip().lower()
    host = (urlsplit(url).hostname or url).lower()
    return host.removeprefix("www.")


async def refresh_statuses(
    session: AsyncSession, product_ids: set[str], *, now: datetime
) -> None:
    if not product_ids:
        return
    sources: dict[str, set[str]] = defaultdict(set)
    launchers: dict[str, set[str]] = defaultdict(set)
    for product_id, publisher, url, kind in (
        await session.execute(
            select(
                ProductObservation.product_id,
                ProductObservation.publisher,
                ProductObservation.source_url,
                ProductObservation.mention_kind,
            ).where(ProductObservation.product_id.in_(product_ids))
        )
    ).all():
        who = source_identity(publisher, url)
        sources[product_id].add(who)
        if kind in LAUNCH_KINDS:
            launchers[product_id].add(who)
    products = (
        await session.execute(select(Product).where(Product.id.in_(product_ids)))
    ).scalars()
    for product in products:
        decision = (product.fda_reference or {}).get("decision_date")
        product.status = product_status(
            product.status,
            independent_sources=len(sources[product.id]),
            official_url=product.official_url,
            fda=product.fda_reference is not None,
        )
        product.launch_status = launch_status(
            product.launch_status,
            launch_sources=len(launchers[product.id]),
            fda_decision=datetime.fromisoformat(decision) if decision else None,
            now=now,
        )


def weekly_mentions(
    observed: list[datetime], covered: list[datetime], *, now: datetime
) -> list[dict[str, Any]]:
    """Mentions per 7-day window, newest first. A window no discovery run
    finished in is unmeasured (None), not quiet."""
    weeks = []
    for start, end in windows(now):
        measured = any(start <= moment < end for moment in covered)
        weeks.append(
            {
                "start": start,
                "end": end,
                "covered": measured,
                "mentions": sum(start <= t < end for t in observed) if measured else None,
            }
        )
    return weeks
