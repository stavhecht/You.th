"""What can happen to an article after it is published (needs a database).

Withdrawing takes it off every public surface with a recorded reason. A
correction is typed into its own draft column and only reaches readers when a
reviewer publishes it with a note. Rolled back after each test.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.domain.enums import ArticleStatus, Verdict
from app.domain.models import Article
from app.services.feed import ArticleNotFound, FeedService
from app.services.review import ReviewError, ReviewService
from tests.db.test_feed_search import _doc, _draft, _publish, _reviewer, _slug

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url


@pytest.fixture
async def session() -> Any:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT correction_draft FROM articles LIMIT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no database with migration 0008 at {DATABASE_URL}")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        try:
            yield db
        finally:
            await db.rollback()
    await engine.dispose()


# --- withdrawal ---------------------------------------------------------------


async def test_a_withdrawn_article_leaves_every_public_surface(session: AsyncSession) -> None:
    article_id = await _publish(session, headline="Zinc lozenges shorten colds")
    slug = await _slug(session, article_id)
    reviewer_id = await _reviewer(session)

    await ReviewService(session).withdraw(
        article_id, reviewer_id, "The key trial was retracted"
    )

    article = await session.get(Article, article_id, populate_existing=True)
    assert article is not None
    assert article.status == ArticleStatus.WITHDRAWN
    assert article.withdrawal_reason == "The key trial was retracted"
    assert article.withdrawn_by == reviewer_id
    with pytest.raises(ArticleNotFound):
        await FeedService(session).article(slug)
    found, _, _ = await FeedService(session).search("zinc lozenges")
    assert slug not in [card["slug"] for card in found]


async def test_only_a_published_article_can_be_withdrawn(session: AsyncSession) -> None:
    article_id = await _draft(session)
    with pytest.raises(ReviewError, match="pending_review"):
        await ReviewService(session).withdraw(article_id, await _reviewer(session), "why")


async def test_the_database_refuses_a_withdrawal_without_a_reason(
    session: AsyncSession,
) -> None:
    article_id = await _publish(session)
    with pytest.raises(IntegrityError):
        await session.execute(
            sa.text("UPDATE articles SET status = 'withdrawn' WHERE id = :id"),
            {"id": article_id},
        )


# --- corrections ----------------------------------------------------------------


async def test_typing_a_correction_does_not_change_what_readers_see(
    session: AsyncSession,
) -> None:
    article_id = await _publish(session, content=_doc("The approved text."))
    slug = await _slug(session, article_id)
    reviewer_id = await _reviewer(session)

    await ReviewService(session).save_correction(
        article_id, _doc("Half a correc"), reviewer_id
    )

    public = await FeedService(session).article(slug)
    assert "The approved text." in str(public["content"])
    article = await session.get(Article, article_id, populate_existing=True)
    assert article is not None and article.correction_draft == _doc("Half a correc")


async def test_publishing_a_correction_replaces_the_text_and_records_the_note(
    session: AsyncSession,
) -> None:
    article_id = await _publish(session, content=_doc("Zinc cuts colds by three days."))
    slug = await _slug(session, article_id)
    reviewer_id = await _reviewer(session)
    service = ReviewService(session)

    await service.save_correction(article_id, _doc("Zinc cuts colds by two days."), reviewer_id)
    await service.publish_correction(article_id, reviewer_id, "Three days should read two.")

    public = await FeedService(session).article(slug)
    assert "two days" in str(public["content"])
    assert [c["note"] for c in public["corrections"]] == ["Three days should read two."]
    article = await session.get(Article, article_id, populate_existing=True)
    assert article is not None
    assert article.correction_draft is None
    assert article.status == ArticleStatus.PUBLISHED
    assert "two days" in (article.card_excerpt or "")
    # And search finds the corrected wording, not the old one.
    found, _, _ = await FeedService(session).search("two days")
    assert slug in [card["slug"] for card in found]


async def test_a_correction_needs_a_draft_and_resolving_citations(
    session: AsyncSession,
) -> None:
    article_id = await _publish(session)
    reviewer_id = await _reviewer(session)
    service = ReviewService(session)

    with pytest.raises(ReviewError, match="no correction"):
        await service.publish_correction(article_id, reviewer_id, "note")

    await service.save_correction(article_id, _doc("A new claim.", cite="S9"), reviewer_id)
    with pytest.raises(ReviewError, match="S9"):
        await service.publish_correction(article_id, reviewer_id, "note")


# --- the reviewer's headline, summary and verdict -------------------------------


async def test_editing_the_summary_keeps_what_the_model_wrote(session: AsyncSession) -> None:
    article_id = await _draft(session, headline="The model's headline")
    service = ReviewService(session)

    await service.set_meta(article_id, await _reviewer(session), headline="A better headline")
    await service.set_meta(article_id, await _reviewer(session), summary="A better summary.")

    article = await session.get(Article, article_id, populate_existing=True)
    assert article is not None
    assert (article.headline, article.summary) == ("A better headline", "A better summary.")
    assert article.original_meta is not None
    assert article.original_meta["headline"] == "The model's headline"
    assert article.original_meta["summary"] == "A test summary."


async def test_a_verdict_cannot_be_raised_past_the_evidence_ceiling(
    session: AsyncSession,
) -> None:
    article_id = await _draft(session)
    await session.execute(
        sa.text(
            "UPDATE articles SET verdict = 'weak',"
            " validation_report = CAST(:report AS jsonb) WHERE id = :id"
        ),
        {"id": article_id, "report": '{"verdict_ceiling": "mixed"}'},
    )
    service = ReviewService(session)
    reviewer_id = await _reviewer(session)

    await service.set_meta(article_id, reviewer_id, verdict=Verdict.MIXED)
    with pytest.raises(ReviewError, match="ceiling"):
        await service.set_meta(article_id, reviewer_id, verdict=Verdict.SUPPORTED)

    article = await session.get(Article, article_id, populate_existing=True)
    assert article is not None and article.verdict == Verdict.MIXED


async def test_meta_is_frozen_once_published(session: AsyncSession) -> None:
    article_id = await _publish(session)
    with pytest.raises(ReviewError, match="published"):
        await ReviewService(session).set_meta(
            article_id, await _reviewer(session), headline="Changed after the fact"
        )


async def test_an_edit_that_deletes_every_citation_cannot_be_published(
    session: AsyncSession,
) -> None:
    """``approve`` checked only that cited handles exist, so citing nothing passed."""
    article_id = await _draft(session, content=_doc("Zinc shortens colds.", cite="S1"))
    reviewer_id = await _reviewer(session)
    service = ReviewService(session)

    await service.save_edits(article_id, _doc("Zinc shortens colds."), reviewer_id)
    with pytest.raises(ReviewError, match="no longer cites any source"):
        await service.approve(article_id, reviewer_id)


# --- the desk's worker status line -----------------------------------------------


async def test_a_run_nobody_claims_shows_as_stalled(session: AsyncSession) -> None:
    from app.api.console.status import worker_status

    await session.execute(
        sa.text(
            "INSERT INTO pipeline_runs (topic, status, created_at)"
            " VALUES ('status line test', 'queued', now() - interval '10 minutes')"
        )
    )
    status = await worker_status(session)

    assert status.queued_articles >= 1
    assert status.oldest_queued_seconds is not None and status.oldest_queued_seconds >= 600
    # Stalled exactly when nothing is running; a dev worker mid-run is not stalled.
    assert status.stalled == (not status.running)


# --- the byline -------------------------------------------------------------------


async def test_the_article_names_the_reviewer_who_approved_it_and_not_their_email(
    session: AsyncSession,
) -> None:
    slug = await _slug(session, await _publish(session))

    public = await FeedService(session).article(slug)

    assert public["reviewer_name"] == "Test Reviewer"
    assert "@" not in str(public)  # no address anywhere in what readers are sent
