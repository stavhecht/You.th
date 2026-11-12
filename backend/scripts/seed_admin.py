"""Create the first reviewer account.

    python -m scripts.seed_admin --email you@example.com --name "Your Name"

Prompts for a password rather than taking it as an argument — an argument lands
in shell history and in `ps` output.

Idempotent: re-running with an existing email updates that user's password
rather than failing, so it doubles as a password reset for local development.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.db import dispose_engine, session_scope
from app.domain.enums import UserRole
from app.domain.models import User
from app.security.auth import hash_password

MIN_PASSWORD_LENGTH = 12


async def seed(email: str, display_name: str, password: str, role: UserRole) -> None:
    async with session_scope() as session:
        normalised = email.strip().lower()
        result = await session.execute(select(User).where(User.email == normalised))
        user = result.scalar_one_or_none()

        if user is None:
            session.add(
                User(
                    email=normalised,
                    display_name=display_name,
                    password_hash=hash_password(password),
                    role=role,
                    is_active=True,
                )
            )
            print(f"Created {role} account for {normalised}")
        else:
            user.password_hash = hash_password(password)
            user.display_name = display_name
            user.role = role
            user.is_active = True
            print(f"Updated existing account for {normalised}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update a reviewer account")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True, dest="display_name")
    parser.add_argument(
        "--role", default=UserRole.ADMIN, choices=[role.value for role in UserRole]
    )
    args = parser.parse_args()

    # The name is printed on every article this reviewer approves ("Checked and
    # approved by ..."), so it has to be a name, never the login address.
    display_name = args.display_name.strip()
    if not display_name or "@" in display_name:
        print("--name must be the reviewer's name as readers should see it, not an email.",
              file=sys.stderr)
        return 1
    args.display_name = display_name

    password = getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
            file=sys.stderr,
        )
        return 1
    if password != getpass.getpass("Confirm password: "):
        print("Passwords did not match.", file=sys.stderr)
        return 1

    async def runner() -> None:
        try:
            await seed(args.email, args.display_name, password, UserRole(args.role))
        finally:
            await dispose_engine()

    asyncio.run(runner())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
