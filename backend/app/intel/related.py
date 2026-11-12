"""What relates to what, across the four discovery systems. Read-only, deterministic.

The trend search (``research_topics``), the MeSH scan (``discovery_*``),
social listening (``social_topics``) and product discovery (``products``) each
keep their own records and their own scores. This module finds the links
between them **at read time**, from keys they already store, and reports each
related item with *its own system's* numbers, untouched. Nothing is merged,
nothing is re-scored, nothing is written — so a link can never go stale or
outlive what it links, and no system's score can move because of another's.

Four rules, each explainable to a reviewer in one line (``matched_by``):

* **same topic / same subject**: the trend search's own ``topic_key``
  (``subject|outcome``, normalised) — social topics are keyed the same way;
* **MeSH substance**: a descriptor whose normalised name is the subject;
* **named in the same posts**: a product mentioned in posts tagged with the topic;
* **category**: a product category whose topic words appear in the subject or
  outcome ("glucose" relates continuous glucose monitors to "blood sugar"
  topics only through the words listed in ``CATEGORY_INFO``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DiscoveryDescriptorKind
from app.domain.models import (
    DiscoveryCandidate,
    DiscoveryDescriptor,
    Product,
    ProductObservation,
    ResearchTopic,
    ResearchTopicObservation,
    SocialCandidate,
    SocialPostTopic,
    SocialTopic,
)
from app.products.contracts import CATEGORY_INFO, ProductCategory
from app.research.normalize import normalize_query
from app.research.trends.history import topic_key

#: Topics looked at when matching by category words: those seen recently.
RECENT_DAYS = 60
LIMIT = 10


def matching_categories(*texts: str) -> dict[ProductCategory, str]:
    """Categories whose topic words appear, as whole words, in the texts. Pure."""
    haystack = f" {' '.join(normalize_query(t) for t in texts)} "
    found: dict[ProductCategory, str] = {}
    for category, (_, words) in CATEGORY_INFO.items():
        for word in words:
            if f" {normalize_query(word)} " in haystack:
                found[category] = word
                break
    return found


async def related_to_topic(
    session: AsyncSession, *, subject: str, outcome: str, social_topic_id: str
) -> list[dict[str, Any]]:
    """Everything related to a social topic."""
    key = topic_key(subject, outcome)
    subject_key = key.split("|", 1)[0]
    items: list[dict[str, Any]] = []

    for topic in (
        await session.execute(
            select(ResearchTopic).where(
                ResearchTopic.topic_key.like(f"{_like(subject_key)}|%", escape="\\")
            )
        )
    ).scalars():
        same = topic.topic_key == key
        items.append(
            _item(
                "research_topic",
                topic.id,
                topic.label,
                "same_topic" if same else "same_subject",
                f"topic key {topic.topic_key!r}",
                await _trend_numbers(session, topic.id),
            )
        )
    for social in (
        await session.execute(
            select(SocialTopic).where(
                SocialTopic.topic_key.like(f"{_like(subject_key)}|%", escape="\\")
            )
        )
    ).scalars():
        if social.id == social_topic_id:
            continue
        items.append(
            _item(
                "social_topic",
                social.id,
                social.label,
                "same_topic" if social.topic_key == key else "same_subject",
                f"topic key {social.topic_key!r}",
                await _social_numbers(session, social.id),
            )
        )
    items.extend(await _mesh(session, subject))
    items.extend(await _products_in_posts(session, social_topic_id))
    items.extend(await _products_by_category(session, subject, outcome, exclude=items))
    return items


async def related_to_product(session: AsyncSession, product: Product) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    shared = (
        await session.execute(
            select(SocialTopic, func.count(func.distinct(ProductObservation.social_post_id)))
            .join(SocialPostTopic, SocialPostTopic.topic_id == SocialTopic.id)
            .join(
                ProductObservation,
                ProductObservation.social_post_id == SocialPostTopic.post_id,
            )
            .where(ProductObservation.product_id == product.id)
            .group_by(SocialTopic.id)
            .order_by(func.count(func.distinct(ProductObservation.social_post_id)).desc())
            .limit(LIMIT)
        )
    ).all()
    for topic, posts in shared:
        items.append(
            _item(
                "social_topic",
                topic.id,
                topic.label,
                "named_in_posts",
                f"{posts} shared post(s)",
                await _social_numbers(session, topic.id),
            )
        )
    info = CATEGORY_INFO.get(ProductCategory(product.category)) if _known(product) else None
    if info is None:
        return items
    since = datetime.now(UTC) - timedelta(days=RECENT_DAYS)
    seen = {item["id"] for item in items}
    sources: tuple[tuple[type[SocialTopic] | type[ResearchTopic], str], ...] = (
        (SocialTopic, "social_topic"),
        (ResearchTopic, "research_topic"),
    )
    for model, kind in sources:
        rows = cast(
            list[SocialTopic | ResearchTopic],
            list(
                (
                    await session.execute(
                        select(model)
                        .where(model.last_seen_at >= since)
                        .order_by(model.last_seen_at.desc())
                    )
                ).scalars()
            ),
        )
        matched = 0
        for topic in rows:
            word = matching_categories(topic.subject, topic.outcome).get(
                ProductCategory(product.category)
            )
            if word is None or topic.id in seen or matched >= LIMIT:
                continue
            matched += 1
            numbers = (
                await _social_numbers(session, topic.id)
                if kind == "social_topic"
                else await _trend_numbers(session, topic.id)
            )
            items.append(
                _item(
                    kind,
                    topic.id,
                    topic.label,
                    "category",
                    f"{word!r} relates it to {product.category}",
                    numbers,
                )
            )
    return items


async def _mesh(session: AsyncSession, subject: str) -> list[dict[str, Any]]:
    norm = normalize_query(subject)
    if not norm:
        return []
    name = func.trim(
        func.lower(func.regexp_replace(DiscoveryDescriptor.name, "[^A-Za-z0-9]+", " ", "g"))
    )
    items = []
    for descriptor in (
        await session.execute(
            select(DiscoveryDescriptor).where(
                name.in_([norm, f"{norm}s", f"{norm}es"]),
                DiscoveryDescriptor.kind == DiscoveryDescriptorKind.SUBSTANCE,
            )
        )
    ).scalars():
        latest = (
            await session.execute(
                select(DiscoveryCandidate)
                .where(DiscoveryCandidate.substance_ui == descriptor.ui)
                .order_by(DiscoveryCandidate.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        own = (
            {
                "candidate_topic": latest.topic,
                "score": latest.score,
                "paper_count": latest.paper_count,
                "baseline_count": latest.baseline_count,
                "status": latest.status.value,
            }
            if latest is not None
            else {"candidate_topic": None}
        )
        items.append(
            _item(
                "mesh_substance",
                descriptor.ui,
                descriptor.name,
                "mesh_substance",
                f"MeSH descriptor {descriptor.ui} named {descriptor.name!r}",
                own,
            )
        )
    return items


async def _products_in_posts(
    session: AsyncSession, social_topic_id: str
) -> list[dict[str, Any]]:
    posts = func.count(func.distinct(ProductObservation.social_post_id))
    rows = (
        await session.execute(
            select(Product, posts)
            .join(ProductObservation, ProductObservation.product_id == Product.id)
            .join(SocialPostTopic, SocialPostTopic.post_id == ProductObservation.social_post_id)
            .where(SocialPostTopic.topic_id == social_topic_id)
            .group_by(Product.id)
            .order_by(posts.desc())
            .limit(LIMIT)
        )
    ).all()
    return [
        _item(
            "product",
            product.id,
            f"{product.name}",
            "named_in_posts",
            f"named in {count} of this topic's posts",
            _product_numbers(product),
        )
        for product, count in rows
    ]


async def _products_by_category(
    session: AsyncSession, subject: str, outcome: str, *, exclude: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    categories = matching_categories(subject, outcome)
    if not categories:
        return []
    seen = {item["id"] for item in exclude}
    rows = (
        await session.execute(
            select(Product)
            .where(Product.category.in_([c.value for c in categories]))
            .order_by(Product.last_seen_at.desc())
            .limit(LIMIT)
        )
    ).scalars()
    return [
        _item(
            "product",
            product.id,
            product.name,
            "category",
            f"{categories[ProductCategory(product.category)]!r} relates it to "
            f"{product.category}",
            _product_numbers(product),
        )
        for product in rows
        if product.id not in seen
    ]


async def _trend_numbers(session: AsyncSession, topic_id: str) -> dict[str, Any]:
    """The trend search's own latest numbers for its topic, as it stored them."""
    row = (
        await session.execute(
            select(ResearchTopicObservation)
            .where(ResearchTopicObservation.topic_id == topic_id)
            .order_by(ResearchTopicObservation.observed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return {"trend_score": None}
    return {
        "trend_score": row.trend_score,
        "trend_status": row.trend_status,
        "data_coverage": row.data_coverage,
        "observed_at": row.observed_at.isoformat(),
    }


async def _social_numbers(session: AsyncSession, topic_id: str) -> dict[str, Any]:
    row = (
        await session.execute(
            select(SocialCandidate)
            .where(SocialCandidate.topic_id == topic_id)
            .order_by(SocialCandidate.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return {"momentum": None}
    return {
        "momentum": row.momentum.get("status"),
        "independent_creators": row.metrics.get("independent_creators"),
        "proposed_at": row.created_at.isoformat(),
    }


def _product_numbers(product: Product) -> dict[str, Any]:
    return {
        "brand": product.brand,
        "category": product.category,
        "status": product.status,
        "launch_status": product.launch_status,
    }


def _known(product: Product) -> bool:
    return product.category in {c.value for c in ProductCategory}


def _item(
    kind: str, ref: str, label: str, relation: str, matched_by: str, own: dict[str, Any]
) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": ref,
        "label": label,
        "relation": relation,
        "matched_by": matched_by,
        "own": own,
    }


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
