"""Social listening and product discovery against real Postgres (``youth_test``).

The daily and weekly runs read whole tables, so these tests run on the
end-to-end database, never the dev one, and empty only the new modules' tables
(``SOCIAL_TABLES``) around each test. Skipped when ``youth_test`` is absent or
lacks migration 0002 — **a skipped run verifies none of this**. Every external
API is a fake; no request leaves the process.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.console.social import list_products, promote_social_candidate
from app.config import Settings, get_settings
from app.domain.enums import RunStatus, UserRole
from app.domain.models import (
    CollectionRun,
    DiscoveryDescriptor,
    PipelineRun,
    Product,
    ProductObservation,
    ResearchFeedState,
    ResearchTopic,
    ResearchTopicObservation,
    SocialCandidate,
    SocialPost,
    SocialPostObservation,
    SocialPostTopic,
    SocialTopic,
    TextReading,
    User,
)
from app.intel.reader import BATCH, ReaderOutput, read_texts
from app.intel.related import related_to_topic
from app.llm.base import LLMError
from app.pipeline.runner import PipelineWorker
from app.products.discover import discover_products
from app.products.store import Mention, record_mentions, refresh_statuses
from app.research.contracts import EvidenceCounts, NewsArticle, WebResult
from app.research.providers.base import NewsResults, ProviderUnavailable
from app.security.auth import AuthenticatedReviewer
from app.social import service
from app.social.collect import collect_daily
from app.social.contracts import CollectionError, CollectionKind, CollectionStatus
from app.social.questions import QuestionsOutput, TopicQuestion
from app.social.result import build_result, latest_weekly_run
from app.social.store import RunReport, load_coverage, prune, save_video, upsert_topic
from app.social.weekly import LiteratureCheck, analyse_week
from app.social.youtube import VideoRecord

URL = os.environ.get("SOCIAL_TEST_DATABASE_URL") or make_url(get_settings().database_url).set(
    database="youth_test"
)
SOCIAL_TABLES = (
    "collection_runs, social_posts, social_post_observations, social_topics, "
    "social_post_topics, text_readings, social_candidates, products, product_observations"
)
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(URL, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            name = await connection.scalar(sa.text("SELECT current_database()"))
            await connection.execute(sa.text("SELECT 1 FROM collection_runs LIMIT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no youth_test database with migration 0002 at {URL}")
    assert name.endswith("_test"), f"refusing to truncate {name!r}: not a *_test database"
    truncate = sa.text(f"TRUNCATE {SOCIAL_TABLES} RESTART IDENTITY CASCADE")
    async with engine.begin() as connection:
        await connection.execute(truncate)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.execute(truncate)
    await engine.dispose()


async def _run(db: AsyncSession, kind: CollectionKind = CollectionKind.SOCIAL_DAILY) -> str:
    run = await service.start_collection_run(db, kind)
    await db.commit()
    return run.id


def _video(vid: str, **fields: Any) -> VideoRecord:
    base: dict[str, Any] = {
        "video_id": vid,
        "title": f"Creatine and sleep {vid}",
        "description": "What the studies say",
        "channel_id": f"chan-{vid}",
        "channel_title": f"Channel {vid}",
        "published_at": NOW - timedelta(days=1),
        "views": 100,
        "likes": 10,
        "comments": 1,
    }
    return VideoRecord(**{**base, **fields})


class FakeYouTube:
    def __init__(self, *, videos: dict[str, VideoRecord], search: list[Any]) -> None:
        self._videos = videos
        self._search = list(search)
        self.searches: list[str] = []

    async def search(self, query: str, **_: Any) -> tuple[list[str], bool]:
        self.searches.append(query)
        answer = self._search.pop(0) if self._search else ([], False)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def videos(self, ids: list[str]) -> list[VideoRecord]:
        return [self._videos[i] for i in ids if i in self._videos]


# --- posts and observations ---------------------------------------------------------


async def test_a_post_seen_twice_is_one_row_with_two_observations(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as db:
        first, second = await _run(db), None
        await db.execute(sa.update(CollectionRun).values(status="completed"))
        second = await _run(db)
        await save_video(db, _video("v1"), run_id=first, now=NOW - timedelta(days=1))
        await save_video(
            db, _video("v1", views=500, likes=None), run_id=second, now=NOW
        )
        await db.commit()
        posts = (await db.execute(sa.select(SocialPost))).scalars().all()
        observations = (
            (
                await db.execute(
                    sa.select(SocialPostObservation).order_by(SocialPostObservation.observed_at)
                )
            )
            .scalars()
            .all()
        )
    assert len(posts) == 1
    assert [o.views for o in observations] == [100, 500]  # history kept, not overwritten
    assert observations[1].likes is None
    async with factory() as db:
        nulls = await db.scalar(
            sa.text("SELECT count(*) FROM social_post_observations WHERE likes IS NULL")
        )
    assert nulls == 1  # SQL NULL, not 0


# --- the queue ----------------------------------------------------------------------


async def test_one_run_per_kind_may_be_active(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as db:
        await _run(db)
        with pytest.raises(service.CollectionAlreadyActive):
            await service.start_collection_run(db, CollectionKind.SOCIAL_DAILY)
        await _run(db, CollectionKind.PRODUCT_DISCOVERY)
    claimed = await service.claim_next(factory)
    assert claimed is not None and claimed[2] == "social_daily"
    async with factory() as db:
        await db.execute(
            sa.update(CollectionRun)
            .where(CollectionRun.id == claimed[0])
            .values(heartbeat_at=NOW - timedelta(hours=2))
        )
        await db.commit()
    assert await service.recover_stale(factory, timedelta(minutes=10)) == 1
    async with factory() as db:
        run = await db.get(CollectionRun, claimed[0])
    assert run is not None and run.status == "failed"


async def test_a_model_outage_holds_every_kind_but_the_daily_run(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.pipeline.runner.get_session_factory", lambda: factory)
    worker = PipelineWorker()
    async with factory() as db:
        await _run(db, CollectionKind.SOCIAL_WEEKLY)
    assert await worker._has_work_needing_a_model() is True
    assert await worker._claim_collection_run(model_free_only=True) is None

    async with factory() as db:
        await _run(db, CollectionKind.SOCIAL_DAILY)
    claimed = await worker._claim_collection_run(model_free_only=True)
    assert claimed is not None and claimed[2] == "social_daily"


# --- the daily run --------------------------------------------------------------------


async def test_the_daily_run_refreshes_discovers_and_drops_withdrawn_videos(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(social_youtube_max_searches_per_day=1)
    async with factory() as db:
        seed = await _run(db)
        await save_video(
            db, _video("gone"), run_id=seed, now=NOW - timedelta(days=1)
        )
        await save_video(
            db, _video("kept"), run_id=seed, now=NOW - timedelta(days=1)
        )
        await db.execute(sa.update(CollectionRun).values(status="completed"))
        await db.commit()
        run_id = await _run(db)
        client = FakeYouTube(
            videos={"kept": _video("kept", views=900), "new": _video("new")},
            search=[(["kept", "new"], False)],
        )
        report = RunReport()
        await collect_daily(db, report, client, run_id=run_id, settings=settings, now=NOW)  # type: ignore[arg-type]
        ids = set((await db.execute(sa.select(SocialPost.platform_post_id))).scalars())
    assert ids == {"kept", "new"}  # deleted on the platform, deleted here
    assert len(client.searches) == 1
    status = report.collection_status()
    assert status["youtube"]["status"] == "SUCCESS"
    assert status["tiktok"]["status"] == "UNAVAILABLE"
    assert status["instagram"]["status"] == "NOT_CONFIGURED"
    assert report.stats["withdrawn"] == 1 and report.stats["new_posts"] == 1


async def test_a_rate_limited_day_is_not_coverage(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(social_youtube_max_searches_per_day=3)
    async with factory() as db:
        run_id = await _run(db)
        client = FakeYouTube(
            videos={},
            search=[CollectionError(CollectionStatus.RATE_LIMITED, "quotaExceeded")],
        )
        report = RunReport()
        await collect_daily(db, report, client, run_id=run_id, settings=settings, now=NOW)  # type: ignore[arg-type]
    assert len(client.searches) == 1  # a spent quota stops the source
    await service.finish(
        factory,
        run_id,
        collection_status=report.collection_status(),
        stats=report.stats,
        notes=[],
        error=None,
    )
    async with factory() as db:
        coverage = await load_coverage(db, now=NOW + timedelta(minutes=1))
    assert report.collection_status()["youtube"]["status"] == "RATE_LIMITED"
    assert coverage.covered.get("youtube", frozenset()) == frozenset()


async def test_without_a_key_youtube_is_not_configured(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as db:
        run_id = await _run(db)
        report = RunReport()
        await collect_daily(db, report, None, run_id=run_id, settings=Settings(), now=NOW)
    assert report.collection_status()["youtube"]["status"] == "NOT_CONFIGURED"


async def test_a_crash_in_the_daily_run_still_deletes_what_retention_requires(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """YouTube allows 30 days of storage; a bug that repeats daily must not breach it."""

    class Broken(FakeYouTube):
        async def videos(self, ids: list[str]) -> list[VideoRecord]:
            raise RuntimeError("an unexpected bug")

    async with factory() as db:
        run_id = await _run(db)
        await save_video(db, _video("old"), run_id=run_id, now=NOW - timedelta(days=31))
        await save_video(db, _video("fresh"), run_id=run_id, now=NOW)
        await db.commit()
        with pytest.raises(RuntimeError):
            await collect_daily(
                db,
                RunReport(),
                Broken(videos={}, search=[]),  # type: ignore[arg-type]
                run_id=run_id,
                settings=Settings(),
                now=NOW,
            )
        await db.rollback()
        left = set((await db.execute(sa.select(SocialPost.platform_post_id))).scalars())
    assert left == {"fresh"}


async def test_retention_deletes_old_posts_and_everything_hanging_off_them(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    old = NOW - timedelta(days=31)
    async with factory() as db:
        run_id = await _run(db)
        post_id = await save_video(db, _video("old"), run_id=run_id, now=old)
        await save_video(db, _video("fresh"), run_id=run_id, now=NOW)
        await record_mentions(
            db,
            [
                Mention(
                    "Oura",
                    "Oura Ring 4",
                    "smart_ring",
                    "review",
                    "Oura Ring 4",
                    "youtube",
                    "https://www.youtube.com/watch?v=old",
                    "Channel old",
                    None,
                    post_id,
                )
            ],
            now=old,
        )
        pruned = await prune(db, retention=timedelta(days=30), now=NOW)
        await db.commit()
        left = set((await db.execute(sa.select(SocialPost.platform_post_id))).scalars())
        mentions = await db.scalar(sa.select(sa.func.count()).select_from(ProductObservation))
        products = await db.scalar(sa.select(sa.func.count()).select_from(Product))
    assert left == {"fresh"} and pruned["posts"] == 1
    assert mentions == 0  # the quote came from the deleted post
    assert products == 1  # the product is ours; its status is kept


# --- products -------------------------------------------------------------------------


async def test_one_product_across_sources_and_spellings_is_corroborated(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    def mention(name: str, url: str, publisher: str, kind: str = "mention") -> Mention:
        return Mention("Oura", name, "smart_ring", kind, name, "news", url, publisher, None)

    async with factory() as db:
        touched = await record_mentions(
            db,
            [
                mention("Oura Ring 4", "https://a.example/1", "Outlet A", "launch"),
                mention("OURA Ring Gen 4", "https://b.example/2", "Outlet B"),
                mention("Oura Ring 4", "https://a.example/1", "Outlet A"),  # same source again
            ],
            now=NOW,
        )
        await refresh_statuses(db, touched, now=NOW)
        await db.commit()
        products = (await db.execute(sa.select(Product))).scalars().all()
        observations = await db.scalar(
            sa.select(sa.func.count()).select_from(ProductObservation)
        )
    assert len(products) == 1 and observations == 2
    assert products[0].status == "corroborated"
    assert products[0].launch_status == "claimed"  # one source said launch: claimed only


# --- the weekly run -------------------------------------------------------------------


class FakeReader:
    """Reads every text as creatine-for-sleep, and names Oura where the text does."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, system: str, user: str) -> ReaderOutput:
        self.calls += 1
        items = []
        for line in user.splitlines():
            index = int(line[1 : line.index("]")])
            products = []
            if "Oura Ring 4" in line:
                products.append(
                    {
                        "brand": "Oura",
                        "product_name": "Oura Ring 4",
                        "category": "smart_ring",
                        "mention_kind": "launch" if "launch" in line else "review",
                        "quote": "Oura Ring 4",
                    }
                )
            items.append(
                {
                    "id": index,
                    "is_wellness_topic": True,
                    "subject": "creatine",
                    "outcome": "sleep",
                    "category": "supplements",
                    "products": products,
                }
            )
        return ReaderOutput.model_validate({"items": items})


