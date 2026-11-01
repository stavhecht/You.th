"""Reader accounts, folders, saves, and the contact inbox.

Needs a live database, for the same reason ``test_db_invariants.py`` does: the
guarantees under test here are SQL ones. The composite foreign key on
``reader_saves``, the ``(reader_id, article_id)`` primary key that makes a save
a move rather than a copy, the unique index on a folder name, and the CHECK
that a handled contact request names who handled it — none of them can be
demonstrated against a fake session, and all four are the reason the schema is
shaped the way it is.

Skipped automatically when no database is reachable. **A skipped run verifies
none of this.**
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.domain.enums import (
    ArticleStatus,
    ContactKind,
    ContactStatus,
    StudyType,
    Subject,
    UserRole,
    Verdict,
)
from app.security.auth import (
    TOKEN_TYPE_READER,
    TOKEN_TYPE_REVIEWER,
    AuthenticatedReviewer,
    AuthError,
    create_access_token,
    create_reader_token,
    decode_token_subject,
    hash_password,
)
from app.services.contact import ContactService
from app.services.feed import FeedService
from app.services.reader import DEFAULT_FOLDER_NAME, ReaderError, ReaderService

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


async def _make_published_article(
    db: AsyncSession,
    *,
    subject: Subject | None = None,
    image: str | None = None,
) -> tuple[str, str]:
    """A published article. Returns ``(id, slug)``.

    Written with raw SQL and a reviewer of its own because publication requires
    one — the CHECK constraint carrying invariant #1 refuses a published row
    with no ``reviewed_by``.
    """
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

    article_id = str(uuid.uuid4())
    slug = f"saved-{article_id[:8]}"
    await db.execute(
        sa.text(
            """
            INSERT INTO articles (
                id, slug, status, topic, product, headline, summary, verdict,
                original_content, evidence_grade, subject,
                card_headline, card_excerpt, card_verdict, card_image,
                reviewed_by, reviewed_at, published_at, created_at, updated_at
            ) VALUES (
                :id, :slug, 'published', :topic, :product, :headline, :summary,
                :verdict, CAST(:content AS jsonb), :grade, :subject,
                :headline, :excerpt, :verdict, :image,
                :reviewer, now(), now(), now(), now()
            )
            """
        ),
        {
            "id": article_id,
            "slug": slug,
            "topic": "test topic",
            "product": "Test Product",
            "headline": "A test headline",
            "summary": "A test summary.",
            "excerpt": "A test excerpt.",
            "verdict": Verdict.MIXED.value,
            "content": json.dumps({"type": "doc", "content": []}),
            "grade": StudyType.RCT.value,
            "subject": subject.value if subject else None,
            "image": image,
            "reviewer": user_id,
        },
    )
    return article_id, slug


async def _make_reader(
    session: AsyncSession, interests: list[Subject] | None = None
) -> Any:
    return await ReaderService(session).signup(
        email=f"reader-{uuid.uuid4().hex[:8]}@example.com",
        password="a-sufficiently-long-password",
        display_name="Maya",
        interests=interests or [],
    )


# --- token separation ------------------------------------------------------
#
# These need no database, but they belong next to the accounts they separate.


def test_a_reader_token_is_refused_by_the_console_decoder() -> None:
    """The `typ` claim, not just the missing `users` row, is what rejects it.

    Checked here rather than only through the endpoint because the endpoint's
    401 would still happen for the wrong reason — a reader id simply not being
    in `users` — and that reason evaporates the day anyone changes that query.
    """
    token, _ = create_reader_token(str(uuid.uuid4()), "a-test-secret")

    with pytest.raises(AuthError):
        decode_token_subject(token, "a-test-secret", expect=TOKEN_TYPE_REVIEWER)

    # ...and it is a perfectly good token on its own surface.
    assert decode_token_subject(token, "a-test-secret", expect=TOKEN_TYPE_READER)


def test_a_reviewer_token_is_refused_by_the_reader_decoder() -> None:
    reviewer = AuthenticatedReviewer(
        id=str(uuid.uuid4()),
        email="r@example.com",
        display_name="R",
        role=UserRole.REVIEWER,
    )
    token, _ = create_access_token(reviewer, "a-test-secret")

    with pytest.raises(AuthError):
        decode_token_subject(token, "a-test-secret", expect=TOKEN_TYPE_READER)


# --- accounts --------------------------------------------------------------


async def test_signup_creates_a_default_folder(session: AsyncSession) -> None:
    """"Save" has somewhere to go before anyone has thought about folders."""
    reader = await _make_reader(session)

    folders = await ReaderService(session).folders(reader.id)
    assert [folder["name"] for folder in folders] == [DEFAULT_FOLDER_NAME]
    assert folders[0]["count"] == 0


async def test_a_second_signup_for_the_same_email_is_refused(
    session: AsyncSession,
) -> None:
    reader = await _make_reader(session)
    service = ReaderService(session)

    with pytest.raises(ReaderError):
        await service.signup(
            email=reader.email.upper(),  # the unique index is on lower(email)
            password="another-sufficiently-long-password",
            display_name="Someone else",
        )

    # The savepoint did its job: the session is still usable afterwards.
    assert await service.get(reader.id)


async def test_clearing_interests_is_distinct_from_omitting_them(
    session: AsyncSession,
) -> None:
    """An empty list turns personalisation off; ``None`` leaves it alone."""
    reader = await _make_reader(session, [Subject.SUPPLEMENTS])
    service = ReaderService(session)

    unchanged = await service.update_profile(reader.id, newsletter=True)
    assert list(unchanged.interests) == [Subject.SUPPLEMENTS]

    cleared = await service.update_profile(reader.id, interests=[])
    assert list(cleared.interests) == []


# --- folders and saves -----------------------------------------------------


async def test_saving_twice_moves_the_article_rather_than_copying_it(
    session: AsyncSession,
) -> None:
    """The ``(reader_id, article_id)`` primary key is the guarantee."""
    reader = await _make_reader(session)
    _, slug = await _make_published_article(session)
    service = ReaderService(session)

    first = await service.create_folder(reader.id, "To try")
    second = await service.create_folder(reader.id, "Sceptical")

    await service.save(reader.id, slug, first.id)
    await service.save(reader.id, slug, second.id)

    saved = await service.saved_cards(reader.id)
    assert [card["slug"] for card in saved] == [slug]
    assert saved[0]["folder_id"] == second.id


async def test_a_reader_cannot_save_into_someone_elses_folder(
    session: AsyncSession,
) -> None:
    """The composite FK makes this a schema fact, not a handler's discipline."""
    mine = await _make_reader(session)
    theirs = await _make_reader(session)
    _, slug = await _make_published_article(session)
    service = ReaderService(session)

    their_folder = await service.create_folder(theirs.id, "Theirs")

    with pytest.raises(ReaderError):
        await service.save(mine.id, slug, their_folder.id)


