"""Database reads and writes for social listening. The daily and weekly runs share these."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    CollectionRun,
    SocialPost,
    SocialPostObservation,
    SocialPostTopic,
    SocialTopic,
    TextReading,
)
from app.research.normalize import query_key
from app.research.trends.history import topic_key
from app.social.analytics import WINDOW_DAYS, WINDOWS, Coverage, PostData, Reading
from app.social.contracts import COVERED, CollectionKind, SourceLog
from app.social.youtube import PLATFORM, VideoRecord

#: Topics a new post may be tagged with before the weekly reading: seen recently.
TAG_LOOKBACK_DAYS = 60


@dataclass
class RunReport:
    """What a collection run did, filled in as it goes so a crash keeps it."""

    sources: dict[str, SourceLog] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def collection_status(self) -> dict[str, Any]:
        return {name: log.as_json() for name, log in self.sources.items()}


def post_text(title: str, description: str | None) -> str:
    return f"{title}\n{description or ''}".strip()


def topic_label(subject: str, outcome: str) -> str:
    label = subject.strip()
    label = label[:1].upper() + label[1:]
    return f"{label} for {outcome.strip()}" if outcome.strip() else label


async def save_video(
    session: AsyncSession, record: VideoRecord, *, run_id: str, now: datetime
) -> str:
    """Upsert the post by its YouTube id and append one observation. Returns the post id."""
    values = {
        "title": record.title,
        "description": record.description,
        "creator_platform_id": record.channel_id,
        "creator_name": record.channel_title,
        "published_at": record.published_at,
        "last_collected_at": now,
    }
    post_id: str = (
        await session.execute(
            insert(SocialPost)
            .values(
                platform=PLATFORM,
                platform_post_id=record.video_id,
                url=record.url,
                **values,
            )
            .on_conflict_do_update(constraint="social_posts_identity", set_=values)
            .returning(SocialPost.id)
        )
    ).scalar_one()
    await session.execute(
        insert(SocialPostObservation)
        .values(
            post_id=post_id,
            collection_run_id=run_id,
            observed_at=now,
            views=record.views,
            likes=record.likes,
            comments=record.comments,
        )
        .on_conflict_do_nothing()
    )
    return post_id


async def upsert_topic(
    session: AsyncSession, *, subject: str, outcome: str, category: str, now: datetime
) -> str:
    topic_id: str = (
        await session.execute(
            insert(SocialTopic)
            .values(
                topic_key=topic_key(subject, outcome),
                label=topic_label(subject, outcome),
                subject=subject,
                outcome=outcome,
                category=category,
                last_seen_at=now,
            )
            .on_conflict_do_update(index_elements=["topic_key"], set_={"last_seen_at": now})
            .returning(SocialTopic.id)
        )
    ).scalar_one()
    return topic_id


async def link(session: AsyncSession, post_id: str, topic_id: str) -> None:
    await session.execute(
        insert(SocialPostTopic)
        .values(post_id=post_id, topic_id=topic_id)
        .on_conflict_do_nothing()
    )


async def tag_known_topics(
    session: AsyncSession, posts: dict[str, str], *, now: datetime
) -> int:
    """Tag new posts (id -> text) with recent topics whose subject and outcome
    words all appear in the text. No model: the daily run stays cheap, and the
    weekly reading adds what this misses."""
    if not posts:
        return 0
    topics = (
        await session.execute(
            select(SocialTopic.id, SocialTopic.subject, SocialTopic.outcome).where(
                SocialTopic.last_seen_at >= now - timedelta(days=TAG_LOOKBACK_DAYS)
            )
        )
    ).all()
    wanted = [
        (topic_id, set(query_key(subject).split()) | set(query_key(outcome).split()))
        for topic_id, subject, outcome in topics
    ]
    tagged = 0
    for post_id, text in posts.items():
        words = set(query_key(text).split())
        for topic_id, needed in wanted:
            if needed and needed <= words:
                await link(session, post_id, topic_id)
                tagged += 1
    return tagged


async def load_topic_posts(
    session: AsyncSession, topic_ids: list[str], *, since: datetime
) -> dict[str, list[PostData]]:
    """Each topic's posts published since ``since``, with their readings oldest first."""
    if not topic_ids:
        return {}
    rows = (
        await session.execute(
            select(
                SocialPostTopic.topic_id,
                SocialPost.id,
                SocialPost.platform,
                SocialPost.creator_platform_id,
                SocialPost.published_at,
            )
            .join(SocialPost, SocialPost.id == SocialPostTopic.post_id)
            .where(SocialPostTopic.topic_id.in_(topic_ids), SocialPost.published_at >= since)
        )
    ).all()
    post_ids = {row[1] for row in rows}
    readings: dict[str, list[Reading]] = defaultdict(list)
    if post_ids:
        for obs in (
            await session.execute(
                select(SocialPostObservation)
                .where(SocialPostObservation.post_id.in_(post_ids))
                .order_by(SocialPostObservation.observed_at)
            )
        ).scalars():
            readings[obs.post_id].append(
                Reading(obs.observed_at, obs.views, obs.likes, obs.comments)
            )
    by_topic: dict[str, list[PostData]] = defaultdict(list)
    for topic_id, post_id, platform, creator, published in rows:
        by_topic[topic_id].append(
            PostData(post_id, platform, creator, published, tuple(readings[post_id]))
        )
    return by_topic


async def load_coverage(session: AsyncSession, *, now: datetime) -> Coverage:
    """Which days each platform's daily collection answered, read from the
    collection runs themselves — never assumed from a schedule."""
    since = now - timedelta(days=WINDOW_DAYS * WINDOWS + 1)
    runs = (
        await session.execute(
            select(
                CollectionRun.finished_at, CollectionRun.collection_status, CollectionRun.stats
            ).where(
                CollectionRun.kind == CollectionKind.SOCIAL_DAILY.value,
                CollectionRun.status == "completed",
                CollectionRun.finished_at >= since,
            )
        )
    ).all()
    covered: dict[str, set[Any]] = defaultdict(set)
    truncated: dict[str, set[Any]] = defaultdict(set)
    for finished, statuses, stats in runs:
        day = finished.date()
        for platform, status in (statuses or {}).items():
            if status.get("status") in {s.value for s in COVERED}:
                covered[platform].add(day)
        for platform, queries in ((stats or {}).get("truncated_queries") or {}).items():
            if queries:
                truncated[platform].add(day)
    return Coverage(
        covered={p: frozenset(d) for p, d in covered.items()},
        truncated={p: frozenset(d) for p, d in truncated.items()},
    )


async def prune(
    session: AsyncSession, *, retention: timedelta, now: datetime
) -> dict[str, int]:
    """Delete raw platform data past retention (YouTube allows 30 days).

    Posts cascade to their observations, topic links and product mentions.
    Topics, candidates' computed numbers and products stay: they are ours.
    """
    cutoff = now - retention
    posts = await session.execute(
        delete(SocialPost).where(SocialPost.last_collected_at < cutoff)
    )
    observations = await session.execute(
        delete(SocialPostObservation).where(SocialPostObservation.observed_at < cutoff)
    )
    readings = await session.execute(delete(TextReading).where(TextReading.read_at < cutoff))
    return {
        "posts": posts.rowcount or 0,  # type: ignore[attr-defined]
        "observations": observations.rowcount or 0,  # type: ignore[attr-defined]
        "readings": readings.rowcount or 0,  # type: ignore[attr-defined]
    }