class FakeEvidence:
    name = "pubmed"

    async def profile_terms(self, terms: str, recent_since: str) -> EvidenceCounts:
        return EvidenceCounts(
            source="pubmed", query=terms, total=40, recent=6, reviews_or_meta=2, rcts=4
        )


class FakeExtraction:
    async def extract(self, payload: Any) -> Any:
        return SimpleNamespace(
            output=SimpleNamespace(
                product="creatine", ingredients=[], target_claims=["improves sleep quality"]
            )
        )


async def _covered_days(db: AsyncSession, days: int) -> None:
    """Completed daily runs on each of the last ``days`` days, YouTube answering."""
    for offset in range(days):
        finished = NOW - timedelta(days=offset)
        db.add(
            CollectionRun(
                label=f"social_daily_seed_{offset}",
                kind="social_daily",
                status="completed",
                collection_status={"youtube": {"status": "SUCCESS"}},
                stats={},
                notes=[],
                created_at=finished,
                finished_at=finished,
            )
        )
    await db.commit()


async def _week_of_posts(db: AsyncSession, run_id: str) -> None:
    for i in range(4):
        title = "Oura Ring 4 and creatine for sleep" if i == 0 else f"Creatine for sleep {i}"
        await save_video(db, _video(f"w{i}", title=title), run_id=run_id, now=NOW)
    await db.commit()


