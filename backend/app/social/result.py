"""``SocialListeningResult``: a weekly run's proposals as one snake_case document.

Read-time assembly, like ``research/service.py::build_result``: the stored
candidate carries the numbers and assessments; supporting posts are looked up
now, so a post deleted under retention simply drops out rather than being shown
from a stale copy. Relations are ``/intel/related``, asked for one topic at a
time: the desk loads this document on every visit.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    CollectionRun,
    SocialCandidate,
    SocialPost,
    SocialPostObservation,
    SocialPostTopic,
    SocialTopic,
)
from app.social import analytics
from app.social.collect import NOT_COLLECTED
from app.social.contracts import (
    CollectionKind,
    EvidenceAssessment,
    Momentum,
    SocialListeningResult,
    SocialTopicResult,
    SupportingPost,
    TopicMetrics,
)
from app.social.youtube import PLATFORM

ALL_PLATFORMS = (PLATFORM, *NOT_COLLECTED)
SUPPORTING_POSTS = 5


async def latest_weekly_run(session: AsyncSession) -> CollectionRun | None:
    """The newest completed run. A failed one proposed nothing, and serving it would
    hide last week's undecided topics behind what reads as a quiet week."""
    return (
        await session.execute(
            select(CollectionRun)
            .where(
                CollectionRun.kind == CollectionKind.SOCIAL_WEEKLY.value,
                CollectionRun.status == "completed",
            )
            .order_by(CollectionRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def build_result(session: AsyncSession, run: CollectionRun) -> SocialListeningResult:
    rows = (
        await session.execute(
            select(SocialCandidate, SocialTopic)
            .join(SocialTopic, SocialTopic.id == SocialCandidate.topic_id)
            .where(SocialCandidate.collection_run_id == run.id)
            .order_by(SocialCandidate.rank)
        )
    ).all()
    daily = (
        await session.execute(
            select(CollectionRun)
            .where(
                CollectionRun.kind == CollectionKind.SOCIAL_DAILY.value,
                CollectionRun.status == "completed",
            )
            .order_by(CollectionRun.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    topics = []
    for candidate, topic in rows:
        metrics = TopicMetrics.model_validate(candidate.metrics)
        topics.append(
            SocialTopicResult(
                candidate_id=candidate.id,
                topic_id=topic.id,
                label=topic.label,
                question=topic.question,
                category=topic.category,
                rank=candidate.rank,
                status=candidate.status,
                metrics=metrics,
                momentum=Momentum.model_validate(candidate.momentum),
                evidence=EvidenceAssessment.model_validate(
                    candidate.evidence or {"status": "not_checked"}
                ),
                supporting_posts=await supporting_posts(session, topic.id),
                limitations=analytics.limitations(metrics, ALL_PLATFORMS),
            )
        )
    return SocialListeningResult(
        run_id=run.id,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.finished_at,
        collection={
            "weekly": run.collection_status,
            "latest_daily": daily.collection_status if daily else None,
            "latest_daily_at": daily.finished_at if daily else None,
        },
        topics=topics,
        notes=list(run.notes or []),
    )


async def supporting_posts(session: AsyncSession, topic_id: str) -> list[SupportingPost]:
    """The topic's most-viewed stored posts, with their latest view count."""
    posts = (
        (
            await session.execute(
                select(SocialPost)
                .join(SocialPostTopic, SocialPostTopic.post_id == SocialPost.id)
                .where(SocialPostTopic.topic_id == topic_id)
                .order_by(SocialPost.published_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    views: dict[str, int | None] = {}
    if posts:
        for obs in (
            await session.execute(
                select(SocialPostObservation)
                .where(SocialPostObservation.post_id.in_([p.id for p in posts]))
                .order_by(SocialPostObservation.observed_at)
            )
        ).scalars():
            views[obs.post_id] = obs.views
    ranked = sorted(posts, key=lambda p: views.get(p.id) or -1, reverse=True)
    return [
        SupportingPost(
            url=post.url,
            title=post.title,
            creator=post.creator_name,
            published_at=post.published_at,
            views=views.get(post.id),
        )
        for post in ranked[:SUPPORTING_POSTS]
    ]
