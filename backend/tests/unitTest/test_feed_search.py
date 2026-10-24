"""Public search (migration 0006), against a real database.

What a fake session cannot show: that ``approve()`` indexes what the reviewer
approved through the SQL function the backfill used, that a draft never
answers a search, that citation markers and generation input stay out of the
index, and that the typo fallback only answers when full text found nothing.

Articles are published through ``ReviewService.approve`` — the real path — and
named with made-up words ("zorbulite") so the assertions hold against a
database that already holds published articles. Skipped, like the other DB
suites, when no database is reachable.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.domain.enums import ArticleStatus, StudyType, UserRole, Verdict
from app.security.auth import hash_password
from app.services.feed import (
    MAX_SEARCH_RESULTS,
    FeedService,
    _decode_search_cursor,
    _encode_search_cursor,
)
from app.services.review import ReviewService

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url


@pytest.fixture
async def session() -> Any:
    """Per-test engine, rolled back afterwards. See test_db_invariants.py."""
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no database at {DATABASE_URL}")

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        try:
            yield db
        finally:
            await db.rollback()
    await engine.dispose()


def _doc(*paragraphs: str, cite: str | None = None) -> dict:
    """A body whose first paragraph is beat 1, optionally ending in a citation."""
    content: list[dict] = []
    for index, text in enumerate(paragraphs):
        inline: list[dict] = [{"type": "text", "text": text}]
        if cite and index == 0:
            inline.append({"type": "citation", "attrs": {"sourceIds": [cite]}})
        node: dict = {"type": "paragraph", "content": inline}
        if index == 0:
            node["attrs"] = {"beat": 1}
        content.append(node)
    return {"type": "doc", "content": content}


async def _draft(
    db: AsyncSession,
    *,
    headline: str = "A test headline",
    product: str = "Test Product",
    topic: str = "test topic",
    content: dict | None = None,
    edited: dict | None = None,
) -> str:
    article_id = str(uuid.uuid4())
    await db.execute(
        sa.text(
            """
            INSERT INTO articles (
                id, slug, status, topic, product, headline, summary, verdict,
                original_content, edited_content, evidence_grade,
                created_at, updated_at
            ) VALUES (
                :id, :slug, 'pending_review', :topic, :product, :headline,
                'A test summary.', :verdict, CAST(:content AS jsonb),
                CAST(:edited AS jsonb), :grade, now(), now()
            )
            """
        ),
        {
            "id": article_id,
            "slug": f"search-{article_id[:8]}",
            "topic": topic,
            "product": product,
            "headline": headline,
            "verdict": Verdict.MIXED.value,
            "content": json.dumps(content or _doc("It claims to help.")),
            "edited": json.dumps(edited) if edited is not None else None,
            "grade": StudyType.RCT.value,
        },
    )
    return article_id


async def _reviewer(db: AsyncSession) -> str:
    user_id = str(uuid.uuid4())
    await db.execute(
        sa.text(
            "INSERT INTO users (id, email, password_hash, display_name, role, created_at)"
            " VALUES (:id, :email, :hash, 'Test Reviewer', :role, now())"
        ),
        {
            "id": user_id,
            "email": f"reviewer-{user_id[:8]}@example.com",
            "hash": hash_password("a-sufficiently-long-password"),
            "role": UserRole.REVIEWER.value,
        },
    )
    return user_id


async def _publish(db: AsyncSession, **draft: Any) -> str:
    article_id = await _draft(db, **draft)
    await ReviewService(db).approve(article_id, await _reviewer(db))
    return article_id


async def _slugs(db: AsyncSession, query: str) -> tuple[list[str], str]:
    items, _, match = await FeedService(db).search(query)
    return [item["slug"] for item in items], match


async def _slug(db: AsyncSession, article_id: str) -> str:
    query = sa.text("SELECT slug FROM articles WHERE id = :id")
    return (await db.execute(query, {"id": article_id})).scalar_one()


async def test_approve_indexes_the_body_and_search_finds_it(session: AsyncSession) -> None:
    article_id = await _publish(
        session,
        content=_doc("It claims to help.", "Trials of zorbulite found a small effect."),
    )

    slugs, match = await _slugs(session, "zorbulite")

    assert slugs == [await _slug(session, article_id)]
    assert match == "text"


async def test_stemming_finds_another_form_of_the_word(session: AsyncSession) -> None:
    article_id = await _publish(session, product="Qwertium", headline="Qwertium and sleep")

    slugs, _ = await _slugs(session, "qwertiums")

    assert await _slug(session, article_id) in slugs


async def test_a_draft_never_answers_a_search(session: AsyncSession) -> None:
    """Even with a search document written onto it, which approve() never does.

    The index is partial on published, but that is an optimisation; this pins
    that ``_published_only`` is what keeps drafts out.
    """
    for status in ArticleStatus:
        if status is ArticleStatus.PUBLISHED:
            continue
        article_id = await _draft(session, product="Vexorbane")
        await session.execute(
            sa.text(
                "UPDATE articles a SET status = CAST(:status AS article_status),"
                " rejection_reason = :reason,"
                " search_document = article_search_document(a) WHERE a.id = :id"
            ),
            {
                "status": status.value,
                "reason": "x" if status is ArticleStatus.REJECTED else None,
                "id": article_id,
            },
        )

    slugs, _ = await _slugs(session, "vexorbane")

    assert slugs == []


async def test_the_edited_body_is_indexed_not_the_original(session: AsyncSession) -> None:
    article_id = await _publish(
        session,
        content=_doc("It claims to help.", "The model wrote about glimmerfrost."),
        edited=_doc("It claims to help.", "The reviewer wrote about pellucidane."),
    )
    slug = await _slug(session, article_id)

    assert (await _slugs(session, "pellucidane"))[0] == [slug]
    assert slug not in (await _slugs(session, "glimmerfrost"))[0]


async def test_citation_markers_and_the_topic_are_not_indexed(session: AsyncSession) -> None:
    """Asked of the function directly: approve() would refuse the citation,
    which has no source row here, before indexing anything."""
    article_id = await _draft(
        session,
        topic="is quintrazine bad for you",
        content=_doc("It claims to help.", cite="S1"),
    )

    lexemes = (
        await session.execute(
            sa.text(
                "SELECT tsvector_to_array(article_search_document(a))"
                " FROM articles a WHERE a.id = :id"
            ),
            {"id": article_id},
        )
    ).scalar_one()

    assert "s1" not in lexemes
    assert "quintrazin" not in lexemes
    assert "help" in lexemes


async def test_a_misspelling_falls_back_to_similar_titles(session: AsyncSession) -> None:
    article_id = await _publish(
        session, product="Zorbulite", headline="Zorbulite improves recovery"
    )

    # Not "zorbulit": the English stemmer reduces "zorbulite" to exactly that,
    # so full text answers it. A typo has to survive stemming to need the
    # fallback.
    slugs, match = await _slugs(session, "zorbullite")

    assert match == "similar"
    assert await _slug(session, article_id) in slugs


async def test_an_exclusion_is_not_undone_by_the_fallback(session: AsyncSession) -> None:
    """Measured: ``creatine -muscle`` excluded the article, then the fallback
    returned it, because trigram similarity cannot see the minus."""
    article_id = await _publish(
        session, product="Zorbulite", headline="Zorbulite improves muscle recovery"
    )

    slugs, match = await _slugs(session, "zorbulite -muscle")

    assert await _slug(session, article_id) not in slugs
    assert match == "text"


def test_a_search_cursor_round_trips_and_refuses_anything_else() -> None:
    assert _decode_search_cursor(_encode_search_cursor("similar", 24)) == ("similar", 24)

    with pytest.raises(ValueError):
        _decode_search_cursor(_encode_search_cursor("text", MAX_SEARCH_RESULTS))
    with pytest.raises(ValueError):
        _decode_search_cursor("bm90IGEgY3Vyc29y")  # "not a cursor"
