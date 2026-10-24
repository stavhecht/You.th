"""Invariants enforced by Postgres, not by application code.

These need a live database. That is the entire point: invariants #1 and #4 are
guaranteed by a CHECK constraint and a trigger precisely so they hold when the
application layer is wrong, and a fake session cannot demonstrate that.

Run with a database available:

    docker compose up -d db
    python -m scripts.migrate
    pytest tests/unitTest/test_db_invariants.py

The URL comes from ``TEST_DATABASE_URL`` if set, otherwise from the application
settings — so it follows ``.env`` and cannot drift from the port compose
actually publishes. ``psql`` is not usable here: ``DATABASE_URL`` carries the
SQLAlchemy ``+asyncpg`` driver suffix, which psql cannot parse.

Skipped automatically when no database is reachable, so the default `pytest`
run stays fast. **A skipped run does not verify these invariants** — run them
before trusting a change to the schema, the review service, or the persist
stage.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.domain.enums import ArticleStatus, RunStatus, StudyType, UserRole, Verdict
from app.domain.models import Article, ArticleSource, PipelineStageRun
from app.security.auth import hash_password
from app.services.media import load_image, store_image
from app.services.review import ReviewError, ReviewService

#: Fall back to the application's own setting rather than a second hardcoded
#: URL. The compose file publishes the database on **5433**, not 5432, to avoid
#: colliding with a native Homebrew Postgres — a literal here silently drifts
#: from that and turns the whole module into skips, which look like passes.
DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url


@pytest.fixture
async def session() -> Any:
    """A session whose writes are rolled back after each test.

    The engine is built **per test**, not once per module. asyncpg connections
    bind to the event loop that created them, and pytest-asyncio gives each
    test its own loop — a module-scoped engine hands every test after the first
    a connection attached to a dead loop ("got Future attached to a different
    loop"). Rebuilding costs a few milliseconds against a local database and
    removes the whole class of problem.
    """
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
            # Roll back rather than truncate: every test's writes disappear
            # without the tests needing to know about each other's data.
            await db.rollback()
    await engine.dispose()


async def _make_user(db: AsyncSession) -> str:
    user_id = str(uuid.uuid4())
    await db.execute(
        sa.text(
            "INSERT INTO users (id, email, password_hash, display_name, role, created_at)"
            " VALUES (:id, :email, :hash, :name, :role, now())"
        ),
        {
            "id": user_id,
            "email": f"reviewer-{user_id[:8]}@example.com",
            "hash": hash_password("a-sufficiently-long-password"),
            "name": "Test Reviewer",
            "role": UserRole.REVIEWER.value,
        },
    )
    return user_id


async def _make_article(
    db: AsyncSession,
    *,
    status: ArticleStatus = ArticleStatus.PENDING_REVIEW,
    content: dict | None = None,
) -> str:
    article_id = str(uuid.uuid4())
    # `rejected` carries a NOT-NULL-by-CHECK reason, so the helper supplies one
    # rather than every caller having to know that.
    reason = "test rejection" if status is ArticleStatus.REJECTED else None
    await db.execute(
        sa.text(
            """
            INSERT INTO articles (
                id, slug, status, topic, product, headline, summary, verdict,
                original_content, evidence_grade, rejection_reason,
                created_at, updated_at
            ) VALUES (
                :id, :slug, :status, :topic, :product, :headline, :summary,
                :verdict, CAST(:content AS jsonb), :grade, :reason, now(), now()
            )
            """
        ),
        {
            "id": article_id,
            "slug": f"test-{article_id[:8]}",
            "status": status.value,
            "topic": "test topic",
            "product": "Test Product",
            "headline": "A test headline",
            "summary": "A test summary.",
            "verdict": Verdict.MIXED.value,
            "content": _json(content if content is not None else {}),
            "grade": StudyType.RCT.value,
            "reason": reason,
        },
    )
    return article_id


def _json(value: dict) -> str:
    import json

    return json.dumps(value)


# ---------------------------------------------------------------------------
# Invariant #1 — nothing publishes without a human.
# ---------------------------------------------------------------------------


async def test_publishing_without_a_reviewer_violates_the_check_constraint(
    session: AsyncSession,
) -> None:
    """Tested with raw SQL, bypassing the service layer deliberately.

    The constraint exists for exactly the case where the service layer is
    wrong, so testing it through the service would prove nothing.
    """
    article_id = await _make_article(session, content={"type": "doc", "content": []})

    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(
            sa.text(
                "UPDATE articles SET status = 'published', published_at = now()"
                " WHERE id = :id"
            ),
            {"id": article_id},
        )
        await session.flush()


async def test_approve_sets_reviewer_and_timestamps(session: AsyncSession) -> None:
    reviewer_id = await _make_user(session)
    article_id = await _make_article(
        session,
        content={
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "attrs": {"beat": 1},
                    "content": [{"type": "text", "text": "It claims to help."}],
                }
            ],
        },
    )

    await ReviewService(session).approve(article_id, reviewer_id)

    row = (
        await session.execute(
            sa.text(
                "SELECT status, reviewed_by, reviewed_at, published_at, card_excerpt"
                " FROM articles WHERE id = :id"
            ),
            {"id": article_id},
        )
    ).one()

    assert row.status == "published"
    assert str(row.reviewed_by) == reviewer_id
    assert row.reviewed_at is not None
    assert row.published_at is not None
    # Card fields are materialised at publish time, from approved content.
    assert row.card_excerpt == "It claims to help."


async def test_validation_failed_article_cannot_be_approved(
    session: AsyncSession,
) -> None:
    """A draft that failed validation is not approvable into existence."""
    reviewer_id = await _make_user(session)
    article_id = await _make_article(
        session,
        status=ArticleStatus.VALIDATION_FAILED,
        content={"type": "doc", "content": []},
    )

    with pytest.raises(ReviewError, match="validation_failed"):
        await ReviewService(session).approve(article_id, reviewer_id)


async def test_validation_failed_article_can_be_discarded_by_rejecting_it(
    session: AsyncSession,
) -> None:
    """The discard path. Unapprovable is not the same as stuck.

    Rejection is the only way a draft leaves a queue tab — there is no delete —
    so it has to be reachable from ``validation_failed``. The reason is
    recorded, which is the whole argument for rejecting rather than deleting.
    """
    reviewer_id = await _make_user(session)
    article_id = await _make_article(
        session,
        status=ArticleStatus.VALIDATION_FAILED,
        content={"type": "doc", "content": []},
    )

    await ReviewService(session).reject(article_id, reviewer_id, "unsalvageable draft")

    row = (
        await session.execute(
            sa.text(
                "SELECT status, rejection_reason, reviewed_by FROM articles WHERE id = :id"
            ),
            {"id": article_id},
        )
    ).one()

    assert row.status == "rejected"
    assert row.rejection_reason == "unsalvageable draft"
    assert str(row.reviewed_by) == reviewer_id


async def test_published_article_cannot_be_rejected(session: AsyncSession) -> None:
    """Widening the rejectable set must not turn reject into an unpublish."""
    reviewer_id = await _make_user(session)
    article_id = await _make_article(
        session,
        content={
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "attrs": {"beat": 1},
                    "content": [{"type": "text", "text": "It claims to help."}],
                }
            ],
        },
    )
    await ReviewService(session).approve(article_id, reviewer_id)

    with pytest.raises(ReviewError, match="published"):
        await ReviewService(session).reject(article_id, reviewer_id, "changed my mind")


async def test_rejected_article_requires_a_reason(session: AsyncSession) -> None:
    article_id = await _make_article(session, content={"type": "doc", "content": []})

    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(
            sa.text("UPDATE articles SET status = 'rejected' WHERE id = :id"),
            {"id": article_id},
        )
        await session.flush()


# ---------------------------------------------------------------------------
# Invariant #4 — the AI draft is immutable.
# ---------------------------------------------------------------------------


async def test_updating_original_content_is_rejected_by_the_trigger(
    session: AsyncSession,
) -> None:
    """Even a direct UPDATE cannot rewrite what the model wrote."""
    article_id = await _make_article(
        session, content={"type": "doc", "content": [{"type": "paragraph"}]}
    )

    with pytest.raises((IntegrityError, DBAPIError), match="immutable"):
        await session.execute(
            sa.text(
                "UPDATE articles SET original_content = CAST(:content AS jsonb)"
                " WHERE id = :id"
            ),
            {"id": article_id, "content": _json({"type": "doc", "content": []})},
        )
        await session.flush()


async def test_edits_land_in_edited_content_and_original_is_untouched(
    session: AsyncSession,
) -> None:
    """The audit trail: what the model wrote vs. what went live."""
    original = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "attrs": {"beat": 1},
                "content": [{"type": "text", "text": "Original wording."}],
            }
        ],
    }
    reviewer_id = await _make_user(session)
    article_id = await _make_article(session, content=original)

    edited = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "attrs": {"beat": 1},
                "content": [{"type": "text", "text": "Human wording."}],
            }
        ],
    }
    await ReviewService(session).save_edits(article_id, edited, reviewer_id)

    row = (
        await session.execute(
            sa.text(
                "SELECT original_content, edited_content FROM articles WHERE id = :id"
            ),
            {"id": article_id},
        )
    ).one()

    assert row.original_content == original
    assert row.edited_content == edited


async def test_an_unedited_article_stores_sql_null_not_json_null(
    session: AsyncSession,
) -> None:
    """Invariant #4's read side, at the SQL layer.

    ``PersistStage`` writes ``edited_content=None`` on every article it creates.
    SQLAlchemy's JSON types default to ``none_as_null=False``, which persists
    that as the JSON literal ``null`` — and ``COALESCE`` treats a JSON ``null``
    as a present value, so the feed renders nothing instead of the AI draft.

    Python readers hide it (``json.loads('null')`` is ``None``, so
    ``edited_content or original_content`` is correct), which is why this needs
    a database to catch: it only appears in the SQL that ``0001_initial.sql``
    and ``services/card.py`` both document as the mechanism.
    """
    content = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "attrs": {"beat": 1},
                "content": [{"type": "text", "text": "The draft."}],
            }
        ],
    }
    article = Article(
        slug=f"unedited-{uuid.uuid4().hex[:8]}",
        status=ArticleStatus.PENDING_REVIEW,
        topic="ashwagandha",
        product="Ashwagandha capsules",
        target_claims=["reduces stress"],
        ingredients=["ashwagandha"],
        headline="Does ashwagandha reduce stress?",
        summary="A summary.",
        verdict=Verdict.MIXED,
        original_content=content,
        edited_content=None,
        evidence_grade=StudyType.RCT,
    )
    session.add(article)
    await session.flush()

    row = (
        await session.execute(
            sa.text(
                "SELECT edited_content IS NULL AS is_sql_null,"
                " COALESCE(edited_content, original_content) AS rendered"
                " FROM articles WHERE id = :id"
            ),
            {"id": article.id},
        )
    ).one()

    assert row.is_sql_null is True
    assert row.rendered == content


async def test_a_source_shown_without_excerpts_stores_sql_null(
    session: AsyncSession,
) -> None:
    """The same trap on ``article_sources.excerpts``.

    PersistStage writes ``excerpts=None`` for every source the model saw no
    full text for — nearly all of them. Stored as JSON ``null``, that would
    count as "excerpts recorded" to ``WHERE excerpts IS NOT NULL``.
    """
    article_id = await _make_article(session)
    source_ids = [
        str(
            await session.scalar(
                sa.text(
                    "INSERT INTO sources (pmid, title, abstract, source_api, created_at,"
                    " last_seen_at) VALUES (:pmid, 'A paper', 'An abstract.', 'pubmed',"
                    " now(), now()) RETURNING id"
                ),
                {"pmid": pmid},
            )
        )
        for pmid in ("97770001", "97770002")
    ]
    excerpts = [{"section": "Results", "text": "Cortisol fell by 23%."}]
    session.add_all(
        [
            ArticleSource(
                article_id=article_id,
                source_id=source_ids[0],
                claim="reduces stress",
                citation_handle="S1",
                excerpts=excerpts,
            ),
            ArticleSource(
                article_id=article_id,
                source_id=source_ids[1],
                claim="reduces stress",
                citation_handle="S2",
                excerpts=None,
            ),
        ]
    )
    await session.flush()

    rows = (
        await session.execute(
            sa.text(
                "SELECT citation_handle, excerpts, excerpts IS NULL AS is_sql_null"
                " FROM article_sources WHERE article_id = :id ORDER BY citation_handle"
            ),
            {"id": article_id},
        )
    ).all()

    assert [(row.citation_handle, row.excerpts, row.is_sql_null) for row in rows] == [
        ("S1", excerpts, False),
        ("S2", None, True),
    ]


async def test_a_stage_that_succeeded_stores_sql_null_in_error(
    session: AsyncSession,
) -> None:
    """Same defect class, and the one that actually fires on every run: the
    orchestrator assigns ``error=None`` to each stage it records as succeeded,
    so ``WHERE error IS NULL`` would match no succeeded stage at all."""
    run_id = str(uuid.uuid4())
    await session.execute(
        sa.text(
            "INSERT INTO pipeline_runs (id, topic, status, created_at)"
            " VALUES (:id, :topic, 'succeeded', now())"
        ),
        {"id": run_id, "topic": "ashwagandha"},
    )
    session.add(
        PipelineStageRun(
            run_id=run_id,
            stage="retrieve",
            ordinal=1,
            status=RunStatus.SUCCEEDED,
            error=None,
            metrics={"candidates_total": 12},
        )
    )
    await session.flush()

    row = (
        await session.execute(
            sa.text(
                "SELECT count(*) AS matched FROM pipeline_stage_runs"
                " WHERE run_id = :id AND error IS NULL"
            ),
            {"id": run_id},
        )
    ).one()

    assert row.matched == 1


# ---------------------------------------------------------------------------
# Invariant #2, surviving human editing.
# ---------------------------------------------------------------------------


async def test_approve_rejects_a_citation_the_editor_invented(
    session: AsyncSession,
) -> None:
    """A reviewer pasting an unknown handle must not be able to publish it."""
    reviewer_id = await _make_user(session)
    article_id = await _make_article(session, content={"type": "doc", "content": []})

    edited = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "attrs": {"beat": 2},
                "content": [
                    {"type": "text", "text": "A trial found an effect "},
                    {"type": "citation", "attrs": {"sourceIds": ["S99"]}},
                ],
            }
        ],
    }
    await ReviewService(session).save_edits(article_id, edited, reviewer_id)

    with pytest.raises(ReviewError, match="S99"):
        await ReviewService(session).approve(article_id, reviewer_id)


# ---------------------------------------------------------------------------
# The sources cache CHECK constraint.
# ---------------------------------------------------------------------------


async def test_source_without_any_identifier_is_rejected(session: AsyncSession) -> None:
    """A source that cannot resolve to a real paper must not exist.

    This is what lets "4/4 citations resolve" mean something.
    """
    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(
            sa.text(
                "INSERT INTO sources (id, title, abstract, source_api, created_at,"
                " last_seen_at) VALUES (gen_random_uuid(), 'T', 'A', 'pubmed',"
                " now(), now())"
            )
        )
        await session.flush()


async def test_published_feed_query_excludes_every_other_status(
    session: AsyncSession,
) -> None:
    """The public surface's visibility rule, checked at the SQL level."""
    for status in ArticleStatus:
        if status is ArticleStatus.PUBLISHED:
            continue
        await _make_article(session, status=status, content={"type": "doc", "content": []})
    await session.flush()

    count = (
        await session.execute(
            sa.text(
                "SELECT count(*) FROM articles WHERE status = 'published'"
                " AND (reviewed_by IS NULL OR published_at IS NULL)"
            )
        )
    ).scalar_one()

    assert count == 0


# ---------------------------------------------------------------------------
# The media store. `prepare_image` is unit-tested in test_media.py without a
# database; these are the parts only Postgres can answer for — that the bytes
# survive a round trip, that the digest key really dedups, and that the CHECK
# constraints refuse a row the URL regex would never produce.
# ---------------------------------------------------------------------------


async def test_stored_image_bytes_survive_the_round_trip(session: AsyncSession) -> None:
    """A bytea column is not a text column, and this is where that is proved.

    Published articles link at these bytes, so a truncation or an encoding
    round trip would be a broken picture on a live page.
    """
    data = bytes(range(256)) * 8
    data = b"\x89PNG\r\n\x1a\n" + data

    stored = await store_image(data, session=session)
    await session.flush()

    digest = hashlib.sha256(data).hexdigest()
    row = await load_image(digest, "png", session=session)

    assert row is not None
    assert row.data == data
    assert stored.src.endswith(f"{digest[2:]}.png")
    assert stored.size == len(data)


async def test_storing_the_same_image_twice_is_one_row(session: AsyncSession) -> None:
    """Content addressing, at the level that actually enforces it.

    ON CONFLICT DO NOTHING rather than an error: two reviewers uploading the
    same picture, or a pipeline redrawing into an identical result, is a normal
    event and not a failure.
    """
    data = b"GIF89a" + b"\x11" * 64

    first = await store_image(data, session=session)
    second = await store_image(data, session=session)
    await session.flush()

    assert first.src == second.src
    count = (
        await session.execute(
            sa.text("SELECT count(*) FROM media_objects WHERE digest = :d"),
            {"d": hashlib.sha256(data).hexdigest()},
        )
    ).scalar_one()
    assert count == 1


async def test_a_digest_that_is_not_a_sha256_is_refused(session: AsyncSession) -> None:
    """The CHECK backing the URL shape.

    ``MEDIA_SRC_RE`` keeps a bad path out of a document; this keeps a row out
    of the table that could never be addressed by a legal path.
    """
    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(
            sa.text(
                "INSERT INTO media_objects (digest, extension, data)"
                " VALUES ('../../etc/passwd', 'png', '\\x00'::bytea)"
            )
        )
        await session.flush()


async def test_an_unsupported_extension_is_refused(session: AsyncSession) -> None:
    """SVG is the one this exists for — a script host served from our origin."""
    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(
            sa.text(
                "INSERT INTO media_objects (digest, extension, data)"
                f" VALUES ('{'a' * 64}', 'svg', '\\x00'::bytea)"
            )
        )
        await session.flush()


async def test_the_same_bytes_under_a_different_extension_are_not_served(
    session: AsyncSession,
) -> None:
    """The digest decides identity, so the extension has to agree with the row.

    Otherwise one upload would be reachable at four URLs, three of them
    declaring a content type the bytes are not.
    """
    data = b"GIF89a" + b"\x22" * 64
    await store_image(data, session=session)
    await session.flush()

    digest = hashlib.sha256(data).hexdigest()
    assert await load_image(digest, "gif", session=session) is not None
    assert await load_image(digest, "png", session=session) is None