class FakeQuestions:
    """Answers each topic in the prompt, with whatever the test wants written."""

    def __init__(self, question: str = "Does creatine really help sleep?") -> None:
        self.question = question
        self.calls = 0

    async def __call__(self, system: str, user: str) -> QuestionsOutput:
        self.calls += 1
        ids = [
            int(line[1 : line.index("]")]) for line in user.splitlines() if line.startswith("[")
        ]
        return QuestionsOutput(
            items=[TopicQuestion(id=index, question=self.question) for index in ids]
        )


async def _weekly(
    db: AsyncSession, reader: FakeReader, *, questions: Any = None, **overrides: Any
) -> RunReport:
    run_id = await _run(db, CollectionKind.SOCIAL_WEEKLY)
    report = RunReport()
    await analyse_week(
        db,
        report,
        read_call=reader,
        question_call=questions,
        evidence=LiteratureCheck(FakeExtraction(), (FakeEvidence(),)),  # type: ignore[arg-type]
        run_id=run_id,
        settings=Settings(**{"social_min_posts": 3, **overrides}),
        now=NOW,
    )
    await db.execute(
        sa.update(CollectionRun)
        .where(CollectionRun.id == run_id)
        .values(status="completed", finished_at=NOW)
    )
    await db.commit()
    return report


async def test_the_weekly_run_reads_once_links_topics_and_proposes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    reader = FakeReader()
    async with factory() as db:
        await _covered_days(db, 14)
        await _week_of_posts(db, await _run(db))
        report = await _weekly(db, reader)
        candidates = (await db.execute(sa.select(SocialCandidate))).scalars().all()
        product = (await db.execute(sa.select(Product))).scalar_one()
        await _weekly(db, reader)  # the second week reads nothing new
    assert reader.calls == 1
    assert report.stats["candidates"] == 1 and len(candidates) == 1
    candidate = candidates[0]
    assert candidate.momentum["status"] == "rising"  # 4 creators against none last week
    assert "evidence" not in candidate.metrics
    assert candidate.evidence is not None and candidate.evidence["status"] == "strong"
    assert product.name == "Oura Ring 4" and product.launch_status == "none"  # a review


