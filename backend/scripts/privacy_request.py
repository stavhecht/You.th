"""Answer an access or erasure request for one email address.

    python -m scripts.privacy_request --email someone@example.com
    python -m scripts.privacy_request --email someone@example.com --export out.json
    python -m scripts.privacy_request --email someone@example.com --erase --apply

For requests that arrive through the privacy contact rather than the reader's
own "Download my data" / "Delete account" buttons on ``/you``: a contact-form
sender has no account to sign in to, and a reader may have lost their password.
**Verify the requester controls the address before running either action** —
signup never confirms email ownership, so the address alone proves nothing.

What it covers, by table:

- ``readers`` (plus ``reader_folders`` and ``reader_saves`` by cascade) — the
  account, through ``ReaderService`` so this and the ``/you`` button erase the
  same rows.
- ``contact_requests`` — every message sent from the address.
- ``users`` — **reported, never erased.** A reviewer row is the answer to "who
  approved this article", and removing staff access is ``is_active = false``,
  a different decision from a reader's erasure request.

What it cannot reach, and the privacy policy says so: database backups (the
2026-09-30 ``evidwell_backup_20260930`` copy among them), and server logs.

The default output is counts only, so running it does not echo someone's
messages into a terminal scrollback; ``--export`` writes the full copy to a file.
Dry by default, like every maintenance script here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import dispose_engine, get_session_factory
from app.domain.models import ContactRequest, Reader, User
from app.services.reader import ReaderService

_CONTACT_FIELDS = ("kind", "name", "email", "link", "note", "status", "created_at")


def _normalise(email: str) -> str:
    return email.strip().lower()


async def holdings(session: AsyncSession, email: str) -> dict[str, Any]:
    """Everything held for an address, in the shape ``--export`` writes."""
    address = _normalise(email)

    reader_id = (
        await session.execute(select(Reader.id).where(func.lower(Reader.email) == address))
    ).scalar_one_or_none()
    account = await ReaderService(session).export(reader_id) if reader_id else None

    contacts = (
        await session.execute(
            select(ContactRequest)
            .where(func.lower(ContactRequest.email) == address)
            .order_by(ContactRequest.created_at)
        )
    ).scalars()

    reviewer = (
        await session.execute(
            select(User.id, User.is_active).where(func.lower(User.email) == address)
        )
    ).one_or_none()

    return {
        "email": address,
        "reader_account": account,
        "contact_requests": [
            {field: getattr(row, field) for field in _CONTACT_FIELDS} for row in contacts
        ],
        "reviewer_account": (
            {"id": reviewer.id, "is_active": reviewer.is_active} if reviewer else None
        ),
    }


async def erase(session: AsyncSession, email: str) -> dict[str, int]:
    """Delete the reader account and contact requests for an address.

    Does not commit; the caller decides. Returns what was deleted.
    """
    address = _normalise(email)
    reader_id = (
        await session.execute(select(Reader.id).where(func.lower(Reader.email) == address))
    ).scalar_one_or_none()
    if reader_id:
        await ReaderService(session).delete_account(reader_id)

    result = await session.execute(
        delete(ContactRequest).where(func.lower(ContactRequest.email) == address)
    )
    return {
        "reader_accounts": 1 if reader_id else 0,
        "contact_requests": result.rowcount or 0,  # type: ignore[attr-defined]
    }


def _summary(held: dict[str, Any]) -> str:
    account = held["reader_account"]
    lines = [
        f"reader account: {'yes' if account else 'no'}"
        + (
            f" ({len(account['folders'])} folders, "
            f"{sum(len(f['saves']) for f in account['folders'])} saved articles)"
            if account
            else ""
        ),
        f"contact requests: {len(held['contact_requests'])}",
    ]
    if held["reviewer_account"]:
        lines.append(
            "reviewer account: yes — not erased by this script; deactivate it in "
            "`users` if that is what is being asked"
        )
    return "\n".join(lines)


async def run(*, email: str, export: Path | None, do_erase: bool, apply: bool) -> int:
    factory = get_session_factory()
    async with factory() as session:
        held = await holdings(session, email)
        print(_summary(held))

        if export is not None:
            export.write_text(json.dumps(jsonable_encoder(held), indent=2, ensure_ascii=False))
            print(f"wrote {export}")

        if do_erase:
            if not apply:
                print("dry run; pass --apply with --erase to delete")
                return 0
            deleted = await erase(session, email)
            await session.commit()
            print(
                f"deleted {deleted['reader_accounts']} reader account(s) and "
                f"{deleted['contact_requests']} contact request(s)"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--export", type=Path, help="write everything held to this JSON file")
    parser.add_argument("--erase", action="store_true", help="delete (needs --apply)")
    parser.add_argument("--apply", action="store_true", help="actually delete")
    args = parser.parse_args()

    async def _main() -> int:
        try:
            return await run(
                email=args.email, export=args.export, do_erase=args.erase, apply=args.apply
            )
        finally:
            await dispose_engine()

    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
