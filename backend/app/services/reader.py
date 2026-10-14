"""Reader accounts: signup, profile, folders, saves.

The public side's counterpart to ``services/review.py``, and deliberately much
smaller. A reader account does three things and no more:

1. **Orders the feed.** Interests lift subjects to the top; nothing is hidden.
   See ``FeedService.page``.
2. **Keeps folders.** A shelf survives the browser, which is the whole reason
   an account exists rather than ``localStorage``.
3. **Records a newsletter opt-in.** A boolean, and nothing that sends anything
   — the sending half is not built, and a column that claims otherwise would
   be worse than an honest one.

What it deliberately cannot do: read a draft, influence what publishes, or see
anything the anonymous feed does not. Invariant #1 is about who decides what
the public sees, and a reader is the public.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ArticleStatus, Subject
from app.domain.models import Article, Reader, ReaderFolder, ReaderSave
from app.security.auth import hash_password

logger = logging.getLogger(__name__)

#: Created with every account, so "Save" has somewhere to go before the reader
#: has thought about folders at all.
DEFAULT_FOLDER_NAME = "Saved"

#: Cheap guards against a folder list that cannot be rendered. Not security —
#: the point is that a reader cannot make their own account unusable.
MAX_FOLDERS = 40
MAX_FOLDER_NAME_CHARS = 60


class ReaderError(RuntimeError):
    """An illegal reader operation. Surfaces as HTTP 409."""


class EmailAlreadyRegistered(ReaderError):
    """Signup for an address that already has an account."""


class ReaderService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- account ----------------------------------------------------------

    async def signup(
        self,
        email: str,
        password: str,
        display_name: str,
        interests: Sequence[Subject] = (),
        newsletter: bool = False,
    ) -> Reader:
        """Create an account and its first folder.

        The uniqueness check is the database's, not a prior SELECT: two signups
        for the same address racing each other both pass a read-then-write
        check and only one passes the unique index. Catching the
        ``IntegrityError`` is what makes the second one a 409 rather than a
        500.
        """
        reader = Reader(
            email=email.strip().lower(),
            password_hash=hash_password(password),
            display_name=display_name.strip(),
            interests=list(interests),
            newsletter=newsletter,
        )
        try:
            # A SAVEPOINT rather than a bare flush, for the same reason
            # SourceCache uses one: a failed INSERT poisons the transaction,
            # and rolling the whole session back would take anything else in
            # this request with it. Here that is only the folder below, but the
            # rule is the shape of the fix, not the size of the loss.
            async with self._session.begin_nested():
                self._session.add(reader)
                await self._session.flush()
        except IntegrityError as exc:
            raise EmailAlreadyRegistered(
                "an account already exists for that email address"
            ) from exc

        self._session.add(
            ReaderFolder(reader_id=reader.id, name=DEFAULT_FOLDER_NAME)
        )
        await self._session.flush()
        logger.info("reader %s signed up", reader.id)
        return reader

    async def by_email(self, email: str) -> Reader | None:
        result = await self._session.execute(
            select(Reader).where(
                Reader.email == email.strip().lower(), Reader.is_active.is_(True)
            )
        )
        return result.scalar_one_or_none()

    async def get(self, reader_id: str) -> Reader:
        reader = await self._session.get(Reader, reader_id)
        if reader is None or not reader.is_active:
            raise ReaderError("no such reader")
        return reader

    async def update_profile(
        self,
        reader_id: str,
        *,
        display_name: str | None = None,
        interests: Sequence[Subject] | None = None,
        newsletter: bool | None = None,
    ) -> Reader:
        """Patch semantics: ``None`` means "leave it alone".

        An empty ``interests`` list is therefore a real instruction — clear
        them — and distinct from omitting the field. That distinction matters
        because clearing interests is how a reader turns personalisation off,
        and collapsing it into "unset" would make that impossible.
        """
        reader = await self.get(reader_id)
        if display_name is not None:
            reader.display_name = display_name.strip()
        if interests is not None:
            reader.interests = list(interests)
        if newsletter is not None:
            reader.newsletter = newsletter
        await self._session.flush()
        return reader

    async def export(self, reader_id: str) -> dict[str, Any]:
        """Everything this account holds about the reader, as plain data.

        The access/portability copy (GDPR Arts 15 and 20, Israeli PPL s.13).
        Built from the tables, not from the API's response models, so a field
        added to ``readers`` later is not silently left out of the copy just
        because no screen shows it yet — ``test_the_export_carries_every_reader_column``
        fails when that happens.

        Excludes ``password_hash`` deliberately: it is a credential, not
        information about the person, and handing it out only gives an
        offline-cracking target to whoever gets hold of the file.

        Contact-form requests are **not** included even when the address
        matches. Signup never verifies that the reader owns their email, so
        matching on it would let anyone read messages someone else sent by
        registering with that person's address. Those go through the privacy
        contact and ``scripts/privacy_request.py`` instead.
        """
        reader = await self.get(reader_id)
        folders = await self.folders(reader_id)

        rows = await self._session.execute(
            select(
                Article.slug,
                Article.headline,
                ReaderSave.folder_id,
                ReaderSave.created_at,
            )
            .join(ReaderSave, ReaderSave.article_id == Article.id)
            .where(ReaderSave.reader_id == reader_id)
            .order_by(ReaderSave.created_at)
        )
        saves_by_folder: dict[str, list[dict[str, Any]]] = {}
        for row in rows.all():
            saves_by_folder.setdefault(row.folder_id, []).append(
                {"article": row.slug, "headline": row.headline, "saved_at": row.created_at}
            )

        return {
            "account": {
                column.name: getattr(reader, column.key)
                for column in Reader.__table__.columns
                if column.name != "password_hash"
            },
            "folders": [
                {
                    "name": folder["name"],
                    "created_at": folder["created_at"],
                    "saves": saves_by_folder.get(folder["id"], []),
                }
                for folder in folders
            ],
        }

    async def delete_account(self, reader_id: str) -> None:
        """Erase the account: the ``readers`` row, its folders and its saves.

        A hard delete, not ``is_active = false``. A deactivated row still holds
        the email, name and interests, which is exactly what an erasure request
        asks us to stop holding. Folders and saves go with it through
        ``ON DELETE CASCADE`` (``reader_folders``, ``reader_saves``); nothing
        else references ``readers``, so no published article or audit trail is
        touched.

        What this cannot reach, and the privacy policy says so: copies already
        in database backups, and server logs, which age out on their own
        schedules rather than being rewritten.
        """
        result = await self._session.execute(
            delete(Reader).where(Reader.id == reader_id)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise ReaderError("no such reader")
        await self._session.flush()
        logger.info("reader %s deleted their account", reader_id)

    # --- folders ----------------------------------------------------------

    async def folders(self, reader_id: str) -> list[dict[str, Any]]:
        """Every folder with the number of articles on it, oldest first.

        Oldest first keeps "Saved" — created at signup — at the head, so the
        default shelf does not move when a reader adds one.
        """
        rows = await self._session.execute(
            select(ReaderFolder).where(ReaderFolder.reader_id == reader_id)
            .order_by(ReaderFolder.created_at, ReaderFolder.id)
        )
        folders = list(rows.scalars())

        counts = await self._session.execute(
            select(ReaderSave.folder_id, func.count())
            .where(ReaderSave.reader_id == reader_id)
            .group_by(ReaderSave.folder_id)
        )
        by_folder = {folder_id: total for folder_id, total in counts.all()}

        return [
            {
                "id": folder.id,
                "name": folder.name,
                "count": by_folder.get(folder.id, 0),
                "created_at": folder.created_at,
            }
            for folder in folders
        ]

    async def create_folder(self, reader_id: str, name: str) -> ReaderFolder:
        cleaned = name.strip()
        if not cleaned:
            raise ReaderError("a folder needs a name")
        if len(cleaned) > MAX_FOLDER_NAME_CHARS:
            raise ReaderError(
                f"folder names are limited to {MAX_FOLDER_NAME_CHARS} characters"
            )

        existing = await self._session.execute(
            select(ReaderFolder).where(ReaderFolder.reader_id == reader_id)
        )
        if len(list(existing.scalars())) >= MAX_FOLDERS:
            raise ReaderError(f"you can keep up to {MAX_FOLDERS} folders")

        folder = ReaderFolder(reader_id=reader_id, name=cleaned)
        try:
            async with self._session.begin_nested():
                self._session.add(folder)
                await self._session.flush()
        except IntegrityError as exc:
            raise ReaderError(f"you already have a folder called “{cleaned}”") from exc
        return folder

    async def delete_folder(self, reader_id: str, folder_id: str) -> None:
        """Remove a folder and the saves on it.

        Refuses the last one: "Save" needs somewhere to go, and an account with
        no folders is a dead end a reader cannot get out of from the UI.
        """
        result = await self._session.execute(
            select(ReaderFolder).where(ReaderFolder.reader_id == reader_id)
        )
        folders = list(result.scalars())
        if len(folders) <= 1:
            raise ReaderError("your last folder cannot be deleted")
        if not any(folder.id == folder_id for folder in folders):
            raise ReaderError("no such folder")

        await self._session.execute(
            delete(ReaderSave).where(
                ReaderSave.reader_id == reader_id, ReaderSave.folder_id == folder_id
            )
        )
        await self._session.execute(
            delete(ReaderFolder).where(ReaderFolder.id == folder_id)
        )
        await self._session.flush()

    # --- saves ------------------------------------------------------------

    async def save(
        self, reader_id: str, slug: str, folder_id: str | None = None
    ) -> str:
        """Put a published article on a shelf. Returns the folder it landed in.

        Idempotent, and a move rather than a copy: the primary key is
        ``(reader_id, article_id)``, so saving something already saved into a
        different folder relocates it.

        Only published articles can be saved, and the check is a join against
        the same ``status = 'published'`` filter the feed uses — a slug that is
        not public must behave here exactly as it does there, or saving becomes
        a probe for unpublished drafts.
        """
        article_id = await self._published_article_id(slug)
        target = folder_id or await self._default_folder_id(reader_id)
        await self._assert_owns_folder(reader_id, target)

        existing = await self._session.get(ReaderSave, (reader_id, article_id))
        if existing is None:
            self._session.add(
                ReaderSave(
                    reader_id=reader_id, article_id=article_id, folder_id=target
                )
            )
        else:
            existing.folder_id = target
        await self._session.flush()
        return target

    async def unsave(self, reader_id: str, slug: str) -> None:
        """Take an article off every shelf. Silent when it was not on one."""
        article_id = await self._published_article_id(slug)
        await self._session.execute(
            delete(ReaderSave).where(
                ReaderSave.reader_id == reader_id, ReaderSave.article_id == article_id
            )
        )
        await self._session.flush()

    async def saved_cards(
        self, reader_id: str, folder_id: str | None = None
    ) -> list[dict[str, Any]]:
        """The reader's shelf as feed cards, newest save first.

        Returns the same card shape the feed serves, so the saved grid and the
        feed grid render from one component. Not paginated: a shelf is a
        human-sized list, and the folder tabs above it are the navigation.
        """
        statement = (
            select(
                Article.slug,
                Article.card_headline,
                Article.card_excerpt,
                Article.card_verdict,
                Article.verdict_qualifier,
                Article.card_image,
                Article.card_image_alt,
                Article.subject,
                Article.published_at,
                ReaderSave.folder_id,
            )
            .join(ReaderSave, ReaderSave.article_id == Article.id)
            .where(
                ReaderSave.reader_id == reader_id,
                # A published article can never be un-published, but the join
                # states the rule anyway: the saved grid must not become the
                # one surface where a withdrawn article stays visible.
                Article.status == ArticleStatus.PUBLISHED,
            )
            .order_by(ReaderSave.created_at.desc())
        )
        if folder_id is not None:
            statement = statement.where(ReaderSave.folder_id == folder_id)

        rows = await self._session.execute(statement)
        return [
            {
                "slug": row.slug,
                "headline": row.card_headline or "",
                "excerpt": row.card_excerpt or "",
                "verdict": row.card_verdict,
                "verdict_qualifier": row.verdict_qualifier,
                "image": row.card_image,
                "image_alt": row.card_image_alt,
                "subject": row.subject,
                "published_at": row.published_at,
                "folder_id": row.folder_id,
            }
            for row in rows.all()
        ]

    async def saved_slugs(self, reader_id: str) -> dict[str, str]:
        """``{slug: folder_id}`` for everything this reader has saved.

        Sent with the feed so every tile knows its own state in one request
        rather than one per card. Small by construction — a shelf is human
        sized — and cheap enough to fetch alongside a page of cards.
        """
        rows = await self._session.execute(
            select(Article.slug, ReaderSave.folder_id)
            .join(ReaderSave, ReaderSave.article_id == Article.id)
            .where(ReaderSave.reader_id == reader_id)
        )
        return {slug: folder_id for slug, folder_id in rows.all()}

    # --- internals --------------------------------------------------------

    async def _published_article_id(self, slug: str) -> str:
        result = await self._session.execute(
            select(Article.id).where(
                Article.slug == slug, Article.status == ArticleStatus.PUBLISHED
            )
        )
        article_id = result.scalar_one_or_none()
        if article_id is None:
            raise ReaderError("no such article")
        return article_id

    async def _default_folder_id(self, reader_id: str) -> str:
        result = await self._session.execute(
            select(ReaderFolder.id)
            .where(ReaderFolder.reader_id == reader_id)
            .order_by(ReaderFolder.created_at, ReaderFolder.id)
            .limit(1)
        )
        folder_id = result.scalar_one_or_none()
        if folder_id is None:
            # Only reachable if the signup-time folder was deleted out from
            # under the account, which delete_folder refuses. Recreated rather
            # than raised: losing a save because bookkeeping drifted is worse
            # than an extra row.
            folder = ReaderFolder(reader_id=reader_id, name=DEFAULT_FOLDER_NAME)
            self._session.add(folder)
            await self._session.flush()
            return folder.id
        return folder_id

    async def _assert_owns_folder(self, reader_id: str, folder_id: str) -> None:
        result = await self._session.execute(
            select(ReaderFolder.id).where(
                ReaderFolder.id == folder_id, ReaderFolder.reader_id == reader_id
            )
        )
        if result.scalar_one_or_none() is None:
            # Deliberately the same message as a folder that does not exist:
            # distinguishing "not yours" from "not there" turns folder ids into
            # an oracle for other readers' accounts.
            raise ReaderError("no such folder")