async def test_an_unpublished_article_cannot_be_saved(session: AsyncSession) -> None:
    """Saving must not become a probe for drafts.

    A slug that 404s on the public article route has to behave the same way
    here, or the save endpoint answers a question the feed refuses to.
    """
    reader = await _make_reader(session)
    article_id, slug = await _make_published_article(session)
    await session.execute(
        sa.text("UPDATE articles SET status = :status WHERE id = :id"),
        {"status": ArticleStatus.PENDING_REVIEW.value, "id": article_id},
    )

    with pytest.raises(ReaderError):
        await ReaderService(session).save(reader.id, slug)


async def test_duplicate_folder_names_are_refused_case_insensitively(
    session: AsyncSession,
) -> None:
    reader = await _make_reader(session)
    service = ReaderService(session)

    await service.create_folder(reader.id, "To try")
    with pytest.raises(ReaderError):
        await service.create_folder(reader.id, "to try")

    # Still usable — the failure rolled back to a savepoint, not the session.
    assert await service.create_folder(reader.id, "Something else")


async def test_the_last_folder_cannot_be_deleted(session: AsyncSession) -> None:
    """An account with nowhere to save is a dead end the UI cannot escape."""
    reader = await _make_reader(session)
    service = ReaderService(session)
    folders = await service.folders(reader.id)

    with pytest.raises(ReaderError):
        await service.delete_folder(reader.id, folders[0]["id"])


# --- the personalised feed -------------------------------------------------


async def test_interests_reorder_the_feed_without_hiding_anything(
    session: AsyncSession,
) -> None:
    """The whole point of tiering rather than filtering.

    A reader who picks one subject must still be able to reach every other
    article — including the unclassified ones, which no interest can ever
    match and which a filter would make permanently invisible.
    """
    await _make_published_article(session, subject=Subject.NUTRITION)
    _, wanted = await _make_published_article(session, subject=Subject.FITNESS)
    _, unclassified = await _make_published_article(session)
    await session.flush()

    items, _ = await FeedService(session).page(
        limit=50, interests=[Subject.FITNESS]
    )
    slugs = [item["slug"] for item in items]

    assert slugs[0] == wanted, "the picked subject leads"
    assert unclassified in slugs, "nothing is hidden by having an interest"


async def test_the_feed_serves_the_derived_card_image(session: AsyncSession) -> None:
    _, slug = await _make_published_article(
        session, image="/api/media/ab/" + "c" * 62 + ".png"
    )
    await session.flush()

    items, _ = await FeedService(session).page(limit=50)
    card = next(item for item in items if item["slug"] == slug)
    assert card["image"] is not None


# --- contact ---------------------------------------------------------------


async def test_a_handled_request_has_to_name_who_handled_it(
    session: AsyncSession,
) -> None:
    """Enforced by a CHECK, so it survives the service being wrong."""
    service = ContactService(session)
    request = await service.submit(
        kind=ContactKind.FACT_CHECK,
        email="maya@example.com",
        note="Someone on my feed says collagen powder rebuilds cartilage.",
        link="https://example.com/a-post",
    )
    assert request.status is ContactStatus.NEW

    # `IntegrityError` specifically, not any exception: a typo in the SQL below
    # would raise too, and would pass a blind `Exception` assertion while
    # verifying nothing about the constraint this test is named for.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                sa.text(
                    "UPDATE contact_requests SET status = 'answered' WHERE id = :id"
                ),
                {"id": request.id},
            )


async def test_reopening_a_request_clears_the_handler(session: AsyncSession) -> None:
    user_id = str(uuid.uuid4())
    await session.execute(
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

    service = ContactService(session)
    request = await service.submit(
        kind=ContactKind.TOPIC, email="maya@example.com", note="Cover creatine."
    )

    await service.set_status(request.id, ContactStatus.ANSWERED, user_id)
    assert request.handled_by == user_id

    # Reopening has to clear both fields or the CHECK refuses the row.
    await service.set_status(request.id, ContactStatus.NEW, user_id)
    assert request.handled_by is None
    assert request.handled_at is None
