"""The daily social collection: refresh tracked posts, find new ones, flag movers.

No model calls, so it stays cheap enough to run every day:

1. **Refresh.** Re-read the counters of every post published within
   ``social_track_days`` (``videos.list``, 1 unit per 50). Each reading is a new
   observation row. A video the API no longer returns (deleted, made private)
   is deleted here, since we may not keep what its owner withdrew.
2. **Discover.** Search a rotating watch list — the research seeds, the
   product categories, and recent social topics — at most
   ``social_youtube_max_searches_per_day`` searches (100 units each). Each
   search covers the whole tracking window, newest first, so a query whose
   turn comes late still finds what was published meanwhile.
3. **Tag** new posts with known topics by their words (the weekly reading adds
   what this misses).
4. **Prune** raw data past retention.

Instagram and TikTok are reported, not collected: Instagram needs a Business
account and Meta's app review, TikTok has no API for commercial use. Neither
is scraped.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain.models import SocialPost, SocialTopic
from app.products.contracts import CATEGORY_INFO
from app.research.normalize import normalize_query
from app.research.seeds import SEEDS
from app.social.contracts import CollectionError, CollectionStatus, SourceLog
from app.social.store import (
    RunReport,
    post_text,
    prune,
    save_video,
    tag_known_topics,
)
from app.social.youtube import PAGE_LIMIT, PLATFORM, YouTubeClient

#: Platforms reported on every run though not collected, and why.
NOT_COLLECTED: dict[str, tuple[CollectionStatus, str]] = {
    "instagram": (
        CollectionStatus.NOT_CONFIGURED,
        "needs an Instagram Business account and Meta app review (Public Content Access)",
    ),
    "tiktok": (
        CollectionStatus.UNAVAILABLE,
        "TikTok has no API open to commercial use (its Research API is academic-only)",
    ),
}
#: Recent social topics searched in the rotation, most recently seen first.
WATCHED_TOPICS = 30
WATCH_TOPIC_DAYS = 28


def platform_logs(youtube_configured: bool) -> dict[str, SourceLog]:
    logs = {name: SourceLog(status, detail) for name, (status, detail) in NOT_COLLECTED.items()}
    logs[PLATFORM] = (
        SourceLog()
        if youtube_configured
        else SourceLog(CollectionStatus.NOT_CONFIGURED, "no YOUTUBE_API_KEY configured")
    )
    return logs


def rotation(queries: list[str], *, day: date, per_day: int) -> list[str]:
    """Today's slice of the watch list. Pure and stateless: the day picks the slice."""
    unique = sorted({normalize_query(q): q for q in queries if q.strip()}.values())
    if not unique or per_day <= 0:
        return []
    if len(unique) <= per_day:
        return unique
    start = (day.toordinal() * per_day) % len(unique)
    return [unique[(start + i) % len(unique)] for i in range(per_day)]


async def watch_list(session: AsyncSession, *, now: datetime) -> list[str]:
    seeds = [seed for group in SEEDS.values() for seed in group]
    categories = [phrase for phrase, _ in CATEGORY_INFO.values()]
    topics = (
        (
            await session.execute(
                select(SocialTopic.label)
                .where(SocialTopic.last_seen_at >= now - timedelta(days=WATCH_TOPIC_DAYS))
                .order_by(SocialTopic.last_seen_at.desc())
                .limit(WATCHED_TOPICS)
            )
        )
        .scalars()
        .all()
    )
    return [*seeds, *categories, *topics]


async def collect_daily(
    session: AsyncSession,
    report: RunReport,
    client: YouTubeClient | None,
    *,
    run_id: str,
    settings: Settings,
    now: datetime,
) -> None:
    report.sources = platform_logs(client is not None)
    log = report.sources[PLATFORM]
    # First and alone: YouTube allows 30 days of storage, so a crash below must not skip it.
    report.stats["pruned"] = await prune(
        session, retention=timedelta(days=settings.social_retention_days), now=now
    )
    await session.commit()
    if client is not None:
        await _refresh(session, report, client, log, run_id=run_id, settings=settings, now=now)
        await session.commit()
        await _discover(session, report, client, log, run_id=run_id, settings=settings, now=now)
        await session.commit()


async def _refresh(
    session: AsyncSession,
    report: RunReport,
    client: YouTubeClient,
    log: SourceLog,
    *,
    run_id: str,
    settings: Settings,
    now: datetime,
) -> None:
    ids: list[str] = list(
        (
            await session.execute(
                select(SocialPost.platform_post_id).where(
                    SocialPost.platform == PLATFORM,
                    SocialPost.published_at >= now - timedelta(days=settings.social_track_days),
                )
            )
        ).scalars()
    )
    refreshed = withdrawn = 0
    for start in range(0, len(ids), PAGE_LIMIT):
        if log.stopped:
            break
        batch = ids[start : start + PAGE_LIMIT]
        try:
            records = await client.videos(batch)
        except CollectionError as exc:
            log.failed(exc, f"refresh {len(batch)} videos")
            continue
        log.ok(len(records))
        for record in records:
            await save_video(session, record, run_id=run_id, now=now)
        gone = set(batch) - {r.video_id for r in records}
        if gone:
            await session.execute(
                delete(SocialPost).where(
                    SocialPost.platform == PLATFORM, SocialPost.platform_post_id.in_(gone)
                )
            )
        refreshed += len(records)
        withdrawn += len(gone)
    report.stats["refreshed"] = refreshed
    report.stats["withdrawn"] = withdrawn


async def _discover(
    session: AsyncSession,
    report: RunReport,
    client: YouTubeClient,
    log: SourceLog,
    *,
    run_id: str,
    settings: Settings,
    now: datetime,
) -> None:
    queries = rotation(
        await watch_list(session, now=now),
        day=now.date(),
        per_day=settings.social_youtube_max_searches_per_day,
    )
    # Every search covers the whole tracking window. Results come newest first,
    # so this loses nothing, and no query's turn can come too late when the
    # watch list grows or shrinks between days.
    since = now - timedelta(days=settings.social_track_days)
    new_posts: dict[str, str] = {}
    truncated: list[str] = []
    for query in queries:
        if log.stopped:
            break
        try:
            ids, full = await client.search(
                query,
                since=since,
                until=now,
                language=settings.research_language,
                region=settings.research_geo,
            )
            known = set(
                (
                    await session.execute(
                        select(SocialPost.platform_post_id).where(
                            SocialPost.platform == PLATFORM,
                            SocialPost.platform_post_id.in_(ids),
                        )
                    )
                ).scalars()
            )
            records = await client.videos([i for i in ids if i not in known])
        except CollectionError as exc:
            log.failed(exc, f"search {query!r}")
            continue
        log.ok(len(ids))
        if full:
            truncated.append(query)
        for record in records:
            post_id = await save_video(session, record, run_id=run_id, now=now)
            new_posts[post_id] = post_text(record.title, record.description)
    report.stats["searched"] = queries
    report.stats["new_posts"] = len(new_posts)
    report.stats["truncated_queries"] = {PLATFORM: truncated}
    report.stats["tagged"] = await tag_known_topics(session, new_posts, now=now)
