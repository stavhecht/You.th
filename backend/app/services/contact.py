"""The "Let us know" inbox.

Public writes, reviewer-only reads. Two rules worth stating because both are
easy to soften later:

* **A submission is never acted on automatically.** It does not create a
  pipeline run and it does not become a topic. A reviewer reads it and decides,
  which is the same principle as invariant #1 applied at the other end of the
  pipeline: nothing reaches generation because a stranger asked for it.
* **Nothing here is ever shown publicly.** No "recently requested" list, no
  counts. The rows carry an email address and free text written by anyone with
  the URL.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ContactKind
from app.domain.models import ContactRequest

logger = logging.getLogger(__name__)

#: Enough for a paragraph and a link, short enough that the column cannot be
#: used as free storage.
MAX_NOTE_CHARS = 4000
MAX_LINK_CHARS = 2000


class ContactError(RuntimeError):
    """An illegal contact operation. Surfaces as HTTP 409."""


class ContactService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def submit(
        self,
        *,
        kind: ContactKind,
        email: str,
        note: str,
        name: str | None = None,
        link: str | None = None,
    ) -> ContactRequest:
        """Record a submission."""
        cleaned_note = note.strip()
        cleaned_name = (name or "").strip()
        if not cleaned_name:
            raise ContactError("tell us your name")
        if not cleaned_note and not (link or "").strip():
            raise ContactError("add a link or tell us what this is about")
        if len(cleaned_note) > MAX_NOTE_CHARS:
            raise ContactError(
                f"that note is longer than the {MAX_NOTE_CHARS} characters we can take"
            )
        if link and len(link) > MAX_LINK_CHARS:
            raise ContactError("that link is too long")

        request = ContactRequest(
            kind=kind,
            email=email.strip().lower(),
            note=cleaned_note,
            name=cleaned_name,
            link=(link or "").strip() or None,
        )
        self._session.add(request)
        await self._session.flush()
        logger.info("contact request %s received (%s)", request.id, kind)
        return request

    async def inbox(self, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first. Unpaginated on purpose — this is a small inbox, and a
        cursor here would be scaffolding for traffic that does not exist."""
        statement = select(ContactRequest).order_by(ContactRequest.created_at.desc())
        rows = await self._session.execute(statement.limit(limit))
        return [
            {
                "id": row.id,
                "kind": row.kind,
                "name": row.name,
                "email": row.email,
                "link": row.link,
                "note": row.note,
                "created_at": row.created_at,
            }
            for row in rows.scalars()
        ]
