"""Status-changing review writes lock the article row (needs a database).

``approve``, ``save_edits`` and ``reject`` each read the status and then write.
Unlocked, an autosave landing while Approve is in flight read "pending", waited
on Approve's row update, and then wrote ``edited_content`` onto the published
article — text nobody approved, under a card derived from the older text.

Two sessions, so the rows are committed (and deleted afterwards) rather than
rolled back like the other DB suites. Skipped when no database is reachable.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.services.review import EditConflict, ReviewError, ReviewService
from tests.unitTest.test_feed_search import _doc, _draft, _reviewer

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url


@pytest.fixture
async def factory() -> Any:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no database at {DATABASE_URL}")
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest.fixture
async def draft(factory: Any) -> Any:
    async with factory() as db:
        article_id = await _draft(db, headline="Locking test headline")
        reviewer_id = await _reviewer(db)
        await db.commit()
    yield article_id, reviewer_id
    async with factory() as db:
        await db.execute(sa.text("DELETE FROM articles WHERE id = :id"), {"id": article_id})
        await db.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": reviewer_id})
        await db.commit()


async def test_an_autosave_racing_approve_cannot_edit_the_published_article(
    factory: Any, draft: tuple[str, str]
) -> None:
    article_id, reviewer_id = draft
    async with factory() as approving, factory() as saving:
        await ReviewService(approving).approve(article_id, reviewer_id)  # not committed yet

        late_save = asyncio.create_task(
            ReviewService(saving).save_edits(article_id, _doc("Unreviewed text."), reviewer_id)
        )
        await asyncio.sleep(0.3)  # let the save reach the database and queue on the row
        await approving.commit()

        with pytest.raises(ReviewError, match="published"):
            await late_save

    async with factory() as db:
        edited = await db.scalar(
            sa.text("SELECT edited_content FROM articles WHERE id = :id"), {"id": article_id}
        )
    assert edited is None


async def test_a_locked_write_rereads_a_row_this_session_already_loaded(
    factory: Any, draft: tuple[str, str]
) -> None:
    """The illustration route loads the article, calls the provider, then writes.
    If it was published in between, the locked re-read must see that rather than
    the copy already in this session."""
    article_id, reviewer_id = draft
    async with factory() as regenerating:
        # Held, as the route holds it: the identity map is weak, and a dropped
        # reference would be re-queried anyway, hiding the stale copy.
        loaded = await ReviewService(regenerating).article_for_illustration(article_id)

        async with factory() as approving:
            await ReviewService(approving).approve(article_id, reviewer_id)
            await approving.commit()

        with pytest.raises(ReviewError, match="published"):
            await ReviewService(regenerating).set_illustration(article_id, {}, reviewer_id)
        assert loaded.status == "published"  # the same object, refreshed by the locked read


async def test_an_autosave_from_an_older_copy_cannot_undo_another_reviewers_edit(
    factory: Any, draft: tuple[str, str]
) -> None:
    """Autosave sends the whole document. Reviewer B, who opened the draft before
    reviewer A saved, used to overwrite A's edit with B's older copy."""
    article_id, reviewer_id = draft
    a_edit = _doc("Reviewer A fixed this sentence.")
    async with factory() as db:
        await ReviewService(db).save_edits(article_id, a_edit, reviewer_id, base=None)
        await db.commit()

    async with factory() as db:
        with pytest.raises(EditConflict):
            await ReviewService(db).save_edits(
                article_id, _doc("Reviewer B, from the old copy."), reviewer_id, base=None
            )
        # Starting from what is stored is fine, and so is a save with no base.
        await ReviewService(db).save_edits(
            article_id, _doc("A again."), reviewer_id, base=a_edit
        )
        await ReviewService(db).save_edits(article_id, _doc("Unconditional."), reviewer_id)
        await db.commit()