async def test_a_proposed_topic_gets_one_question_and_is_never_asked_again(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    questions = FakeQuestions()
    async with factory() as db:
        await _covered_days(db, 14)
        await _week_of_posts(db, await _run(db))
        report = await _weekly(db, FakeReader(), questions=questions)
        await _weekly(db, FakeReader(), questions=questions)
        topic = (await db.execute(sa.select(SocialTopic))).scalar_one()
    assert topic.question == "Does creatine really help sleep?"
    assert questions.calls == 1
    assert report.sources["questions"].status == CollectionStatus.SUCCESS


async def test_an_unusable_question_or_a_model_outage_leaves_the_label_and_the_run(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async def down(system: str, user: str) -> QuestionsOutput:
        raise LLMError("server gone")

    async with factory() as db:
        await _covered_days(db, 14)
        await _week_of_posts(db, await _run(db))
        report = await _weekly(db, FakeReader(), questions=down)
        await _weekly(db, FakeReader(), questions=FakeQuestions("What about sleep?"))
        topic = (await db.execute(sa.select(SocialTopic))).scalar_one()
        candidates = (await db.execute(sa.select(SocialCandidate))).scalars().all()
    assert topic.question is None and len(candidates) == 2
    assert report.sources["questions"].status == CollectionStatus.ERROR


async def test_promoting_a_topic_generates_from_its_question(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    email = "social-promote@example.test"
    async with factory() as db:
        await db.execute(sa.delete(User).where(User.email == email))
        user = User(
            email=email,
            password_hash="x",
            display_name="T",
            role=UserRole.REVIEWER,
            is_active=True,
        )
        db.add(user)
        await db.commit()
        run_id = None
        try:
            await _covered_days(db, 14)
            await _week_of_posts(db, await _run(db))
            await _weekly(db, FakeReader(), questions=FakeQuestions())
            candidate = (await db.execute(sa.select(SocialCandidate))).scalar_one()
            reviewer = AuthenticatedReviewer(
                id=user.id, email=email, display_name="T", role=UserRole.REVIEWER
            )
            await promote_social_candidate(candidate.id, db, reviewer)
            await db.commit()
            await db.refresh(candidate)
            run_id = candidate.pipeline_run_id
            run = await db.get(PipelineRun, run_id)
            assert run is not None and run.topic == "Does creatine really help sleep?"
        finally:
            await db.rollback()
            await db.execute(sa.text("TRUNCATE social_candidates"))
            if run_id:
                await db.execute(sa.delete(PipelineRun).where(PipelineRun.id == run_id))
            await db.execute(sa.delete(User).where(User.email == email))
            await db.commit()


async def test_without_two_covered_weeks_momentum_is_insufficient_history(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as db:
        await _covered_days(db, 3)
        await _week_of_posts(db, await _run(db))
        await _weekly(db, FakeReader())
        candidate = (await db.execute(sa.select(SocialCandidate))).scalar_one()
    assert candidate.momentum["status"] == "insufficient_history"
    windows = candidate.metrics["windows"]
    assert windows[1]["covered"] is False and windows[1]["uploads"] is None


async def test_a_dismissed_topic_is_held_back_and_the_desk_is_told_why(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    email = "social-test@example.test"
    async with factory() as db:
        await db.execute(sa.delete(User).where(User.email == email))
        user = User(
            email=email,
            password_hash="x",
            display_name="T",
            role=UserRole.REVIEWER,
            is_active=True,
        )
        db.add(user)
        await db.commit()
        try:
            await _covered_days(db, 14)
            await _week_of_posts(db, await _run(db))
            await _weekly(db, FakeReader())
            await db.execute(
                sa.update(SocialCandidate).values(
                    status="dismissed",
                    dismiss_reason="not for us",
                    decided_by=user.id,
                    decided_at=NOW,
                )
            )
            await db.commit()
            report = await _weekly(db, FakeReader())
        finally:
            await db.rollback()
            await db.execute(sa.text("TRUNCATE social_candidates"))
            await db.execute(sa.delete(User).where(User.email == email))
            await db.commit()
    assert report.stats["candidates"] == 0 and report.stats["suppressed"] == 1
    assert any("held back" in note for note in report.notes)


@pytest.mark.parametrize(
    ("run_status", "proposed"),
    [
        (RunStatus.FAILED, 1),
        (RunStatus.CANCELLED, 1),
        (RunStatus.SUCCEEDED, 0),
        (RunStatus.QUEUED, 0),
    ],
)
async def test_a_topic_whose_draft_failed_is_proposed_again(
    factory: async_sessionmaker[AsyncSession], run_status: RunStatus, proposed: int
) -> None:
    """No article exists to duplicate. With a question set, this also proves the topic
    is not held back as a repeat of itself."""
    email = "social-retry@example.test"
    drafted = "social-retry-test"
    async with factory() as db:
        await db.execute(sa.delete(User).where(User.email == email))
        user = User(
            email=email,
            password_hash="x",
            display_name="T",
            role=UserRole.REVIEWER,
            is_active=True,
        )
        draft = PipelineRun(topic=drafted, status=run_status, attempts=1)
        db.add_all([user, draft])
        await db.commit()
        try:
            await _covered_days(db, 14)
            await _week_of_posts(db, await _run(db))
            await _weekly(db, FakeReader(), questions=FakeQuestions())
            await db.execute(
                sa.update(SocialCandidate).values(
                    status="promoted",
                    pipeline_run_id=draft.id,
                    decided_by=user.id,
                    decided_at=NOW,
                )
            )
            await db.commit()
            report = await _weekly(db, FakeReader(), questions=FakeQuestions())
        finally:
            await db.rollback()
            await db.execute(sa.text("TRUNCATE social_candidates"))
            await db.execute(sa.delete(PipelineRun).where(PipelineRun.topic == drafted))
            await db.execute(sa.delete(User).where(User.email == email))
            await db.commit()
    assert report.stats["candidates"] == proposed


@pytest.mark.parametrize("status", ["promoted", "dismissed"])
async def test_a_decided_question_does_not_come_back_under_another_topic(
    factory: async_sessionmaker[AsyncSession], status: str
) -> None:
    """Promoted with no posts this week, so not held back; dismissed with posts, so held."""
    email = "social-repeat@example.test"
    drafted = "social-repeat-test"
    async with factory() as db:
        await db.execute(sa.delete(User).where(User.email == email))
        user = User(
            email=email,
            password_hash="x",
            display_name="T",
            role=UserRole.REVIEWER,
            is_active=True,
        )
        draft = PipelineRun(topic=drafted, status=RunStatus.SUCCEEDED, attempts=1)
        db.add_all([user, draft])
        await db.commit()
        try:
            await _covered_days(db, 14)
            run_id = await _run(db)
            await _week_of_posts(db, run_id)
            twin = await upsert_topic(
                db, subject="magnesium", outcome="sleep", category="supplements", now=NOW
            )
            await db.execute(
                sa.update(SocialTopic)
                .where(SocialTopic.id == twin)
                .values(question="Does creatine really help sleep?")
            )
            if status == "dismissed":
                for i in range(3):
                    await save_video(
                        db,
                        _video(f"m{i}", title=f"Magnesium for sleep {i}"),
                        run_id=run_id,
                        now=NOW,
                    )
            db.add(
                SocialCandidate(
                    collection_run_id=run_id,
                    topic_id=twin,
                    rank=1,
                    metrics={"independent_creators": 10},
                    momentum={},
                    status=status,
                    pipeline_run_id=draft.id if status == "promoted" else None,
                    dismiss_reason="not for us" if status == "dismissed" else None,
                    decided_by=user.id,
                    decided_at=NOW,
                )
            )
            await db.commit()
            report = await _weekly(
                db, FakeReader(), questions=FakeQuestions("Can creatine really help sleep?")
            )
        finally:
            await db.rollback()
            await db.execute(sa.text("TRUNCATE social_candidates"))
            await db.execute(sa.delete(PipelineRun).where(PipelineRun.topic == drafted))
            await db.execute(sa.delete(User).where(User.email == email))
            await db.commit()
    assert report.stats["candidates"] == 0 and report.stats["repeated_questions"] == 1
    assert any("same question as Magnesium for sleep" in note for note in report.notes)


async def test_a_repeated_question_gives_its_slot_to_the_next_best_topic(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cutting to the limit before dropping repeats proposed 8 of 10 with 33 eligible."""
    wanted = (
        ("creatine", 5, "Does creatine help sleep?"),
        ("magnesium", 4, "Can creatine help sleep?"),  # the repeat, second by creators
        ("zinc", 3, "Does zinc help sleep?"),
    )
    async with factory() as db:
        await _covered_days(db, 14)
        run_id = await _run(db)
        for subject, posts, question in wanted:
            topic_id = await upsert_topic(
                db, subject=subject, outcome="sleep", category="supplements", now=NOW
            )
            await db.execute(
                sa.update(SocialTopic)
                .where(SocialTopic.id == topic_id)
                .values(question=question)
            )
            for i in range(posts):
                video = _video(f"{subject}{i}", title=f"{subject.capitalize()} for sleep {i}")
                await save_video(db, video, run_id=run_id, now=NOW)
        await db.commit()
        report = await _weekly(db, FakeReader(), social_max_candidates=2)
        proposed = (
            await db.execute(
                sa.select(SocialTopic.subject)
                .join(SocialCandidate, SocialCandidate.topic_id == SocialTopic.id)
                .order_by(SocialCandidate.rank)
            )
        ).scalars()
        subjects = list(proposed)
    assert subjects == ["creatine", "zinc"]
    assert report.stats["repeated_questions"] == 1


async def test_the_result_is_snake_case_with_evidence_beside_the_metrics(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as db:
        await _covered_days(db, 14)
        await _week_of_posts(db, await _run(db))
        await _weekly(db, FakeReader())
        run = (
            await db.execute(
                sa.select(CollectionRun).where(CollectionRun.kind == "social_weekly")
            )
        ).scalar_one()
        result = (await build_result(db, run)).model_dump(mode="json")
        topic = result["topics"][0]
        related = await related_to_topic(
            db, subject="creatine", outcome="sleep", social_topic_id=topic["topic_id"]
        )
    assert {
        "metrics",
        "momentum",
        "evidence",
        "supporting_posts",
        "limitations",
    } <= set(topic)
    assert "independent_creators" in topic["metrics"]
    assert topic["evidence"]["status"] == "strong"
    assert len(topic["supporting_posts"]) == 4
    assert any(item["kind"] == "product" for item in related)
    assert any("not evidence" in note for note in topic["limitations"])
    assert result["collection"]["latest_daily"]["youtube"]["status"] == "SUCCESS"


async def test_a_failed_weekly_run_does_not_replace_the_last_completed_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """It proposed nothing, so serving it read as a quiet week and hid undecided topics."""

    def weekly(label: str, status: str, days_ago: int) -> CollectionRun:
        when = NOW - timedelta(days=days_ago)
        return CollectionRun(
            label=label,
            kind="social_weekly",
            status=status,
            collection_status={},
            stats={},
            notes=[],
            created_at=when,
            finished_at=when,
        )

    async with factory() as db:
        db.add(weekly("crashed", "failed", 1))
        await db.commit()
        assert await latest_weekly_run(db) is None

        db.add(weekly("done", "completed", 8))
        await db.commit()
        latest = await latest_weekly_run(db)
    assert latest is not None and latest.label == "done"


async def test_a_batch_already_read_survives_a_crash_in_the_next_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A batch is minutes of model time; a restart must not throw the finished ones away."""

    class DiesOnTheSecondBatch(FakeReader):
        async def __call__(self, system: str, user: str) -> ReaderOutput:
            if self.calls == 1:
                raise RuntimeError("worker killed")
            return await super().__call__(system, user)

    texts = {f"t{i}": f"Creatine for sleep {i}" for i in range(BATCH + 1)}
    async with factory() as db:
        with pytest.raises(RuntimeError):
            await read_texts(db, DiesOnTheSecondBatch(), texts, max_batches=5)
    async with factory() as db:
        saved = await db.scalar(sa.select(sa.func.count()).select_from(TextReading))
    assert saved == BATCH


async def test_the_product_list_gives_each_product_its_own_mentions_newest_first(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    def mention(brand: str, name: str, url: str) -> Mention:
        return Mention(
            brand=brand,
            product_name=name,
            category="smart_ring",
            mention_kind="review",
            quote=name,
            source_kind="news",
            source_url=url,
            publisher=url,
            published_at=NOW,
        )

    async with factory() as db:
        older = NOW - timedelta(days=2)
        first = mention("Oura", "Oura Ring 4", "https://a.test/1")
        await record_mentions(db, [first], now=older)
        await record_mentions(
            db,
            [
                mention("Oura", "Oura Ring 4", "https://b.test/2"),
                mention("Ultrahuman", "Ultrahuman Ring Air", "https://c.test/3"),
            ],
            now=NOW,
        )
        await db.commit()
        products = {p.brand: p for p in await list_products(db, None, 30)}
    assert products["Oura"].mentions == 2
    assert [r["url"] for r in products["Oura"].recent] == ["https://b.test/2", "https://a.test/1"]
    assert products["Ultrahuman"].mentions == 1


# --- product discovery ------------------------------------------------------------------


class FakeNews:
    name = "news"

    async def search(self, terms: list[str], **_: Any) -> NewsResults:
        articles = (
            [
                NewsArticle(
                    title="Oura Ring 4 launches with longer battery",
                    url="https://news.example/oura",
                    source="Tech Daily",
                )
            ]
            if terms == ["smart ring"]
            else []
        )
        return NewsResults(source="news", total=len(articles), articles=articles)


class FakeWeb:
    name = "web"

    async def search(self, query: str, **_: Any) -> list[WebResult]:
        return [
            WebResult(title="Oura on Amazon", url="https://www.amazon.com/oura"),
            WebResult(title="Oura Ring 4", url="https://ouraring.com/oura-ring-4"),
        ]


class NoFda:
    async def get(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("a smart ring is not an FDA category")


async def test_product_discovery_reads_feeds_and_news_then_confirms_on_the_brand_domain(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    feed = "https://feeds.example/social-test"
    async with factory() as db:
        db.add(
            ResearchFeedState(
                feed_url=feed,
                items=[
                    {
                        "title": "Hands on with the Oura Ring 4",
                        "link": "https://feeds.example/a",
                        "published": NOW.isoformat(),
                        "summary": "A sleep tracker review",
                        "publisher": "Feed A",
                    },
                ],
            )
        )
        await db.commit()
        report = RunReport()
        try:
            await discover_products(
                db,
                report,
                read_call=FakeReader(),
                news=FakeNews(),
                web=FakeWeb(),
                fda_http=NoFda(),
                settings=Settings(product_max_news_queries=12),
                now=NOW,
            )
            product = (await db.execute(sa.select(Product))).scalar_one()
        finally:
            await db.execute(
                sa.delete(ResearchFeedState).where(ResearchFeedState.feed_url == feed)
            )
            await db.commit()
    assert product.official_url == "https://ouraring.com/oura-ring-4"
    assert product.status == "confirmed"
    assert product.launch_status == "claimed"  # one outlet said "launches"
    status = report.collection_status()
    assert (
        status["news"]["status"] == "SUCCESS"
        and status["openfda"]["status"] == "EMPTY"
    )
    assert report.stats["mentions"] == 2


class NoNews:
    name = "news"

    async def search(self, terms: list[str], **_: Any) -> NewsResults:
        return NewsResults(source="news", total=0, articles=[])


class RecordingWeb:
    """Finds no official page, and can fail for the products named in ``down``."""

    name = "web"

    def __init__(self, down: tuple[str, ...] = ()) -> None:
        self.down = down
        self.asked: list[str] = []

    async def search(self, query: str, **_: Any) -> list[WebResult]:
        self.asked.append(query.split()[0])
        if query.split()[0] in self.down:
            raise ProviderUnavailable("it broke")
        return [WebResult(title="On Amazon", url="https://www.amazon.com/x")]


async def _named_only_on_youtube(db: AsyncSession, *brands: str) -> None:
    mentions = [
        Mention(
            brand=brand,
            product_name=f"{brand} Ring 4",
            category="smart_ring",
            mention_kind="review",
            quote=f"{brand} Ring",
            source_kind="youtube",
            source_url=f"https://www.youtube.com/watch?v={brand}",
            publisher=f"Channel {brand}",
            published_at=NOW,
        )
        for brand in brands
    ]
    await record_mentions(db, mentions, now=NOW)
    await db.commit()


async def _discover_with(db: AsyncSession, web: Any, *, lookups: int) -> RunReport:
    report = RunReport()
    await discover_products(
        db,
        report,
        read_call=None,
        news=NoNews(),
        web=web,
        fda_http=NoFda(),  # type: ignore[arg-type]
        settings=Settings(product_max_news_queries=0, product_max_confirmations=lookups),
        now=NOW,
    )
    await db.commit()
    return report


async def test_a_product_named_only_on_youtube_is_still_confirmed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The weekly social run records these and looks nothing up: 30 of 34 in dev, 2026-11-06."""
    async with factory() as db:
        await _named_only_on_youtube(db, "Oura")
        report = await _discover_with(db, FakeWeb(), lookups=10)
        product = (await db.execute(sa.select(Product))).scalar_one()
    assert product.official_url == "https://ouraring.com/oura-ring-4"
    assert product.status == "confirmed"
    assert report.stats["products_touched"] == 0 and report.stats["products_checked"] == 1


async def test_a_page_on_the_brands_site_must_name_the_product(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A rumoured product also finds the brand's homepage, and "confirmed" never falls."""

    class Homepage:
        name = "web"

        async def search(self, query: str, **_: Any) -> list[WebResult]:
            return [WebResult(title="Oura: the smart ring", url="https://ouraring.com/")]

    async with factory() as db:
        rumour = Mention(
            brand="Oura",
            product_name="Oura Ring 9",
            category="smart_ring",
            mention_kind="review",
            quote="Oura Ring 9",
            source_kind="youtube",
            source_url="https://www.youtube.com/watch?v=rumour",
            publisher="Channel",
            published_at=NOW,
        )
        await record_mentions(db, [rumour], now=NOW)
        await db.commit()
        await _discover_with(db, Homepage(), lookups=1)
        product = (await db.execute(sa.select(Product))).scalar_one()
    assert product.official_url is None and product.status == "uncertain"


async def test_a_category_page_on_the_brands_site_confirms_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Its title names the product line, so only the address tells it from a product's page."""

    class CategoryPage:
        name = "web"

        async def search(self, query: str, **_: Any) -> list[WebResult]:
            return [WebResult(title="Galaxy watches", url="https://www.samsung.com/us/watches/")]

    async with factory() as db:
        line = Mention(
            brand="Samsung",
            product_name="Galaxy watches",
            category="other",
            mention_kind="review",
            quote="Galaxy watches",
            source_kind="youtube",
            source_url="https://www.youtube.com/watch?v=line",
            publisher="Channel",
            published_at=NOW,
        )
        await record_mentions(db, [line], now=NOW)
        await db.commit()
        await _discover_with(db, CategoryPage(), lookups=1)
        product = (await db.execute(sa.select(Product))).scalar_one()
    assert product.official_url is None and product.status == "uncertain"


async def test_news_is_read_before_the_feed_backlog_fills_the_budget(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    feed = "https://feeds.example/social-backlog"
    async with factory() as db:
        db.add(
            ResearchFeedState(
                feed_url=feed,
                items=[
                    {
                        "title": f"A sleep tracker review {i}",
                        "link": f"https://feeds.example/{i}",
                        "publisher": "Feed A",
                    }
                    for i in range(BATCH + 5)
                ],
            )
        )
        await db.commit()
        report = RunReport()
        try:
            await discover_products(
                db,
                report,
                read_call=FakeReader(),
                news=FakeNews(),
                web=FakeWeb(),
                fda_http=NoFda(),  # type: ignore[arg-type]
                settings=Settings(product_max_news_queries=12, social_max_reading_batches=1),
                now=NOW,
            )
            kinds = set((await db.execute(sa.select(ProductObservation.source_kind))).scalars())
        finally:
            await db.execute(
                sa.delete(ResearchFeedState).where(ResearchFeedState.feed_url == feed)
            )
            await db.commit()
    assert kinds == {"news"}
    assert report.stats["reading_backlog"] > 0, "the budget must really have run out"


async def test_a_short_lookup_budget_gives_every_product_a_turn(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    web = RecordingWeb()
    async with factory() as db:
        await _named_only_on_youtube(db, "Aura", "Bora", "Cora")
        for _ in range(3):
            await _discover_with(db, web, lookups=1)
    assert sorted(web.asked) == ["Aura", "Bora", "Cora"]


async def test_a_lookup_that_could_not_run_keeps_the_products_turn(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as db:
        await _named_only_on_youtube(db, "Aura")
        report = await _discover_with(db, RecordingWeb(down=("Aura",)), lookups=1)
        product = (await db.execute(sa.select(Product))).scalar_one()
    assert product.confirmation_checked_at is None
    assert report.stats["products_checked"] == 0
    assert report.collection_status()["web"]["status"] == "ERROR"


# --- relations -----------------------------------------------------------------------------


async def test_relations_link_the_systems_and_report_their_own_numbers_untouched(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as db:
        await _covered_days(db, 14)
        await _week_of_posts(db, await _run(db))
        await _weekly(db, FakeReader())
        research = ResearchTopic(
            topic_key="creatine|sleep",
            label="Creatine for sleep",
            subject="creatine",
            outcome="sleep",
            category="supplements",
        )
        mesh = DiscoveryDescriptor(ui="D999991", name="Creatine", kind="substance")
        db.add_all([research, mesh])
        await db.flush()
        db.add(
            ResearchTopicObservation(
                topic_id=research.id,
                research_run_id=await _research_run(db),
                observed_at=NOW,
                window_start=NOW - timedelta(days=7),
                window_end=NOW,
                stories=5,
                independent_publishers=3,
                trend_score=61.5,
                data_coverage=0.8,
                trend_status="rising",
                metrics={},
                scores={},
            )
        )
        await db.commit()
        try:
            topic = (await db.execute(sa.select(SocialTopic))).scalar_one()
            items = await related_to_topic(
                db, subject=topic.subject, outcome=topic.outcome, social_topic_id=topic.id
            )
            score = await db.scalar(sa.select(ResearchTopicObservation.trend_score))
        finally:
            await db.execute(sa.delete(ResearchTopic).where(ResearchTopic.id == research.id))
            await db.execute(
                sa.text("DELETE FROM research_runs WHERE label = 'social_test_run'")
            )
            await db.execute(
                sa.delete(DiscoveryDescriptor).where(DiscoveryDescriptor.ui == "D999991")
            )
            await db.commit()
    by_kind = {item["kind"]: item for item in items}
    assert by_kind["research_topic"]["relation"] == "same_topic"
    assert by_kind["research_topic"]["own"]["trend_score"] == 61.5 == score
    assert by_kind["mesh_substance"]["id"] == "D999991"
    assert by_kind["product"]["relation"] == "named_in_posts"


async def _research_run(db: AsyncSession) -> str:
    run_id = await db.scalar(
        sa.text(
            "INSERT INTO research_runs (label, mode, status, params, config) "
            "VALUES ('social_test_run', 'manual', 'completed', '{}', '{}') RETURNING id"
        )
    )
    return str(run_id)


async def test_the_scheduler_queues_what_is_due_and_does_not_hammer_a_failure(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.social.schedule import INTERVALS, enqueue_due

    async with factory() as db:
        first = await enqueue_due(db, now=NOW)
        await db.commit()
        again = await enqueue_due(db, now=NOW + timedelta(minutes=15))  # still active
        await db.execute(sa.update(CollectionRun).values(status="failed", finished_at=NOW))
        await db.commit()
        soon = await enqueue_due(db, now=NOW + timedelta(minutes=30))  # failed, back off
        later = await enqueue_due(db, now=NOW + timedelta(hours=3))
    assert set(first) == set(INTERVALS) and again == [] and soon == []
    assert set(later) == set(INTERVALS)
    # The trend scan is queued only when the scheduler hands it an interval
    # (its own two settings), and then like any other kind.
    assert CollectionKind.MESH_SCAN not in first
    async with factory() as db:
        await db.execute(sa.update(CollectionRun).values(status="failed", finished_at=NOW))
        await db.commit()
        mesh = await enqueue_due(
            db,
            now=NOW + timedelta(hours=9),
            intervals={CollectionKind.MESH_SCAN: timedelta(hours=24)},
        )
        await db.commit()
    assert mesh == [CollectionKind.MESH_SCAN]


async def test_a_topic_found_this_week_counts_last_week_by_the_same_rule(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reader names the topic only on this week's posts; last week's posts
    match it by their words and must count too, or the topic reads as a surge."""

    class ThisWeekOnly(FakeReader):
        async def __call__(self, system: str, user: str) -> ReaderOutput:
            output = await super().__call__(system, user)
            for item, line in zip(output.items, user.splitlines(), strict=True):
                if "(new)" not in line:
                    item.is_wellness_topic = False
            return output

    async with factory() as db:
        await _covered_days(db, 14)
        run_id = await _run(db)
        for i in range(4):
            await save_video(
                db,
                _video(f"new{i}", title=f"Creatine for sleep (new) {i}"),
                run_id=run_id,
                now=NOW,
            )
            await save_video(
                db,
                _video(
                    f"old{i}",
                    title=f"Creatine for sleep {i}",
                    published_at=NOW - timedelta(days=9),
                ),
                run_id=run_id,
                now=NOW,
            )
        await db.commit()
        await _weekly(db, ThisWeekOnly())
        candidate = (await db.execute(sa.select(SocialCandidate))).scalar_one()
    windows = candidate.metrics["windows"]
    assert windows[0]["creators"] == windows[1]["creators"] == 4
    assert candidate.momentum["status"] == "steady"


async def test_tagging_needs_every_subject_and_outcome_word(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.social.store import tag_known_topics, upsert_topic

    async with factory() as db:
        run_id = await _run(db)
        topic_id = await upsert_topic(
            db, subject="creatine", outcome="sleep", category="supplements", now=NOW
        )
        before_bed = await save_video(
            db, _video("bed", title="Creatine before bed"), run_id=run_id, now=NOW
        )
        helps = await save_video(
            db,
            _video("helps", title="Does creatine help you sleep?"),
            run_id=run_id,
            now=NOW,
        )
        tagged = await tag_known_topics(
            db,
            {before_bed: "Creatine before bed", helps: "Does creatine help you sleep?"},
            now=NOW,
        )
        linked = set(
            (
                await db.execute(
                    sa.select(SocialPost.platform_post_id)
                    .join(SocialPostTopic, SocialPostTopic.post_id == SocialPost.id)
                    .where(SocialPostTopic.topic_id == topic_id)
                )
            ).scalars()
        )
    assert tagged == 1 and linked == {"helps"}


async def test_an_announcement_is_not_a_launch(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as db:
        touched = await record_mentions(
            db,
            [
                Mention(
                    "Oura",
                    "Ring 5",
                    "smart_ring",
                    "announcement",
                    "Ring 5",
                    "news",
                    f"https://{site}.example/x",
                    site,
                    None,
                )
                for site in ("a", "b")
            ],
            now=NOW,
        )
        await refresh_statuses(db, touched, now=NOW)
        product = (await db.execute(sa.select(Product))).scalar_one()
    assert product.status == "corroborated" and product.launch_status == "none"


async def test_a_product_relates_to_topics_through_its_category_words(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.intel.related import related_to_product
    from app.social.store import upsert_topic

    async with factory() as db:
        await upsert_topic(
            db, subject="blood sugar", outcome="", category="general_health", now=NOW
        )
        await upsert_topic(db, subject="creatine", outcome="", category="supplements", now=NOW)
        touched = await record_mentions(
            db,
            [
                Mention(
                    "Dexcom",
                    "Stelo",
                    "glucose_monitor",
                    "review",
                    "Dexcom Stelo",
                    "news",
                    "https://a.example/stelo",
                    "A",
                    None,
                    text="Dexcom Stelo, a glucose monitor you buy yourself",
                )
            ],
            now=NOW,
        )
        product = await db.get(Product, touched.pop())
        assert product is not None
        items = await related_to_product(db, product)
    labels = {(item["kind"], item["label"], item["relation"]) for item in items}
    assert ("social_topic", "Blood sugar", "category") in labels
    assert not any(label == "Creatine" for _, label, _ in labels)
